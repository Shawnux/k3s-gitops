"""Thin client for TIDAL's v2 JSON:API (openapi.tidal.com/v2).

Written against API reference v1.10.245. Pages are cursor-based and small
(20 items), so everything here follows cursors to the end.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import parse_qs, urlsplit

import requests

log = logging.getLogger("tidal_disco.api")

JSONAPI = "application/vnd.api+json"
NEEDED_SCOPES = ("collection.read", "playlists.read", "playlists.write")


class TidalError(RuntimeError):
    def __init__(self, status: int, method: str, path: str, body: str = ""):
        self.status, self.method, self.path, self.body = status, method, path, body
        super().__init__(f"TIDAL {method} {path} -> {status} {body[:300]}")


class AuthError(RuntimeError):
    pass


class Auth:
    """Refresh-token auth. TIDAL may rotate the refresh token, so the newest one
    is kept in the state database; the one from the environment only seeds it."""

    def __init__(self, client_id, client_secret, state, env_refresh_token, token_url, session=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.state = state
        self.env_refresh = env_refresh_token
        self.token_url = token_url
        self.session = session or requests.Session()
        self._access = None
        self._expires = 0.0

    def token(self, force: bool = False) -> str:
        if not force and self._access and time.time() < self._expires - 60:
            return self._access
        if not self.client_id:
            raise AuthError("TIDAL_CLIENT_ID is not set")
        stored = self.state.get("refresh_token")
        candidates = [t for t in dict.fromkeys((stored, self.env_refresh)) if t]
        if not candidates:
            raise AuthError("no refresh token: run `tidal-disco login` and set TIDAL_REFRESH_TOKEN")
        last = ""
        for rt in candidates:
            data = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": self.client_id}
            auth = (self.client_id, self.client_secret) if self.client_secret else None
            try:
                r = self.session.post(self.token_url, data=data, auth=auth, timeout=30)
            except requests.RequestException as exc:
                raise AuthError(f"could not reach TIDAL's token endpoint: {exc}") from exc
            if r.status_code == 200:
                tok = r.json()
                self._access = tok["access_token"]
                self._expires = time.time() + int(tok.get("expires_in", 3600))
                new = tok.get("refresh_token") or rt
                if new != stored:
                    self.state.set("refresh_token", new)
                granted = set((tok.get("scope") or "").split())
                missing = [s for s in NEEDED_SCOPES if granted and s not in granted]
                if missing:
                    log.warning("token is missing scopes %s: log in again with them", missing)
                return self._access
            last = f"{r.status_code} {r.text[:200]}"
            if r.status_code >= 500:
                raise AuthError(f"TIDAL token endpoint error: {last}")
        raise AuthError(f"refresh token rejected ({last}): run `tidal-disco login` again")


def next_page(doc: dict):
    """(cursor, raw_next_link) for a JSON:API document; both None on the last page."""
    links = doc.get("links") or {}
    cursor = (links.get("meta") or {}).get("nextCursor")
    if cursor:
        return cursor, None
    nxt = links.get("next")
    if not nxt:
        return None, None
    found = parse_qs(urlsplit(nxt).query).get("page[cursor]")
    if found:
        return found[0], None
    return None, nxt


class Client:
    MAX_RETRIES = 6

    def __init__(self, auth, base, country, rps=3.0, session=None, sleep=time.sleep, timeout=45):
        self.auth = auth
        self.base = base.rstrip("/")
        self.country = country
        self.session = session or requests.Session()
        self.sleep = sleep
        self.timeout = timeout
        self.base_interval = 1.0 / rps if rps > 0 else 0.0
        self.interval = self.base_interval
        self._last = 0.0
        self._streak = 0
        self.calls = 0
        self.rate_limited = 0

    # pacing ----------------------------------------------------------------
    def _throttle(self) -> None:
        wait = self._last + self.interval - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        self._last = time.monotonic()

    def _slow_down(self) -> None:
        self.rate_limited += 1
        self._streak = 0
        self.interval = min(max(self.interval, 0.25) * 2, 5.0)

    def _speed_up(self) -> None:
        self._streak += 1
        if self._streak >= 40 and self.interval > self.base_interval:
            self.interval = max(self.base_interval, self.interval * 0.8)
            self._streak = 0

    def _abs(self, link: str) -> str:
        if link.startswith("http"):
            return link
        parts = urlsplit(self.base)
        if parts.path and link.startswith(parts.path + "/"):
            return f"{parts.scheme}://{parts.netloc}{link}"
        return self.base + link

    # requests --------------------------------------------------------------
    def request(self, method, path, params=None, body=None, idempotency_key=None):
        url = self._abs(path)
        attempt = 0
        reauthed = False
        while True:
            attempt += 1
            self._throttle()
            headers = {"Authorization": f"Bearer {self.auth.token()}", "Accept": JSONAPI}
            if body is not None:
                headers["Content-Type"] = JSONAPI
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
            try:
                r = self.session.request(
                    method, url, params=params, json=body, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                if attempt > self.MAX_RETRIES:
                    raise TidalError(0, method, path, str(exc)) from exc
                self.sleep(min(60, 2**attempt))
                continue
            self.calls += 1
            if r.status_code == 429:
                self._slow_down()
                if attempt > self.MAX_RETRIES + 4:
                    raise TidalError(429, method, path, r.text)
                try:
                    wait = float(r.headers.get("Retry-After", ""))
                except ValueError:
                    wait = min(120, 2**attempt)
                self.sleep(min(max(wait, 1.0), 300))
                continue
            if r.status_code == 401 and not reauthed:
                reauthed = True
                self.auth.token(force=True)
                continue
            if r.status_code >= 500:
                if attempt > self.MAX_RETRIES:
                    raise TidalError(r.status_code, method, path, r.text)
                self.sleep(min(60, 2**attempt))
                continue
            if 200 <= r.status_code < 300:
                self._speed_up()
                if not r.content:
                    return None
                try:
                    return r.json()
                except ValueError:
                    return None
            raise TidalError(r.status_code, method, path, r.text)

    def get(self, path, params=None):
        return self.request("GET", path, params) or {}

    def paged(self, path, params=None, max_pages=5000):
        """Yield every page of a collection or relationship, following cursors."""
        params = dict(params or {})
        url = path
        seen = set()
        for _ in range(max_pages):
            doc = self.get(url, params)
            yield doc
            cursor, raw = next_page(doc)
            marker = cursor or raw
            if not marker:
                return
            if marker in seen:
                log.warning("TIDAL repeated a page cursor on %s; stopping there", path)
                return
            seen.add(marker)
            if cursor:
                params["page[cursor]"] = cursor
            else:
                url, params = raw, {}
        raise TidalError(0, "GET", path, f"more than {max_pages} pages")
