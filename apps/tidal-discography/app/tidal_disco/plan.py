"""Pure logic: which releases count, in what order, and which playlist part they go in."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

TYPE_RANK = {"ALBUM": 0, "EP": 1, "SINGLE": 2}
DJ_MIX_TITLE = re.compile(r"\b(dj[\s-]?mix|continuous (dj )?mix|mixed by)\b|\(mixed\)", re.I)
MIXED_TRACK = re.compile(r"^mixed$|\(mixed\)\s*$|\bcontinuous mix\b", re.I)
COLLECTION_PHRASE = re.compile(
    r"\b(compiled|selected|curated|hosted|presented) by\b|\byear\s?mix\b|\bradio\s?show\b"
    r"|\bpodcast\b|\bepisode\s*#?\d{2,5}\b",
    re.I,
)
VARIOUS = {"various artists", "various", "varios artistas", "verschiedene interpreten"}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").casefold()
    return re.sub(r"[\W_]+", "", text)


def simplify(text: str) -> str:
    """Lower-case words separated by single spaces; apostrophes dropped."""
    text = unicodedata.normalize("NFKD", text or "").casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"['’`]", "", text)
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


def base_pattern(title: str) -> str:
    """A title with brackets, numbers and punctuation removed, so the episodes
    of one show all look the same. (Same idea as the old tidal-sync.)"""
    t = (title or "").lower()
    t = re.sub(r"[\(\[].*?[\)\]]", "", t)
    t = re.sub(r"\d+", "", t)
    t = re.sub(r"[^\w\s]", "", t)
    return " ".join(t.split())


def full_title(title: str, version: str | None) -> str:
    """Title with its version added, unless the title already contains it
    (TIDAL often has "Song (X Remix)" with version "X Remix")."""
    title, version = title or "", (version or "").strip()
    if not version or norm(version) in norm(title):
        return title
    return f"{title} ({version})"


def sort_name(name: str) -> str:
    n = unicodedata.normalize("NFKD", name or "").casefold().strip()
    n = "".join(c for c in n if not unicodedata.combining(c))
    return re.sub(r"^(the|a)\s+", "", n)


@dataclass
class Block:
    artist_id: str
    artist_name: str
    track_ids: list[str] = field(default_factory=list)
    releases: list[dict] = field(default_factory=list)


@dataclass
class Plan:
    blocks: list[Block] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    unchecked: list[dict] = field(default_factory=list)  # kept, but TIDAL gave no track credits
    repeat_recordings: int = 0

    @property
    def n_tracks(self) -> int:
        return sum(len(b.track_ids) for b in self.blocks)

    @property
    def n_releases(self) -> int:
        return sum(len(b.releases) for b in self.blocks)


def own_share(album: dict, artist):
    """(tracks that are the artist's, tracks we could judge). A track is the
    artist's when they are credited on it or named in its title, as a remixer is."""
    name = norm(artist.name)
    own = known = 0
    for t in album["tracks"]:
        ids = t.get("artist_ids")
        named = len(name) >= 4 and name in norm(f"{t['title']} {t.get('version') or ''}")
        if ids is None and not named:
            continue
        known += 1
        if named or artist.id in ids:
            own += 1
    return own, known


def collection_label(title: str, names: list[str]) -> str | None:
    hit = COLLECTION_PHRASE.search(title)
    if hit:
        return hit.group(0).lower()
    padded = f" {simplify(title)} "
    for name in names:
        if f" {name} " in padded:
            return name
    return None


def classify(album: dict, artist, cfg, title_res, names=(), series=frozenset()) -> str | None:
    """Why a release is left out, or None when it is the artist's own release."""
    aid = album["id"]
    if aid in cfg.force_exclude_album_ids:
        return "excluded in config"
    if aid in cfg.force_include_album_ids:
        return None if album["tracks"] else "no playable tracks"
    if album["type"] and album["type"] not in cfg.types:
        return f"type {album['type']} not wanted"
    tracks = album["tracks"]
    if not tracks:
        return "no playable tracks"
    if album.get("artist_ids") is not None and artist.id not in album["artist_ids"]:
        return "appears on (not a main artist)"
    if any((n or "").casefold().strip() in VARIOUS for n in album.get("artist_names") or []):
        return "various-artists compilation"
    whole = full_title(album["title"], album.get("version"))
    for rx in title_res:
        if rx.search(whole):
            return f"title matches /{rx.pattern}/"
    big = len(tracks) >= cfg.compilation_min_tracks
    if cfg.exclude_dj_mixes:
        if DJ_MIX_TITLE.search(whole):
            return "DJ mix"
        mixed = sum(
            1 for t in tracks
            if MIXED_TRACK.search(t["title"]) or MIXED_TRACK.search(t.get("version") or "")
        )
        if big and mixed * 2 >= len(tracks):
            return "DJ mix"
    if not big:
        return None
    own, known = own_share(album, artist)
    share = own / known if known else None
    if share is not None and share < cfg.min_own_track_share:
        return f"compilation ({own} of {known} tracks are the artist's)"
    if cfg.exclude_collections:
        if base_pattern(album["title"]) in series:
            return "series (many releases share this title)"
        if share is None or share < cfg.collection_own_share:
            label = collection_label(whole, names)
            if label:
                return f"collection ({label})"
    return None


def _edition_rank(album: dict, prefer_explicit: bool):
    return (
        0 if album.get("explicit") == prefer_explicit else 1,
        0 if "HIRES_LOSSLESS" in (album.get("media_tags") or []) else 1,
        -(album.get("popularity") or 0),
        album.get("date") or "9999",
        album["id"],
    )


def dedupe_editions(albums: list[dict], prefer_explicit: bool):
    """Collapse copies of one release: clean/explicit pairs and re-deliveries
    under a new id. Two releases are copies when they have the same name, type
    and tracklist, or when they carry exactly the same recordings (ISRCs) in
    the same order, however TIDAL wrote the name. Anything else stays."""
    parent = list(range(len(albums)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first_with: dict[tuple, int] = {}
    for i, al in enumerate(albums):
        keys = [(
            "name", norm(full_title(al["title"], al.get("version"))), al["type"],
            tuple(norm(full_title(t["title"], t.get("version"))) for t in al["tracks"]),
        )]
        isrcs = tuple(t.get("isrc") or "" for t in al["tracks"])
        if isrcs and all(isrcs):
            keys.append(("isrc", isrcs))
        for key in keys:
            if key in first_with:
                parent[find(i)] = find(first_with[key])
            else:
                first_with[key] = i

    groups: dict[int, list[dict]] = {}
    for i, al in enumerate(albums):
        groups.setdefault(find(i), []).append(al)
    kept, dropped = [], []
    for group in groups.values():
        best = min(group, key=lambda a: _edition_rank(a, prefer_explicit))
        first = min((a.get("date") or "9999-99-99") for a in group)
        kept.append((best, first))
        for other in group:
            if other is not best:
                dropped.append((other, f"duplicate edition of {best['id']}"))
    return kept, dropped


def build_plan(artists, albums_by_artist: dict, cfg) -> Plan:
    """artists: iterable of objects with .id and .name. Returns blocks in playlist order."""
    title_res = [re.compile(p, re.I) for p in cfg.exclude_title_regex]
    names = [n for n in dict.fromkeys(simplify(x) for x in cfg.collection_names + cfg.extra_collection_names) if n]
    plan = Plan()
    seen_albums: set[str] = set()
    seen_tracks: set[str] = set()
    seen_isrc: set[str] = set()

    for artist in sorted(artists, key=lambda a: (sort_name(a.name), a.id)):
        if artist.id not in albums_by_artist:
            continue
        listed = albums_by_artist[artist.id]
        counts = Counter(
            base_pattern(al["title"]) for al in listed if len(al["tracks"]) >= cfg.compilation_min_tracks
        )
        series = {b for b, c in counts.items() if c >= cfg.series_threshold and len(b) > 2}
        candidates = []
        for album in listed:
            if album["id"] in seen_albums:
                continue  # co-headlined release already placed under the other artist
            reason = classify(album, artist, cfg, title_res, names, series)
            if reason:
                plan.excluded.append(_excluded(artist, album, reason))
            else:
                candidates.append(album)
                if len(album["tracks"]) >= cfg.compilation_min_tracks and not own_share(album, artist)[1]:
                    plan.unchecked.append(_excluded(artist, album, "kept without a credit check"))

        if cfg.dedupe == "none":
            kept = [(al, al.get("date") or "9999-99-99") for al in candidates]
        else:
            kept, dropped = dedupe_editions(candidates, cfg.prefer_explicit)
            for album, reason in dropped:
                plan.excluded.append(_excluded(artist, album, reason))
        kept.sort(key=lambda pair: (pair[1], TYPE_RANK.get(pair[0]["type"], 9), norm(pair[0]["title"]), pair[0]["id"]))

        # recordings: a track on an album keeps its album copy, so albums play
        # complete; otherwise the first release that carried it keeps it
        winners: dict[str, tuple] = {}
        if cfg.dedupe == "recordings":
            for idx, (album, date) in enumerate(kept):
                for t in album["tracks"]:
                    if t.get("isrc"):
                        score = (0 if album["type"] == "ALBUM" else 1, date, idx)
                        if t["isrc"] not in winners or score < winners[t["isrc"]][0]:
                            winners[t["isrc"]] = (score, t["id"])

        block = Block(artist.id, artist.name)
        for album, date in kept:
            seen_albums.add(album["id"])
            added = 0
            for t in album["tracks"]:
                if t["id"] in seen_tracks:
                    continue
                isrc = t.get("isrc")
                if isrc and isrc in seen_isrc:
                    if cfg.dedupe == "recordings":
                        continue
                    plan.repeat_recordings += 1
                elif isrc and cfg.dedupe == "recordings" and winners[isrc][1] != t["id"]:
                    continue
                seen_tracks.add(t["id"])
                if isrc:
                    seen_isrc.add(isrc)
                block.track_ids.append(t["id"])
                added += 1
            block.releases.append(
                {"album_id": album["id"], "title": full_title(album["title"], album.get("version")),
                 "type": album["type"], "date": date if date != "9999-99-99" else "", "tracks": added,
                 "of": len(album["tracks"])}
            )
        plan.blocks.append(block)
    return plan


def _excluded(artist, album: dict, reason: str) -> dict:
    return {
        "artist": artist.name, "artist_id": artist.id, "album_id": album["id"],
        "title": full_title(album["title"], album.get("version")), "version": "",
        "type": album["type"], "date": album.get("date") or "", "reason": reason,
    }


def pack(blocks: list[Block], prev: dict[str, int], target: int, cap: int):
    """Assign artist blocks to numbered parts.

    An artist stays in the part it was first given, so new releases land next to
    the old ones. A newly followed artist goes to the nearest part with room to
    its alphabetical neighbours. A part that outgrows the cap hands its last
    artist to another part.
    Returns ({part: [track ids]}, {part: (first, last artist)}, {block key: part}).
    """
    pieces = []  # (order, key, name, tracks)
    for block in blocks:
        ids = block.track_ids
        if not ids:
            continue
        chunks = [ids[i : i + target] for i in range(0, len(ids), target)]
        for n, chunk in enumerate(chunks):
            key = block.artist_id if n == 0 else f"{block.artist_id}#{n + 1}"
            pieces.append((len(pieces), key, block.artist_name, chunk))

    parts: dict[int, list] = {}
    unassigned = []
    first_run = not any(p[1] in prev for p in pieces)
    for piece in pieces:
        k = prev.get(piece[1])
        if k is None:
            unassigned.append(piece)
        else:
            parts.setdefault(k, []).append(piece)

    def load(k):
        return sum(len(p[3]) for p in parts.get(k, []))

    for k in sorted(parts):
        while load(k) > cap and len(parts[k]) > 1:
            unassigned.append(parts[k].pop())
    unassigned.sort(key=lambda p: p[0])

    def neighbour_part(order):
        placed = [(p[0], k) for k, members in parts.items() for p in members]
        before = [x for x in placed if x[0] < order]
        if before:
            return max(before)[1]
        return min(placed)[1] if placed else 1

    for piece in unassigned:
        size = len(piece[3])
        existing = sorted(parts)
        if first_run:
            choices = existing[-1:]
        else:
            home = neighbour_part(piece[0])
            choices = sorted(existing, key=lambda k: (abs(k - home), k))
        dest = next((k for k in choices if load(k) + size <= target), None)
        if dest is None:
            dest = (existing[-1] + 1) if existing else 1
        parts.setdefault(dest, []).append(piece)

    top = max(list(parts) + list(prev.values()) + [0])
    tracks, labels, mapping = {}, {}, {}
    for k in range(1, top + 1):
        members = sorted(parts.get(k, []), key=lambda p: p[0])
        tracks[k] = [t for p in members for t in p[3]]
        labels[k] = (members[0][2], members[-1][2]) if members else ("", "")
        for p in members:
            mapping[p[1]] = k
    return tracks, labels, mapping
