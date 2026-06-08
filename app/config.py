"""Runtime configuration for K-Sorter.

All persistent state lives under CONFIG_DIR (``/config`` in the container, an
Unraid ``appdata`` share in practice). Nothing here is speculative — every
setting maps to a documented feature in plan.md.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    val = os.getenv(name)
    return val if val not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _path_or_mount(name: str, mount: str) -> str:
    """An explicit env path wins; otherwise use the container mount if present.

    This lets the Unraid template expose Source/Destination/Watch folders as
    *Path* mappings (with a folder-browse dropdown) rather than text env vars:
    when the user maps a folder, the mount exists and we pick it up.
    """
    val = os.getenv(name)
    if val:
        return val
    return mount if Path(mount).is_dir() else ""


@dataclass(frozen=True)
class Settings:
    # Where the SQLite DB, logs and exports live.
    config_dir: Path = field(default_factory=lambda: Path(_env("KSORTER_CONFIG_DIR", "/config")))

    # WebUI port (informational; uvicorn is launched with it in the entrypoint).
    port: int = field(default_factory=lambda: int(_env("KSORTER_PORT", "8080")))

    # Source / destination folder mounts (Unraid Path mappings). Used to prefill
    # the web UI so you don't have to retype the container paths.
    source_default: str = field(default_factory=lambda: _path_or_mount("KSORTER_SOURCE", "/source"))
    dest_default: str = field(default_factory=lambda: _path_or_mount("KSORTER_DEST", "/destination"))

    # Optional watch-folder mode (plan.md §10). Enabled when both are present;
    # mapped as Path folders (/watch, /watch_dest) or set via env. Empty = off.
    watch_dir: str = field(default_factory=lambda: _path_or_mount("KSORTER_WATCH_DIR", "/watch"))
    watch_dest: str = field(default_factory=lambda: _path_or_mount("KSORTER_WATCH_DEST", "/watch_dest"))

    # Seed dataset source (free, CC0). Refreshed only on restart / manual update.
    seed_url: str = field(
        default_factory=lambda: _env(
            "KSORTER_SEED_URL",
            "https://unpkg.com/kpopnet.json@latest/kpopnet.json",
        )
    )

    # Matching thresholds (0-100). At/above auto = auto-sort; between = confirm.
    auto_threshold: int = field(default_factory=lambda: int(_env("KSORTER_AUTO_THRESHOLD", "90")))
    confirm_threshold: int = field(default_factory=lambda: int(_env("KSORTER_CONFIRM_THRESHOLD", "70")))

    # Cross-filesystem move: verify by size always; checksum is opt-in (slow).
    verify_checksum: bool = field(default_factory=lambda: _env_bool("KSORTER_VERIFY_CHECKSUM", False))

    @property
    def db_path(self) -> Path:
        return self.config_dir / "k-sorter.db"

    @property
    def logs_dir(self) -> Path:
        return self.config_dir / "logs"

    @property
    def seed_cache(self) -> Path:
        return self.config_dir / "kpopnet.json"

    def ensure_dirs(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
