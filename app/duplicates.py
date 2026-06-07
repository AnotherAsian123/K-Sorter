"""Duplicate detection (plan.md §10).

Never deletes anything. Flags suspected duplicates using file size first, then a
fast partial hash (head+tail) to confirm without reading whole videos. Results
surface in the UI and are written to duplicates.csv for review.
"""
from __future__ import annotations

import csv
import hashlib
from collections import defaultdict
from pathlib import Path

from . import database as db
from .config import settings
from .logging_setup import get_logger
from .scanner import scan

log = get_logger("ksorter.dupes")

_EDGE = 256 * 1024  # bytes read from head and tail


def _partial_hash(path: Path, size: int) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            h.update(fh.read(_EDGE))
            if size > _EDGE * 2:
                fh.seek(-_EDGE, 2)
                h.update(fh.read(_EDGE))
    except OSError:
        return f"unreadable:{size}"
    return h.hexdigest()


def detect(source: str) -> list[dict]:
    """Return suspected duplicate groups, persist them, and write the CSV."""
    by_size: dict[int, list[Path]] = defaultdict(list)
    for vf in scan(source):
        if vf.size > 0:
            by_size[vf.size].append(vf.path)

    suspects: dict[str, list[tuple[Path, int]]] = defaultdict(list)
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        for p in paths:
            key = f"{size}:{_partial_hash(p, size)}"
            suspects[key].append((p, size))

    flagged = [(key, p, size) for key, items in suspects.items()
               if len(items) > 1 for p, size in items]

    # Persist (replace previous run) and export CSV.
    db.execute("DELETE FROM duplicates")
    if flagged:
        db.executemany(
            "INSERT INTO duplicates(group_key, path, size) VALUES(?,?,?)",
            [(k, str(p), s) for k, p, s in flagged])
        out = settings.logs_dir / "duplicates.csv"
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["duplicate_set", "size_bytes", "path"])
            for k, p, s in flagged:
                w.writerow([k, s, p])
        log.info("Flagged %d suspected duplicate files -> %s", len(flagged), out)

    return [{"set": k, "path": str(p), "size": s} for k, p, s in flagged]
