"""Keep TIDAL playlists filled with the real discography of every followed artist."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter

from . import __version__, config as config_mod, login as login_mod
from .api import Auth, AuthError, Client, TidalError
from .collect import Collector, Incomplete, followed_artists
from .plan import build_plan, pack, sort_name
from .playlists import AddsRejected, PlaylistSync
from .state import State

log = logging.getLogger("tidal_disco")


def _client(cfg, state) -> Client:
    auth = Auth(cfg.client_id, cfg.client_secret, state, cfg.refresh_token, cfg.token_url)
    return Client(auth, cfg.api_base, cfg.country_code, cfg.requests_per_second)


def _notify(cfg, text: str) -> None:
    if not cfg.discord_webhook_url:
        return
    try:
        import requests

        requests.post(cfg.discord_webhook_url, json={"content": text[:1900]}, timeout=15)
    except Exception as exc:  # a failed notice must not fail the run
        log.warning("could not post to Discord: %s", exc)


def _print_artist(block, excluded) -> None:
    print(f"\n{block.artist_name}  ({len(block.releases)} releases, {len(block.track_ids)} tracks)")
    for r in block.releases:
        title = r["title"] + (f" ({r['version']})" if r.get("version") else "")
        of = r.get("of", r["tracks"])
        if r["tracks"] == 0:
            count = "all tracks already listed"
        elif r["tracks"] < of:
            count = f"{r['tracks']} of {of} tracks, the rest already listed"
        else:
            count = f"{r['tracks']} tracks"
        print(f"  + {r['date'] or '????-??-??'}  {r['type']:<6} {title}  [{count}]  id {r['album_id']}")
    for e in excluded:
        title = e["title"] + (f" ({e['version']})" if e["version"] else "")
        print(f"  - {e['date'] or '????-??-??'}  {e['type']:<6} {title}  id {e['album_id']}\n      left out: {e['reason']}")


def cmd_sync(args, cfg) -> int:
    started = time.time()
    state = State(cfg.state_path)
    client = _client(cfg, state)
    dry = args.dry_run or bool(args.only)

    artists = [a for a in followed_artists(client) if a.id not in cfg.skip_artist_ids]
    followed_ids = {a.id for a in artists}
    previous = int(state.get("followed_count", "0"))
    if not artists:
        print("TIDAL returned no followed artists; nothing was changed.", file=sys.stderr)
        return 2
    if previous and len(artists) < previous * 0.5 and not args.force:
        print(f"TIDAL returned {len(artists)} followed artists, down from {previous}. "
              "Nothing was changed; run with --force if that is right.", file=sys.stderr)
        return 2
    if args.only:
        wanted = [w.casefold() for w in args.only]
        artists = [a for a in artists if a.id in args.only or any(w in a.name.casefold() for w in wanted)]
        if not artists:
            print("no followed artist matches --only", file=sys.stderr)
            return 2
    artists.sort(key=lambda a: (sort_name(a.name), a.id))

    deadline = started + cfg.max_minutes * 60 if cfg.max_minutes else None
    collector = Collector(client, state, cfg, deadline)
    albums, warnings, stale = {}, [], []
    done = 0
    try:
        for n, artist in enumerate(artists, 1):
            before = collector.fetched
            try:
                albums[artist.id] = collector.collect_artist(artist)
            except TidalError as exc:
                fallback = collector.cached_artist(artist)
                if fallback is None:
                    warnings.append(f"{artist.name}: TIDAL error {exc.status}, skipped this run")
                else:
                    albums[artist.id] = fallback
                    stale.append(artist.name)
                    warnings.append(f"{artist.name}: TIDAL error {exc.status}, used the last good result")
            done = n
            log.info("[%d/%d] %s: %d releases listed, %d fetched", n, len(artists), artist.name,
                     len(albums.get(artist.id, [])), collector.fetched - before)
    except Incomplete:
        msg = (f"Time budget reached after {done} of {len(artists)} artists "
               f"({collector.fetched} releases fetched and cached). Playlists were not touched; "
               "the next run picks up where this one stopped.")
        print(msg)
        _notify(cfg, f"tidal-discography: {msg}")
        return 0

    plan = build_plan(artists, albums, cfg)

    if args.only:
        by_artist = {}
        for e in plan.excluded:
            by_artist.setdefault(e["artist_id"], []).append(e)
        for block in plan.blocks:
            _print_artist(block, by_artist.get(block.artist_id, []))
        if cfg.dedupe != "recordings" and plan.repeat_recordings:
            print(f"\n{plan.repeat_recordings} tracks repeat a recording already listed; "
                  'dedupe = "recordings" keeps each recording once.')
        print(f"\n(dedupe = {cfg.dedupe}; --only is a preview: no playlist was changed)")
        return 0

    parts, labels, mapping = pack(plan.blocks, state.block_parts(), cfg.part_target, cfg.part_cap)
    for known in state.known_parts():
        parts.setdefault(known, [])
        labels.setdefault(known, ("", ""))
    per_part = Counter(mapping.values())
    if not dry:
        state.save_block_parts(mapping)
        state.set("followed_count", str(len(artists)))

    # removals that are expected: the track now lives in another part, or its artist was unfollowed
    explained = {t for tracks in parts.values() for t in tracks}
    for artist_id, album_ids in state.all_artist_results().items():
        if artist_id not in followed_ids:
            for album_id in album_ids:
                cached = state.get_album(album_id)
                if cached:
                    explained.update(t["id"] for t in cached[0].get("tracks", []))

    syncer = PlaylistSync(client, state, cfg, dry_run=dry)
    results, failed = [], False
    for part in sorted(parts):
        try:
            res = syncer.sync_part(part, parts[part], labels[part], per_part.get(part, 0),
                                   force=args.force, rebuild=args.rebuild, explained=explained)
        except (TidalError, AddsRejected) as exc:
            failed = True
            res = {"part": part, "name": cfg.part_name(part), "desired": len(parts[part]),
                   "added": 0, "removed": 0, "status": "failed", "warnings": [f"{cfg.part_name(part)}: {exc}"]}
        results.append(res)
        warnings += res["warnings"]
        log.info("%s: %s, +%d -%d (%d wanted)", res["name"], res["status"], res["added"],
                 res["removed"], res["desired"])

    reasons = Counter(e["reason"].split(" (")[0].split(" of ")[0].split(" /")[0] for e in plan.excluded)
    report = {
        "version": __version__, "started": started, "finished": time.time(), "dry_run": dry,
        "artists": len(artists), "releases": plan.n_releases, "tracks": plan.n_tracks,
        "repeat_recordings": plan.repeat_recordings, "dedupe": cfg.dedupe,
        "excluded_by_reason": dict(reasons), "excluded": plan.excluded, "unchecked": plan.unchecked,
        "parts": results, "warnings": warnings, "stale_artists": stale,
        "api_calls": client.calls, "rate_limited": client.rate_limited,
    }
    if not dry:
        state.set("last_report", json.dumps(report))
    if args.report_file:
        with open(args.report_file, "w") as fh:
            json.dump(report, fh, indent=1)

    added = sum(r["added"] for r in results)
    removed = sum(r["removed"] for r in results)
    verb = "would add" if dry else "added"
    lines = [
        f"{len(artists)} artists, {plan.n_releases} releases, {plan.n_tracks} tracks "
        f"in {len(results)} playlist part(s).",
        f"{verb} {added}, {'would remove' if dry else 'removed'} {removed}.",
        f"Left out {len(plan.excluded)} releases: "
        + (", ".join(f"{n} {r}" for r, n in reasons.most_common(6)) or "none") + ".",
    ]
    if cfg.dedupe != "recordings" and plan.repeat_recordings:
        lines.append(f"{plan.repeat_recordings} tracks repeat a recording already in the list "
                     "(single and album copies); dedupe = \"recordings\" drops them.")
    if plan.unchecked:
        lines.append(f"{len(plan.unchecked)} multi-track releases were kept without a credit check because "
                     "TIDAL returned no track credits for them (listed in `tidal-disco report`).")
    lines += [f"WARNING: {w}" for w in warnings[:15]]
    if len(warnings) > 15:
        lines.append(f"... and {len(warnings) - 15} more warnings (see `tidal-disco report`)")
    summary = "\n".join(lines)
    print(summary)
    if not dry and (added or removed or warnings):
        _notify(cfg, "tidal-discography\n" + summary)
    return 1 if failed else 0


def cmd_report(args, cfg) -> int:
    state = State(cfg.state_path)
    raw = state.get("last_report")
    if not raw:
        print("no finished run yet")
        return 1
    report = json.loads(raw)
    if args.json:
        print(json.dumps(report, indent=1))
        return 0
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(report["finished"]))
    print(f"Last run {when}: {report['artists']} artists, {report['releases']} releases, "
          f"{report['tracks']} tracks, {report['api_calls']} API calls.")
    for p in report["parts"]:
        print(f"  {p['name']}: {p['status']}, +{p['added']} -{p['removed']}, {p['desired']} wanted")
    needle = (args.artist or "").casefold()
    rows = [e for e in report["excluded"] if needle in e["artist"].casefold()]
    print(f"\nLeft out ({len(rows)} releases):")
    current = None
    for e in sorted(rows, key=lambda e: (sort_name(e["artist"]), e["date"])):
        if e["artist"] != current:
            current = e["artist"]
            print(f"\n{current}")
        title = e["title"] + (f" ({e['version']})" if e["version"] else "")
        print(f"  {e['date'] or '????-??-??'}  {e['type']:<6} {title}  id {e['album_id']}: {e['reason']}")
    unchecked = [e for e in report.get("unchecked", []) if needle in e["artist"].casefold()]
    if unchecked:
        print(f"\nKept without a credit check ({len(unchecked)} releases):")
        for e in sorted(unchecked, key=lambda e: (sort_name(e["artist"]), e["date"])):
            print(f"  {e['artist']}: {e['date'] or '????-??-??'}  {e['type']:<6} {e['title']}  id {e['album_id']}")
    for w in report["warnings"]:
        print(f"WARNING: {w}")
    return 0


def cmd_login(args, cfg) -> int:
    argv = ["--client-id", args.client_id or cfg.client_id, "--redirect", args.redirect]
    if args.client_secret or cfg.client_secret:
        argv += ["--client-secret", args.client_secret or cfg.client_secret]
    if args.paste:
        argv.append("--paste")
    return login_mod.main(argv)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tidal-disco", description=__doc__)
    parser.add_argument("--config", help="path to config.toml (or set TIDAL_DISCO_CONFIG)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sync", help="update the playlists")
    s.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    s.add_argument("--only", action="append", metavar="ARTIST",
                   help="preview one artist (name fragment or id): kept and left-out releases")
    s.add_argument("--force", action="store_true", help="skip the safety limits")
    s.add_argument("--rebuild", action="store_true", help="remove and re-add every managed track, in order")
    s.add_argument("--report-file", help="also write the run report to this JSON file")
    s.set_defaults(func=cmd_sync)

    r = sub.add_parser("report", help="what the last run kept and left out")
    r.add_argument("--artist", help="only this artist (name fragment)")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_report)

    lg = sub.add_parser("login", help="one-time TIDAL login; prints a refresh token")
    lg.add_argument("--client-id")
    lg.add_argument("--client-secret")
    lg.add_argument("--redirect", default=login_mod.REDIRECT)
    lg.add_argument("--paste", action="store_true", help="paste the callback address instead of listening")
    lg.set_defaults(func=cmd_login)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    cfg = config_mod.load(args.config)
    try:
        return args.func(args, cfg)
    except AuthError as exc:
        print(f"auth: {exc}", file=sys.stderr)
        _notify(cfg, f"tidal-discography could not log in: {exc}")
        return 1
    except TidalError as exc:
        print(f"TIDAL API error: {exc}", file=sys.stderr)
        _notify(cfg, f"tidal-discography failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
