"""SQLite state: the rotating refresh token, the album cache and what the tool put where."""

from __future__ import annotations

import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS album_cache(
    album_id TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS artist_result(
    artist_id TEXT PRIMARY KEY, name TEXT NOT NULL, album_ids TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS block_part(block TEXT PRIMARY KEY, part INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS part(
    part INTEGER PRIMARY KEY, playlist_id TEXT NOT NULL, desired_hash TEXT, remote_stamp TEXT);
CREATE TABLE IF NOT EXISTS managed(
    playlist_id TEXT NOT NULL, track_id TEXT NOT NULL, PRIMARY KEY(playlist_id, track_id));
CREATE TABLE IF NOT EXISTS unaddable(track_id TEXT PRIMARY KEY, reason TEXT, at REAL NOT NULL);
"""

UNADDABLE_RETRY_DAYS = 14


class State:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # key/value -------------------------------------------------------------
    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # album cache -----------------------------------------------------------
    def get_album(self, album_id: str):
        row = self.db.execute(
            "SELECT payload, fetched_at FROM album_cache WHERE album_id=?", (album_id,)
        ).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    def put_album(self, album_id: str, record: dict) -> None:
        self.db.execute(
            "INSERT INTO album_cache(album_id,payload,fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(album_id) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
            (album_id, json.dumps(record, separators=(",", ":")), time.time()),
        )

    # last good listing per artist -----------------------------------------
    def get_artist_result(self, artist_id: str):
        row = self.db.execute(
            "SELECT album_ids FROM artist_result WHERE artist_id=?", (artist_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def set_artist_result(self, artist_id: str, name: str, album_ids: list[str]) -> None:
        self.db.execute(
            "INSERT INTO artist_result(artist_id,name,album_ids,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(artist_id) DO UPDATE SET name=excluded.name, album_ids=excluded.album_ids, "
            "updated_at=excluded.updated_at",
            (artist_id, name, json.dumps(album_ids), time.time()),
        )

    def all_artist_results(self) -> dict[str, list[str]]:
        return {r[0]: json.loads(r[1]) for r in self.db.execute("SELECT artist_id, album_ids FROM artist_result")}

    # which part each artist block lives in ---------------------------------
    def block_parts(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT block, part FROM block_part"))

    def save_block_parts(self, mapping: dict[str, int]) -> None:
        self.db.execute("BEGIN")
        self.db.execute("DELETE FROM block_part")
        self.db.executemany("INSERT INTO block_part(block,part) VALUES(?,?)", mapping.items())
        self.db.execute("COMMIT")

    # playlists -------------------------------------------------------------
    def get_part(self, part: int):
        row = self.db.execute(
            "SELECT playlist_id, desired_hash, remote_stamp FROM part WHERE part=?", (part,)
        ).fetchone()
        return {"playlist_id": row[0], "desired_hash": row[1], "remote_stamp": row[2]} if row else None

    def set_part(self, part: int, playlist_id: str, desired_hash=None, remote_stamp=None) -> None:
        self.db.execute(
            "INSERT INTO part(part,playlist_id,desired_hash,remote_stamp) VALUES(?,?,?,?) "
            "ON CONFLICT(part) DO UPDATE SET playlist_id=excluded.playlist_id, "
            "desired_hash=excluded.desired_hash, remote_stamp=excluded.remote_stamp",
            (part, playlist_id, desired_hash, remote_stamp),
        )

    def known_parts(self) -> list[int]:
        return [r[0] for r in self.db.execute("SELECT part FROM part ORDER BY part")]

    def managed(self, playlist_id: str) -> set[str]:
        return {
            r[0]
            for r in self.db.execute("SELECT track_id FROM managed WHERE playlist_id=?", (playlist_id,))
        }

    def add_managed(self, playlist_id: str, track_ids) -> None:
        self.db.executemany(
            "INSERT OR IGNORE INTO managed(playlist_id,track_id) VALUES(?,?)",
            [(playlist_id, t) for t in track_ids],
        )

    def drop_managed(self, playlist_id: str, track_ids) -> None:
        self.db.executemany(
            "DELETE FROM managed WHERE playlist_id=? AND track_id=?",
            [(playlist_id, t) for t in track_ids],
        )

    # tracks TIDAL refused --------------------------------------------------
    def unaddable(self) -> set[str]:
        cutoff = time.time() - UNADDABLE_RETRY_DAYS * 86400
        self.db.execute("DELETE FROM unaddable WHERE at < ?", (cutoff,))
        return {r[0] for r in self.db.execute("SELECT track_id FROM unaddable")}

    def mark_unaddable(self, items: dict[str, str]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO unaddable(track_id,reason,at) VALUES(?,?,?)",
            [(t, reason, time.time()) for t, reason in items.items()],
        )
