"""Watch-folder mode (plan.md §10). Enabled by env vars KSORTER_WATCH_DIR and
KSORTER_WATCH_DEST. New videos dropped into the watch folder are auto-sorted —
confident matches only; uncertain ones are left for you in the normal queue.
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from . import engine
from .config import settings
from .logging_setup import get_logger
from .scanner import is_video, scan

log = get_logger("ksorter.watch")


async def watch_loop() -> None:
    if not settings.watch_dir or not settings.watch_dest:
        log.info("Watch-folder mode disabled (set KSORTER_WATCH_DIR + KSORTER_WATCH_DEST).")
        return
    try:
        from watchfiles import awatch
    except ImportError:
        log.warning("watchfiles not installed; watch-folder disabled.")
        return

    src, dest = settings.watch_dir, Path(settings.watch_dest)
    log.info("Watching %s -> %s (confident matches only)", src, dest)

    # Sort anything already sitting there on startup.
    await asyncio.to_thread(_sort_existing, src, dest)

    async for changes in awatch(src):
        for _change, path in changes:
            if is_video(path) and Path(path).exists():
                await asyncio.to_thread(_sort_one, Path(path), dest)


def _sort_existing(src: str, dest: Path) -> None:
    for vf in scan(src):
        _sort_one(vf.path, dest)


def _sort_one(path: Path, dest: Path) -> None:
    from .scanner import VideoFile
    try:
        vf = VideoFile(path=path, size=path.stat().st_size, stem=path.stem)
    except OSError:
        return
    item = engine.build_plan_item(vf, dest)
    if item.status == "auto":
        result = engine.apply_item(item, "watch-" + uuid.uuid4().hex[:8])
        log.info("Watch auto-sort %s -> %s", path.name, result.get("dest", result["status"]))
    else:
        log.info("Watch: %s needs review (%s); left in place.", path.name, item.status)
