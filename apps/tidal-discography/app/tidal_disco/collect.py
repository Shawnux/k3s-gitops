"""Read side: followed artists, each artist's releases, each release's tracks.

Artists are followed by ID, and releases come from the artist's own albums
relationship, so nothing here depends on name search.
"""

from __future__ import annotations

import logging
import time
import zlib
from dataclasses import dataclass

from .api import TidalError

log = logging.getLogger("tidal_disco.collect")

BATCH = 20  # filter[id] takes at most 20 ids


class Incomplete(Exception):
    """The time budget ran out; cached work is kept and the next run resumes."""


@dataclass(frozen=True)
class Artist:
    id: str
    name: str


def index_included(doc: dict) -> dict:
    return {(r.get("type"), r.get("id")): r for r in doc.get("included") or []}


def _rel(resource: dict, name: str) -> dict:
    return (resource.get("relationships") or {}).get(name) or {}


def _has_more(rel: dict) -> bool:
    links = rel.get("links") or {}
    return bool(links.get("next") or (links.get("meta") or {}).get("nextCursor"))


def followed_artists(client) -> list[Artist]:
    found: dict[str, Artist] = {}
    for doc in client.paged("/userCollectionArtists/me/relationships/items", {"include": "items"}):
        inc = index_included(doc)
        for ident in doc.get("data") or []:
            if ident.get("type") != "artists":
                continue
            aid = str(ident["id"])
            name = ((inc.get(("artists", aid)) or {}).get("attributes") or {}).get("name")
            found[aid] = Artist(aid, name or "")
    nameless = [a.id for a in found.values() if not a.name]
    for i in range(0, len(nameless), BATCH):
        chunk = nameless[i : i + BATCH]
        try:
            doc = client.get("/artists", {"countryCode": client.country, "filter[id]": chunk})
        except TidalError:
            break
        for res in doc.get("data") or []:
            name = (res.get("attributes") or {}).get("name")
            if name:
                found[str(res["id"])] = Artist(str(res["id"]), name)
    return [a if a.name else Artist(a.id, f"Artist {a.id}") for a in found.values()]


def _album_base(res: dict, inc: dict) -> dict:
    a = res.get("attributes") or {}
    art = _rel(res, "artists").get("data")
    artist_ids = [str(x["id"]) for x in art] if art is not None else None
    names = []
    for aid in artist_ids or []:
        names.append(((inc.get(("artists", aid)) or {}).get("attributes") or {}).get("name") or "")
    return {
        "id": str(res["id"]),
        "title": a.get("title") or "",
        "version": a.get("version") or "",
        "type": a.get("albumType") or a.get("type") or "",
        "date": a.get("releaseDate") or "",
        "explicit": bool(a.get("explicit")),
        "barcode": a.get("barcodeId") or "",
        "n_items": a.get("numberOfItems"),
        "popularity": a.get("popularity") or 0,
        "media_tags": a.get("mediaTags") or [],
        "artist_ids": artist_ids,
        "artist_names": names,
        "tracks": [],
        "n_videos": 0,
        "complete": False,
        "mismatch": False,
    }


def _tracks(idents: list, inc: dict):
    """Tracks for a list of item identifiers. Returns (tracks, videos, unresolved)."""
    tracks, videos, unresolved = [], 0, 0
    for pos, ident in enumerate(idents):
        if ident.get("type") != "tracks":
            videos += 1
            continue
        tid = str(ident["id"])
        res = inc.get(("tracks", tid))
        if res is None:
            unresolved += 1
            continue
        a = res.get("attributes") or {}
        art = _rel(res, "artists").get("data")
        meta = ident.get("meta") or {}
        tracks.append(
            {
                "id": tid,
                "title": a.get("title") or "",
                "version": a.get("version") or "",
                "isrc": a.get("isrc") or "",
                "explicit": bool(a.get("explicit")),
                "vol": meta.get("volumeNumber") or 1,
                "num": meta.get("trackNumber") or pos + 1,
                "artist_ids": [str(x["id"]) for x in art] if art is not None else None,
            }
        )
    tracks.sort(key=lambda t: (t["vol"], t["num"]))
    return tracks, videos, unresolved


class Collector:
    def __init__(self, client, state, cfg, deadline: float | None = None):
        self.client = client
        self.state = state
        self.cfg = cfg
        self.deadline = deadline
        self.batch_supported = True
        self.track_lookup = True
        self.fetched = 0
        self.gone = 0

    def _check_deadline(self) -> None:
        if self.deadline and time.time() > self.deadline:
            raise Incomplete()

    # listing ---------------------------------------------------------------
    def list_albums(self, artist_id: str) -> dict[str, dict]:
        """Every release TIDAL lists for the artist: album id -> attributes."""
        out: dict[str, dict] = {}
        params = {"countryCode": self.client.country, "include": "albums"}
        for doc in self.client.paged(f"/artists/{artist_id}/relationships/albums", params):
            inc = index_included(doc)
            for ident in doc.get("data") or []:
                if ident.get("type") != "albums":
                    continue
                aid = str(ident["id"])
                out[aid] = (inc.get(("albums", aid)) or {}).get("attributes") or {}
        return out

    # album details ---------------------------------------------------------
    def _batch(self, ids: list[str]) -> dict[str, dict]:
        params = {
            "countryCode": self.client.country,
            "filter[id]": ids,
            "include": "artists,items,items.artists",
        }
        out = {}
        for doc in self.client.paged("/albums", params):
            inc = index_included(doc)
            for res in doc.get("data") or []:
                rec = _album_base(res, inc)
                items = _rel(res, "items")
                idents = items.get("data")
                if idents is not None:
                    rec["tracks"], rec["n_videos"], unresolved = _tracks(idents, inc)
                    rec["complete"] = (
                        not _has_more(items)
                        and not unresolved
                        and rec["n_items"] in (None, len(idents))
                    )
                out[rec["id"]] = rec
        return out

    def _single(self, album_id: str, partial: dict | None) -> dict | None:
        """One album fetched the long way: its own record, then every page of items."""
        cc = self.client.country
        rec = partial
        try:
            if rec is None or rec["artist_ids"] is None:
                doc = self.client.get(f"/albums/{album_id}", {"countryCode": cc, "include": "artists"})
                if not doc.get("data"):
                    return None
                rec = _album_base(doc["data"], index_included(doc))
            idents, inc = [], {}
            params = {"countryCode": cc, "include": "items,items.artists"}
            for doc in self.client.paged(f"/albums/{album_id}/relationships/items", params):
                idents += doc.get("data") or []
                inc.update(index_included(doc))
        except TidalError as exc:
            if exc.status == 404:
                self.gone += 1
                return None
            raise
        rec["tracks"], rec["n_videos"], unresolved = _tracks(idents, inc)
        rec["complete"] = True
        rec["mismatch"] = bool(unresolved) or rec["n_items"] not in (None, len(idents))
        if rec["mismatch"]:
            log.warning(
                "album %s (%s): TIDAL says %s items, got %d (%d unreadable)",
                album_id, rec["title"], rec["n_items"], len(idents), unresolved,
            )
        return rec

    def _fill_credits(self, rec: dict) -> None:
        """Track credits decide what is a compilation, so when an album came back
        without them, ask for the tracks' artists directly."""
        missing = {t["id"]: t for t in rec["tracks"] if t.get("artist_ids") is None}
        if not missing or not self.track_lookup or len(rec["tracks"]) < self.cfg.compilation_min_tracks:
            return
        ids = list(missing)
        for i in range(0, len(ids), BATCH):
            params = {"countryCode": self.client.country, "filter[id]": ids[i : i + BATCH], "include": "artists"}
            try:
                doc = self.client.get("/tracks", params)
            except TidalError as exc:
                if exc.status not in (400, 404, 405, 422):
                    raise
                self.track_lookup = False
                log.warning("track credit lookup refused (%s); some releases cannot be credit-checked", exc.status)
                return
            for res in doc.get("data") or []:
                art = _rel(res, "artists").get("data")
                if art is not None and str(res.get("id")) in missing:
                    missing[str(res["id"])]["artist_ids"] = [str(x["id"]) for x in art]

    def fetch_albums(self, ids: list[str]) -> dict[str, dict]:
        out = {}
        for i in range(0, len(ids), BATCH):
            self._check_deadline()
            chunk = ids[i : i + BATCH]
            got: dict[str, dict] = {}
            if self.batch_supported:
                try:
                    got = self._batch(chunk)
                except TidalError as exc:
                    if exc.status not in (400, 404, 405, 422):
                        raise
                    self.batch_supported = False
                    log.warning("batched album lookup refused (%s); fetching one by one", exc.status)
            for aid in chunk:
                rec = got.get(aid)
                if rec is None or not rec["complete"]:
                    self._check_deadline()
                    rec = self._single(aid, rec)
                if rec is not None:
                    self._fill_credits(rec)
                    self.state.put_album(aid, rec)
                    self.fetched += 1
                    out[aid] = rec
        return out

    def _fresh(self, cached, attrs: dict) -> bool:
        rec, fetched_at = cached
        if not rec.get("complete"):
            return False
        listed = attrs.get("numberOfItems")
        if listed is not None and rec.get("n_items") is not None and listed != rec["n_items"]:
            return False
        # spread re-checks out so they do not all land on one night
        jitter = 0.75 + (zlib.crc32(rec["id"].encode()) % 1000) / 2000
        return time.time() - fetched_at < self.cfg.album_refresh_days * 86400 * jitter

    # one artist ------------------------------------------------------------
    def collect_artist(self, artist: Artist) -> list[dict]:
        self._check_deadline()
        listing = self.list_albums(artist.id)
        wanted = set(self.cfg.types)
        recs, need = {}, []
        for aid, attrs in listing.items():
            kind = attrs.get("albumType") or attrs.get("type")
            if kind and kind not in wanted and aid not in self.cfg.force_include_album_ids:
                recs[aid] = {
                    "id": aid, "title": attrs.get("title") or "", "version": attrs.get("version") or "",
                    "type": kind, "date": attrs.get("releaseDate") or "", "tracks": [],
                    "artist_ids": None, "artist_names": [], "explicit": bool(attrs.get("explicit")),
                    "popularity": 0, "media_tags": [], "stub": True,
                }
                continue
            cached = self.state.get_album(aid)
            if cached and self._fresh(cached, attrs):
                recs[aid] = cached[0]
            else:
                need.append(aid)
        recs.update(self.fetch_albums(need))
        self.state.set_artist_result(artist.id, artist.name, list(listing))
        return [recs[a] for a in listing if a in recs]

    def cached_artist(self, artist: Artist) -> list[dict] | None:
        """Last good result for an artist, for when TIDAL fails on them this run."""
        ids = self.state.get_artist_result(artist.id)
        if ids is None:
            return None
        out = []
        for aid in ids:
            cached = self.state.get_album(aid)
            if cached:
                out.append(cached[0])
        return out
