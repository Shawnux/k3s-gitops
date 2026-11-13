"""Settings: a TOML file plus environment variables for the secrets."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field

DEFAULT_TITLE_REGEX = [
    r"\bkaraoke\b",
    r"\btribute to\b",
    r"\boriginally performed by\b",
    r"\blullaby (versions?|renditions?)\b",
    r"\b(track by track|audio) commentary\b",
]


# Radio shows, mix series and festival sets. Matched as whole words against the
# release title. The first two lines are the old tidal-sync blocklist.
DEFAULT_COLLECTIONS = [
    "group therapy", "a state of trance", "asot", "abgt", "corstens countdown", "wake your mind",
    "electric for life", "purified", "club life", "satisfaction", "live at", "live from", "live set",
    "tomorrowland", "ultra music", "electric daisy carnival", "edc", "awakenings", "creamfields", "defqon",
    "clublife", "anjunabeats volume", "anjunabeats worldwide", "anjunadeep", "global dj broadcast",
    "future sound of egypt", "fsoe", "in search of sunrise", "universal religion", "trance nation",
    "whos afraid of 138", "pure trance", "find your harmony", "vonyc sessions", "hardwell on air",
    "heldeep radio", "protocol radio", "tritonia", "night owl radio", "drumcode radio",
    "spinnin sessions", "fabric presents", "fabriclive", "dj kicks", "global underground",
    "essential mix", "late night tales", "balance presents", "ministry of sound", "mixmag presents",
]


@dataclass
class Config:
    # [tidal]
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    country_code: str = "US"
    requests_per_second: float = 3.0
    api_base: str = "https://openapi.tidal.com/v2"
    token_url: str = "https://auth.tidal.com/v1/oauth2/token"

    # [discography]
    types: list[str] = field(default_factory=lambda: ["ALBUM", "EP", "SINGLE"])
    dedupe: str = "editions"  # none | editions | recordings
    prefer_explicit: bool = True
    min_own_track_share: float = 0.5
    compilation_min_tracks: int = 4
    exclude_dj_mixes: bool = True
    exclude_collections: bool = True
    collection_names: list[str] = field(default_factory=lambda: list(DEFAULT_COLLECTIONS))
    extra_collection_names: list[str] = field(default_factory=list)
    collection_own_share: float = 0.8
    series_threshold: int = 15
    exclude_title_regex: list[str] = field(default_factory=lambda: list(DEFAULT_TITLE_REGEX))
    skip_artist_ids: list[str] = field(default_factory=list)
    force_include_album_ids: list[str] = field(default_factory=list)
    force_exclude_album_ids: list[str] = field(default_factory=list)

    # [playlist]
    name: str = "Master Discography"
    access: str = "UNLISTED"  # UNLISTED | PUBLIC
    part_target: int = 1850
    part_cap: int = 2000
    prune_unmanaged: bool = False
    max_removal_fraction: float = 0.25

    # [run]
    state_path: str = "/data/state.db"
    max_minutes: int = 300
    album_refresh_days: int = 60
    discord_webhook_url: str = ""

    def part_name(self, part: int) -> str:
        return f"{self.name} {part:02d}"

    def validate(self) -> None:
        if self.dedupe not in ("none", "editions", "recordings"):
            raise SystemExit(f"config: dedupe must be none, editions or recordings, not {self.dedupe!r}")
        if self.access not in ("UNLISTED", "PUBLIC"):
            raise SystemExit("config: playlist.access must be UNLISTED or PUBLIC")
        bad = set(self.types) - {"ALBUM", "EP", "SINGLE"}
        if bad:
            raise SystemExit(f"config: unknown release types {sorted(bad)}")
        if not (0 < self.part_target <= self.part_cap <= 10000):
            raise SystemExit("config: need 0 < part_target <= part_cap <= 10000")


_SECTIONS = ("tidal", "discography", "playlist", "run")
_ENV = {
    "TIDAL_CLIENT_ID": "client_id",
    "TIDAL_CLIENT_SECRET": "client_secret",
    "TIDAL_REFRESH_TOKEN": "refresh_token",
    "TIDAL_API_BASE": "api_base",
    "TIDAL_TOKEN_URL": "token_url",
    "TIDAL_DISCO_STATE": "state_path",
    "DISCORD_WEBHOOK_URL": "discord_webhook_url",
}


def load(path: str | None = None, env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    cfg = Config()
    path = path or env.get("TIDAL_DISCO_CONFIG")
    if path:
        with open(path, "rb") as fh:
            doc = tomllib.load(fh)
        known = set(Config.__dataclass_fields__)
        for section, values in doc.items():
            if section not in _SECTIONS or not isinstance(values, dict):
                raise SystemExit(f"config: unknown section [{section}]")
            for key, value in values.items():
                if key not in known:
                    raise SystemExit(f"config: unknown key {section}.{key}")
                setattr(cfg, key, value)
    for var, attr in _ENV.items():
        if env.get(var):
            setattr(cfg, attr, env[var].strip())
    cfg.skip_artist_ids = [str(x) for x in cfg.skip_artist_ids]
    cfg.force_include_album_ids = [str(x) for x in cfg.force_include_album_ids]
    cfg.force_exclude_album_ids = [str(x) for x in cfg.force_exclude_album_ids]
    cfg.validate()
    return cfg
