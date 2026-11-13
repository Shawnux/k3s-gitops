"""Write side: make each playlist part match its desired track list.

Rules that keep this safe:
- only tracks this tool added are ever removed (unless prune_unmanaged is on);
- a run that wants to remove a large share of a playlist for no known reason
  (not a move between parts, not an unfollow) skips those removals;
- TIDAL can accept an addition and not make it (past 10,000 items, or a track
  it won't add), so every write is followed by a count check. On a mismatch
  the playlist is re-read to find the tracks that did not land; those are
  tried once more on the next run, then skipped for 14 days.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid

from .api import TidalError

log = logging.getLogger("tidal_disco.playlists")

HARD_CAP = 10000
ADD_BATCH = 50
REMOVE_BATCH = 50
REJECT_ABORT = 25  # this many refused tracks with nothing accepted means the request itself is wrong


class AddsRejected(RuntimeError):
    pass


def desired_hash(desired: list[str], skipped: set[str]) -> str:
    h = hashlib.sha256()
    h.update("\n".join(desired).encode())
    h.update(b"|")
    h.update("\n".join(sorted(skipped & set(desired))).encode())
    return h.hexdigest()


def plan_changes(current: list[dict], desired: list[str], managed: set[str], prune_unmanaged: bool,
                 rebuild: bool = False):
    """current: [{'track', 'item', 'type'}] in playlist order.
    Returns (items to remove, items kept)."""
    want = set(desired)
    remove, keep, seen = [], [], set()
    for it in current:
        ours = prune_unmanaged or it["track"] in managed
        stale = rebuild or it["track"] not in want or it["track"] in seen
        if it["type"] == "tracks" and ours and stale and it.get("item"):
            remove.append(it)
        else:
            keep.append(it)
            seen.add(it["track"])
    return remove, keep


def plan_runs(keep: list[dict], desired: list[str], skip: set[str]):
    """Missing tracks grouped into runs, each with the item to insert before
    (None means append). Order inside a run is the desired order."""
    present = {}
    for it in keep:
        present.setdefault(it["track"], it.get("item"))
    runs, run = [], []
    for track in desired:
        if track in present:
            if run:
                runs.append((present[track], run))
                run = []
        elif track not in skip:
            run.append(track)
    if run:
        runs.append((None, run))
    return runs


class PlaylistSync:
    def __init__(self, client, state, cfg, dry_run: bool = False):
        self.client = client
        self.state = state
        self.cfg = cfg
        self.dry_run = dry_run
        self._mine = None
        self._accepted = 0

    # lookups ---------------------------------------------------------------
    def meta(self, playlist_id: str):
        try:
            doc = self.client.get(f"/playlists/{playlist_id}", {"countryCode": self.client.country})
        except TidalError as exc:
            if exc.status in (403, 404):
                return None
            raise
        data = doc.get("data")
        if not data:
            return None
        a = data.get("attributes") or {}
        return {
            "count": a.get("numberOfItems"), "modified": a.get("lastModifiedAt"),
            "name": a.get("name"), "description": a.get("description") or "",
        }

    def _my_playlists(self) -> dict[str, str]:
        if self._mine is None:
            self._mine = {}
            try:
                params = {"filter[owners.id]": "me", "countryCode": self.client.country}
                for doc in self.client.paged("/playlists", params):
                    for res in doc.get("data") or []:
                        name = (res.get("attributes") or {}).get("name")
                        if name:
                            self._mine.setdefault(name, res["id"])
            except TidalError as exc:
                if exc.status not in (400, 403, 404, 422):
                    raise
                log.warning("could not list my playlists (%s); new ones are created if needed", exc.status)
        return self._mine

    def ensure(self, part: int):
        """Playlist id for a part: the remembered one, else one of mine with the
        right name, else a new one. None in a dry run when it would be created."""
        name = self.cfg.part_name(part)
        row = self.state.get_part(part)
        if row and self.meta(row["playlist_id"]) is not None:
            return row["playlist_id"]
        pid = self._my_playlists().get(name)
        if pid is None:
            if self.dry_run:
                return None
            body = {"data": {"type": "playlists", "attributes": {
                "name": name, "accessType": self.cfg.access,
                "description": "Managed by tidal-discography."}}}
            doc = self.client.request("POST", "/playlists", body=body, idempotency_key=str(uuid.uuid4()))
            pid = doc["data"]["id"]
            self._mine[name] = pid
            log.info("created playlist %s (%s)", name, pid)
        else:
            log.info("adopted existing playlist %s (%s)", name, pid)
        if not self.dry_run:
            self.state.set_part(part, pid)
        return pid

    def read_items(self, playlist_id: str) -> list[dict]:
        items = []
        params = {"countryCode": self.client.country}
        for doc in self.client.paged(f"/playlists/{playlist_id}/relationships/items", params):
            for ident in doc.get("data") or []:
                items.append({
                    "track": str(ident["id"]), "type": ident.get("type"),
                    "item": (ident.get("meta") or {}).get("itemId"),
                })
        return items

    # writes ----------------------------------------------------------------
    def _post_add(self, playlist_id, tracks, anchor):
        # No onDuplicates: the playlist is read before every change, so the tool
        # never adds a track that is already there, and TIDAL's own duplicate
        # check might treat the same recording on another release as a repeat.
        body = {"data": [{"id": t, "type": "tracks"} for t in tracks]}
        if anchor:
            body["meta"] = {"positionBefore": anchor}
        self.client.request(
            "POST", f"/playlists/{playlist_id}/relationships/items", body=body,
            idempotency_key=str(uuid.uuid4()),
        )

    def _add(self, playlist_id, tracks, anchor, rejected: dict) -> list[str]:
        """Add a batch before anchor; on a refusal, halve it to find the bad track."""
        try:
            self._post_add(playlist_id, tracks, anchor)
            self._accepted += len(tracks)
            return list(tracks)
        except TidalError as exc:
            if exc.status not in (400, 403, 404, 409, 422):
                raise
            if len(tracks) == 1:
                rejected[tracks[0]] = f"{exc.status} {exc.body[:120]}"
                if not self._accepted and len(rejected) >= REJECT_ABORT:
                    raise AddsRejected(
                        f"TIDAL refuses every addition: {exc.status} {exc.body[:300]}") from exc
                return []
            mid = len(tracks) // 2
            done = self._add(playlist_id, tracks[:mid], anchor, rejected)
            return done + self._add(playlist_id, tracks[mid:], anchor, rejected)

    def _check(self, part, pid, name, expected, attempted, out) -> bool:
        """After writing: does TIDAL hold what it accepted? True when it does."""
        lost_key = f"lost:{pid}"
        after = self.meta(pid) or {}
        if after.get("count") is not None and after["count"] != expected:
            self.client.sleep(10)  # the count can lag behind the last write
            after = self.meta(pid) or {}
        out["total"] = after.get("count")
        if after.get("count") is None or after["count"] == expected:
            if self.state.get(lost_key):
                self.state.set(lost_key, "")
            return True

        present = {it["track"] for it in self.read_items(pid)}
        lost = [t for t in dict.fromkeys(attempted) if t not in present]
        before = set(json.loads(self.state.get(lost_key) or "[]"))
        again = [t for t in lost if t in before]
        first = [t for t in lost if t not in before]
        self.state.set(lost_key, json.dumps(first))
        if again:
            self.state.mark_unaddable({t: "accepted by TIDAL but never added" for t in again})
            self.state.drop_managed(pid, again)
            out["warnings"].append(
                f"{name}: TIDAL accepted {len(again)} track(s) twice without adding them "
                f"(e.g. {', '.join(again[:3])}); they are skipped for 14 days")
        if first:
            out["warnings"].append(
                f"{name}: TIDAL accepted {len(first)} track(s) without adding them "
                f"(e.g. {', '.join(first[:3])}); the next run tries them once more")
        if not lost:
            out["warnings"].append(
                f"{name}: TIDAL reports {after['count']} items, expected {expected}; "
                "the next run re-reads the playlist")
        out["lost"] = lost
        return False

    def sync_part(self, part: int, desired: list[str], label=("", ""), n_artists=0,
                  force=False, rebuild=False, explained=frozenset()) -> dict:
        """explained: tracks whose removal is expected (they moved to another
        part, or their artist was unfollowed), so the removal limit ignores them."""
        name = self.cfg.part_name(part)
        out = {"part": part, "name": name, "desired": len(desired), "added": 0, "removed": 0,
               "status": "ok", "warnings": []}
        pid = self.ensure(part)
        out["playlist_id"] = pid
        if pid is None:  # dry run, nothing exists yet
            out.update(status="would create", added=len(desired))
            return out

        skip = self.state.unaddable()
        wanted_hash = desired_hash(desired, skip)
        info = self.meta(pid) or {}
        stamp = f"{info.get('count')}|{info.get('modified')}"
        row = self.state.get_part(part) or {}
        if not (force or rebuild) and row.get("desired_hash") == wanted_hash and row.get("remote_stamp") == stamp:
            out["status"] = "unchanged"
            return out

        current = self.read_items(pid)
        managed = self.state.managed(pid)
        remove, keep = plan_changes(current, desired, managed, self.cfg.prune_unmanaged, rebuild)
        limit = max(ADD_BATCH, int(len(current) * self.cfg.max_removal_fraction))
        partial = False
        unexplained = [it for it in remove if it["track"] not in explained]
        if unexplained and not (force or rebuild) and len(unexplained) > limit:
            out["warnings"].append(
                f"{name}: would remove {len(unexplained)} of {len(current)} tracks for no known reason, "
                f"more than the safety limit; those removals were skipped (run with --force if that is right)")
            remove = [it for it in remove if it["track"] in explained]
            going = {it["item"] for it in remove}
            keep = [it for it in current if it.get("item") not in going]
            partial = True

        runs = plan_runs(keep, desired, skip)
        room = HARD_CAP - len(keep)
        total = sum(len(r[1]) for r in runs)
        if total > room:
            out["warnings"].append(f"{name}: only room for {max(room, 0)} of {total} new tracks (10,000 cap)")
            trimmed, left = [], max(room, 0)
            for anchor, tracks in runs:
                if left <= 0:
                    break
                trimmed.append((anchor, tracks[:left]))
                left -= len(tracks[:left])
            runs = trimmed
            partial = True

        if self.dry_run:
            out.update(status="would change", added=sum(len(r[1]) for r in runs), removed=len(remove))
            return out

        for i in range(0, len(remove), REMOVE_BATCH):
            chunk = remove[i : i + REMOVE_BATCH]
            body = {"data": [{"id": it["track"], "type": "tracks", "meta": {"itemId": it["item"]}}
                             for it in chunk]}
            self.client.request("DELETE", f"/playlists/{pid}/relationships/items", body=body,
                                idempotency_key=str(uuid.uuid4()))
            self.state.drop_managed(pid, [it["track"] for it in chunk])
            out["removed"] += len(chunk)

        rejected: dict[str, str] = {}
        attempted: list[str] = []
        self._accepted = 0
        for anchor, tracks in runs:
            for i in range(0, len(tracks), ADD_BATCH):
                done = self._add(pid, tracks[i : i + ADD_BATCH], anchor, rejected)
                self.state.add_managed(pid, done)
                attempted += done
                out["added"] += len(done)
        if rejected:
            self.state.mark_unaddable(rejected)
            out["warnings"].append(
                f"{name}: TIDAL refused {len(rejected)} track(s); they are retried in 14 days")

        first, last = label
        span = f"{first} to {last}" if first and first != last else first
        description = (f"{span} · {n_artists} artists. " if span else "") + \
            "Managed by tidal-discography; tracks you add by hand are left alone."
        if (info.get("description") or "") != description[:500]:
            try:
                self.client.request("PATCH", f"/playlists/{pid}", body={"data": {
                    "id": pid, "type": "playlists", "attributes": {"description": description[:500]}}},
                    idempotency_key=str(uuid.uuid4()))
            except TidalError as exc:
                out["warnings"].append(f"{name}: could not update the description ({exc.status})")

        expected = len(keep) + out["added"]
        if not self._check(part, pid, name, expected, attempted, out) or partial:
            self.state.set_part(part, pid, None, None)
        else:
            after = self.meta(pid) or {}
            new_hash = desired_hash(desired, self.state.unaddable())
            self.state.set_part(part, pid, new_hash, f"{after.get('count')}|{after.get('modified')}")
        return out
