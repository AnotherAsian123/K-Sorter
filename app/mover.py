"""Safe, fast, verified file operations (plan.md §5 step 6).

Move strategy, fastest-first:
  1. os.replace (atomic rename)  -> instant, when src & dest share a mount point.
  2. hardlink + unlink           -> instant, no data copied, for separate bind
                                    mounts on the SAME underlying filesystem
                                    (the common Unraid case: /watch + /watch_dest
                                    mapped as different volumes on one disk).
  3. copy -> verify -> delete    -> only when genuinely on different filesystems.

  - Never overwrite. Collisions are skipped and reported.
  - Filenames are never altered; only their parent folder changes.
  - Folder names are sanitized so nothing can escape the destination root.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .logging_setup import get_logger

log = get_logger("ksorter.moves")

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_component(name: str) -> str:
    """Make a single path component safe (no separators, no traversal)."""
    name = (name or "").strip().replace("..", "")
    name = _ILLEGAL.sub("", name)
    name = name.rstrip(". ")            # Windows dislikes trailing dot/space
    return name or "Unknown"


def _sha256(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class MoveResult:
    status: str          # 'moved' | 'skipped' | 'error'
    method: str = ""     # 'rename' | 'hardlink' | 'copy'
    dest: str = ""
    reason: str = ""


def safe_move(src: Path, dest: Path, verify_checksum: bool = False) -> MoveResult:
    src, dest = Path(src), Path(dest)
    if not src.exists():
        return MoveResult("error", reason="source disappeared")
    if dest.exists():
        log.info("SKIP collision: %s already exists (source kept: %s)", dest, src)
        return MoveResult("skipped", dest=str(dest), reason="destination exists")

    dest.parent.mkdir(parents=True, exist_ok=True)

    # 1) Atomic rename — instant when src & dest share a mount point.
    try:
        os.replace(src, dest)
        log.info("MOVE rename: %s -> %s", src, dest)
        return MoveResult("moved", method="rename", dest=str(dest))
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            # A real problem (permissions, read-only, etc.) — not just a mount
            # boundary. Worth a warning; we still try the safe fallbacks below.
            log.warning("rename failed (%s); trying link/copy: %s -> %s", exc, src, dest)

    # 2) Hardlink + unlink — instant, no copy, for separate bind mounts on the
    #    same underlying filesystem (typical Unraid /watch -> /watch_dest).
    try:
        os.link(src, dest)
    except OSError:
        pass  # different filesystem (or no hardlink support) -> real copy
    else:
        try:
            src.unlink()
        except OSError:
            log.warning("hardlinked but could not remove source: %s", src)
        log.info("MOVE hardlink (cross-mount, same fs): %s -> %s", src, dest)
        return MoveResult("moved", method="hardlink", dest=str(dest))

    # 3) Cross-filesystem: copy -> verify -> swap -> remove source.
    tmp = dest.with_name(dest.name + ".ksort-tmp")
    try:
        shutil.copyfile(src, tmp)
        src_size = src.stat().st_size
        if tmp.stat().st_size != src_size:
            tmp.unlink(missing_ok=True)
            return MoveResult("error", reason="size mismatch after copy")
        if verify_checksum and _sha256(tmp) != _sha256(src):
            tmp.unlink(missing_ok=True)
            return MoveResult("error", reason="checksum mismatch after copy")
        os.replace(tmp, dest)
        src.unlink()
        log.info("MOVE copy%s: %s -> %s",
                 "+sha256" if verify_checksum else "+size", src, dest)
        return MoveResult("moved", method="copy", dest=str(dest))
    except OSError as exc:
        Path(tmp).unlink(missing_ok=True)
        log.exception("MOVE failed: %s -> %s", src, dest)
        return MoveResult("error", reason=str(exc))


def replicate(src: Path, dest: Path) -> MoveResult:
    """Place a copy of src at dest WITHOUT removing src (for collab replication).
    Prefers a hardlink (no extra disk) and falls back to a real copy."""
    src, dest = Path(src), Path(dest)
    if dest.exists():
        return MoveResult("skipped", dest=str(dest), reason="destination exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dest)
        log.info("REPLICA hardlink: %s -> %s", src, dest)
        return MoveResult("moved", method="hardlink", dest=str(dest))
    except OSError:
        try:
            shutil.copyfile(src, dest)
            log.info("REPLICA copy: %s -> %s", src, dest)
            return MoveResult("moved", method="copy", dest=str(dest))
        except OSError as exc:
            Path(dest).unlink(missing_ok=True)
            log.exception("REPLICA failed: %s -> %s", src, dest)
            return MoveResult("error", reason=str(exc))


def undo_one(source: str, dest: str, action: str) -> bool:
    """Reverse a single journal entry. Returns True on success."""
    src_p, dest_p = Path(source), Path(dest)
    try:
        if action == "replica":
            # Replica left the original in place; just remove the copy/link.
            dest_p.unlink(missing_ok=True)
            return True
        # action == 'move': put it back where it came from.
        if not dest_p.exists():
            log.warning("UNDO: dest missing, cannot restore %s", dest)
            return False
        if src_p.exists():
            log.warning("UNDO: original path occupied, not overwriting %s", source)
            return False
        src_p.parent.mkdir(parents=True, exist_ok=True)
        os.replace(dest_p, src_p)
        log.info("UNDO: %s -> %s", dest, source)
        return True
    except OSError:
        log.exception("UNDO failed for %s -> %s", dest, source)
        return False
