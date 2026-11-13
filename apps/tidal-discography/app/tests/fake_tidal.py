"""A small stand-in for TIDAL's v2 API and token endpoint, shaped after API
reference v1.10.245: JSON:API documents, 20 items a page, cursor paging,
identifiers in `data` with the resources in `included`."""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

PAGE = 20


class FakeTidal:
    def __init__(self):
        self.artists: dict[str, str] = {}
        self.albums: dict[str, dict] = {}
        self.tracks: dict[str, dict] = {}
        self.artist_albums: dict[str, list[str]] = {}
        self.followed: list[str] = []
        self.playlists: dict[str, dict] = {}
        self.refresh_token = "rt-0"
        self.access_token = None
        self.clock = 0
        self.calls: list[tuple[str, str]] = []
        # switches for failure scenarios
        self.batch_disabled = False
        self.rate_limit_every = 0
        self.fail_artists: set[str] = set()
        self.bad_tracks: set[str] = set()
        self.cap = 10000
        self.silent_drop: set[str] = set()   # accepted with 201, never added
        self.count_lag = 0                   # numberOfItems lags this far behind one write
        self.gone_artists: set[str] = set()  # followed, but TIDAL answers 404
        self.add_bodies: list[dict] = []
        self.reject_all_adds = False
        self.no_item_credits = False   # album responses carry no track artists
        self.no_track_lookup = False   # and /tracks?filter[id] is refused too
        self._n = 0
        self._next_id = 1000

    # catalog builders ------------------------------------------------------
    def artist(self, aid, name, follow=True):
        self.artists[aid] = name
        self.artist_albums.setdefault(aid, [])
        if follow:
            self.followed.append(aid)
        return aid

    def album(self, album_id, title, artists, tracks, kind="ALBUM", date="2020-01-01",
              explicit=False, version=None, listed_under=None, n_items=None, tags=()):
        """tracks: list of (title, [artist ids]) or (title, [artist ids], isrc) or dicts."""
        idents = []
        for n, spec in enumerate(tracks, 1):
            if isinstance(spec, str):  # an existing track id
                idents.append({"id": spec, "type": "tracks", "num": n})
                continue
            title_, credits = spec[0], spec[1]
            self._next_id += 1
            tid = f"t{self._next_id}"
            self.tracks[tid] = {
                "title": title_, "artists": list(credits),
                "isrc": spec[2] if len(spec) > 2 else f"ISRC{tid}",
                "version": spec[3] if len(spec) > 3 else None, "explicit": explicit,
            }
            idents.append({"id": tid, "type": "tracks", "num": n})
        self.albums[album_id] = {
            "title": title, "artists": list(artists), "items": idents, "type": kind, "date": date,
            "explicit": explicit, "version": version,
            "n_items": len(idents) if n_items is None else n_items, "tags": list(tags),
        }
        for aid in listed_under if listed_under is not None else artists:
            self.artist_albums.setdefault(aid, []).append(album_id)
        return [i["id"] for i in idents]

    def playlist_tracks(self, name):
        for p in self.playlists.values():
            if p["name"] == name:
                return [i["track"] for i in p["items"]]
        return None

    # resources -------------------------------------------------------------
    def _artist_res(self, aid):
        return {"id": aid, "type": "artists", "attributes": {"name": self.artists.get(aid, "?")}}

    def _track_res(self, tid, with_artists):
        t = self.tracks[tid]
        res = {"id": tid, "type": "tracks", "attributes": {
            "title": t["title"], "isrc": t["isrc"], "explicit": t["explicit"],
            "duration": "PT3M", "mediaTags": [], "popularity": 0.5}}
        if t["version"]:
            res["attributes"]["version"] = t["version"]
        if with_artists:
            res["relationships"] = {"artists": {"data": [{"id": a, "type": "artists"} for a in t["artists"]]}}
        return res

    def _album_attrs(self, album_id):
        al = self.albums[album_id]
        attrs = {"title": al["title"], "albumType": al["type"], "releaseDate": al["date"],
                 "explicit": al["explicit"], "numberOfItems": al["n_items"], "barcodeId": f"upc{album_id}",
                 "mediaTags": al["tags"], "popularity": 0.5, "duration": "PT40M", "numberOfVolumes": 1}
        if al["version"]:
            attrs["version"] = al["version"]
        return attrs

    def _item_idents(self, album_id):
        return [{"id": i["id"], "type": i["type"], "meta": {"trackNumber": i["num"], "volumeNumber": 1}}
                for i in self.albums[album_id]["items"]]

    def _playlist_res(self, pid):
        p = self.playlists[pid]
        lag, p["lag"] = p.get("lag", 0), 0
        return {"id": pid, "type": "playlists", "attributes": {
            "name": p["name"], "description": p["description"], "accessType": p["access"],
            "numberOfItems": len(p["items"]) - lag, "lastModifiedAt": f"2026-11-11T00:00:{p['modified']:06d}Z",
            "playlistType": "USER"}}

    def _touch(self, pid):
        self.clock += 1
        self.playlists[pid]["modified"] = self.clock

    # request handling ------------------------------------------------------
    def handle(self, method, raw_path, headers, body):
        parts = urlsplit(raw_path)
        path, q = parts.path, parse_qs(parts.query)
        self.calls.append((method, path))
        if path == "/token":
            form = parse_qs(body.decode())
            if form.get("grant_type") == ["authorization_code"]:
                if form.get("code") != ["good-code"] or not form.get("code_verifier"):
                    return 400, {"error": "invalid_grant"}
            elif form.get("refresh_token") != [self.refresh_token]:
                return 400, {"error": "invalid_grant"}
            n = int(self.refresh_token.split("-")[1]) + 1
            self.refresh_token = f"rt-{n}"
            self.access_token = f"at-{n}"
            return 200, {"access_token": self.access_token, "refresh_token": self.refresh_token,
                         "expires_in": 86400, "scope": "collection.read playlists.read playlists.write"}
        if headers.get("Authorization") != f"Bearer {self.access_token}":
            return 401, {"errors": [{"code": "UNAUTHORIZED"}]}
        self._n += 1
        if self.rate_limit_every and self._n % self.rate_limit_every == 0:
            return 429, {"errors": [{"code": "TOO_MANY_REQUESTS"}]}
        if not path.startswith("/v2/"):
            return 404, {"errors": [{"code": "NOT_FOUND"}]}
        seg = path[4:].split("/")
        include = set(",".join(q.get("include", [])).split(",")) - {""}
        start = int(q.get("page[cursor]", ["0"])[0])
        payload = json.loads(body) if body else None

        def page(idents, style="next"):
            chunk = idents[start:start + PAGE]
            links = {"self": raw_path}
            if start + PAGE < len(idents):
                keep = [(k, v) for k, vs in q.items() if k != "page[cursor]" for v in vs]
                query = "&".join(f"{quote(k)}={quote(v)}" for k, v in keep + [("page[cursor]", str(start + PAGE))])
                if style == "next":
                    links["next"] = f"{path}?{query}"
                else:
                    links["meta"] = {"nextCursor": str(start + PAGE)}
            return chunk, links

        # followed artists
        if seg == ["userCollectionArtists", "me", "relationships", "items"] and method == "GET":
            idents, links = page([{"id": a, "type": "artists", "meta": {"addedAt": "2026-01-01T00:00:00Z"}}
                                  for a in self.followed])
            doc = {"data": idents, "links": links}
            if "items" in include:
                doc["included"] = [self._artist_res(i["id"]) for i in idents]
            return 200, doc

        if seg[0] == "artists" and len(seg) == 4 and seg[2:] == ["relationships", "albums"]:
            if seg[1] in self.fail_artists:
                return 500, {"errors": [{"code": "INTERNAL"}]}
            if seg[1] in self.gone_artists:
                return 404, {"errors": [{"code": "NOT_FOUND"}]}
            idents, links = page([{"id": a, "type": "albums"} for a in self.artist_albums.get(seg[1], [])],
                                 style="meta")
            doc = {"data": idents, "links": links}
            if "albums" in include:
                doc["included"] = [{"id": i["id"], "type": "albums", "attributes": self._album_attrs(i["id"])}
                                   for i in idents]
            return 200, doc

        if seg == ["albums"] and method == "GET":
            ids = q.get("filter[id]", [])
            if len(ids) > 20 or (self.batch_disabled and ids):
                return 400, {"errors": [{"code": "INVALID_FILTER"}]}
            data, included = [], {}
            for album_id in ids:
                if album_id in self.albums:
                    data.append(self._album_doc(album_id, include, included))
            return 200, {"data": data, "included": list(included.values()), "links": {"self": raw_path}}

        if seg[0] == "albums" and len(seg) == 2 and method == "GET":
            if seg[1] not in self.albums:
                return 404, {"errors": [{"code": "NOT_FOUND"}]}
            included = {}
            return 200, {"data": self._album_doc(seg[1], include, included),
                         "included": list(included.values()), "links": {"self": raw_path}}

        if seg[0] == "albums" and len(seg) == 4 and seg[2:] == ["relationships", "items"]:
            if seg[1] not in self.albums:
                return 404, {"errors": [{"code": "NOT_FOUND"}]}
            idents, links = page(self._item_idents(seg[1]))
            included = {}
            if "items" in include:
                for i in idents:
                    self._include_track(i["id"], "items.artists" in include, included)
            return 200, {"data": idents, "included": list(included.values()), "links": links}

        if seg == ["tracks"] and method == "GET":
            ids = q.get("filter[id]", [])
            if self.no_track_lookup or len(ids) > 20:
                return 400, {"errors": [{"code": "INVALID_FILTER"}]}
            included = {}
            for tid in ids:
                if tid in self.tracks:
                    included[("tracks", tid)] = self._track_res(tid, "artists" in include)
            return 200, {"data": list(included.values()), "links": {"self": raw_path}}

        if seg == ["playlists"] and method == "GET":
            if q.get("filter[owners.id]") != ["me"]:
                return 400, {"errors": [{"code": "INVALID_FILTER"}]}
            ids, links = page(list(self.playlists))
            return 200, {"data": [self._playlist_res(p) for p in ids], "links": links}

        if seg == ["playlists"] and method == "POST":
            attrs = payload["data"]["attributes"]
            assert payload["data"]["type"] == "playlists" and 1 <= len(attrs["name"]) <= 250
            assert attrs.get("accessType", "UNLISTED") in ("PUBLIC", "UNLISTED")
            pid = str(uuid.uuid4())
            self.playlists[pid] = {"name": attrs["name"], "description": attrs.get("description", ""),
                                   "access": attrs.get("accessType", "UNLISTED"), "items": [], "modified": 0}
            self._touch(pid)
            return 201, {"data": self._playlist_res(pid)}

        if seg[0] == "playlists" and len(seg) == 2:
            pid = seg[1]
            if pid not in self.playlists:
                return 404, {"errors": [{"code": "NOT_FOUND"}]}
            if method == "GET":
                return 200, {"data": self._playlist_res(pid)}
            if method == "PATCH":
                attrs = payload["data"]["attributes"]
                assert len(attrs.get("description", "")) <= 500
                self.playlists[pid].update({k: v for k, v in attrs.items() if k in ("name", "description")})
                self._touch(pid)
                return 204, None

        if seg[0] == "playlists" and len(seg) == 4 and seg[2:] == ["relationships", "items"]:
            pid = seg[1]
            if pid not in self.playlists:
                return 404, {"errors": [{"code": "NOT_FOUND"}]}
            items = self.playlists[pid]["items"]
            if method == "GET":
                idents, links = page([{"id": i["track"], "type": "tracks",
                                       "meta": {"itemId": i["item"], "addedAt": "2026-11-11T00:00:00Z"}}
                                      for i in items])
                return 200, {"data": idents, "links": links}
            if method == "POST":
                data, meta = payload["data"], payload.get("meta") or {}
                if not 1 <= len(data) <= 50 or self.reject_all_adds:
                    return 400, {"errors": [{"code": "INVALID_REQUEST_BODY"}]}
                self.add_bodies.append(payload)
                if any(d["id"] in self.bad_tracks or d["id"] not in self.tracks for d in data):
                    return 404, {"errors": [{"code": "NOT_FOUND", "detail": "track"}]}
                assert all(d["type"] == "tracks" for d in data)
                have = {i["track"] for i in items}
                new = []
                for d in data:
                    if meta.get("onDuplicates") == "SKIP" and d["id"] in have:
                        continue
                    new.append({"track": d["id"], "item": str(uuid.uuid4())})
                new = [n for n in new if n["track"] not in self.silent_drop]  # accepted, never added
                new = new[: max(self.cap - len(items), 0)]  # past the cap: accepted, silently dropped
                self.playlists[pid]["lag"] = min(self.count_lag, len(new))
                before = meta.get("positionBefore")
                if before:
                    index = next((n for n, i in enumerate(items) if i["item"] == before), None)
                    if index is None:
                        return 404, {"errors": [{"code": "NOT_FOUND", "detail": "positionBefore"}]}
                    items[index:index] = new
                else:
                    items.extend(new)
                self._touch(pid)
                return 201, None
            if method == "DELETE":
                data = payload["data"]
                if not 1 <= len(data) <= 50:
                    return 400, {"errors": [{"code": "INVALID_REQUEST_BODY"}]}
                gone = {d["meta"]["itemId"] for d in data}
                self.playlists[pid]["items"] = [i for i in items if i["item"] not in gone]
                self._touch(pid)
                return 204, None

        return 404, {"errors": [{"code": "NOT_FOUND", "detail": path}]}

    def _include_track(self, tid, with_artists, included):
        with_artists = with_artists and not self.no_item_credits
        included[("tracks", tid)] = self._track_res(tid, with_artists)
        if with_artists:
            for a in self.tracks[tid]["artists"]:
                included[("artists", a)] = self._artist_res(a)

    def _album_doc(self, album_id, include, included):
        al = self.albums[album_id]
        res = {"id": album_id, "type": "albums", "attributes": self._album_attrs(album_id),
               "relationships": {"artists": {"links": {"self": f"/albums/{album_id}/relationships/artists"}},
                                 "items": {"links": {"self": f"/albums/{album_id}/relationships/items"}}}}
        if "artists" in include:
            res["relationships"]["artists"]["data"] = [{"id": a, "type": "artists"} for a in al["artists"]]
            for a in al["artists"]:
                included[("artists", a)] = self._artist_res(a)
        if "items" in include:
            idents = self._item_idents(album_id)
            res["relationships"]["items"]["data"] = idents[:PAGE]
            if len(idents) > PAGE:
                res["relationships"]["items"]["links"]["next"] = (
                    f"/albums/{album_id}/relationships/items?page%5Bcursor%5D={PAGE}")
            for i in idents[:PAGE]:
                self._include_track(i["id"], "items.artists" in include, included)
        return res

    # server ----------------------------------------------------------------
    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _go(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                try:
                    status, doc = fake.handle(self.command, self.path, self.headers, body)
                except AssertionError as exc:
                    status, doc = 400, {"errors": [{"code": "ASSERT", "detail": str(exc)}]}
                raw = json.dumps(doc).encode() if doc is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/vnd.api+json")
                self.send_header("Content-Length", str(len(raw)))
                if status == 429:
                    self.send_header("Retry-After", "0")
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PATCH = do_DELETE = _go

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
