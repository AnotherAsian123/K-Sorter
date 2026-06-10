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
    method: str = ""     # 'rename' | 'hardlink' | 'copy' | 'dedupe'
    dest: str = ""
    reason: str = ""


def _partial_hash(path: Path, edge: int = 1024 * 1024) -> str | None:
    """Hash of the file's head + tail — enough to confirm two files are the same
    without reading whole videos. None if unreadable (treated as 'not equal')."""
    h = hashlib.sha256()
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            h.update(fh.read(edge))
            if size > edge * 2:
                fh.seek(-edge, 2)
                h.update(fh.read(edge))
    except OSError:
        return None
    return h.hexdigest()


def _same_file(a: Path, b: Path) -> bool:
    """True only if a and b are genuinely the same file (same inode, or same
    size AND matching head/tail hash). Used to safely remove a redundant copy."""
    try:
        if os.path.samefile(a, b):
            return True
    except OSError:
        pass
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
    except OSError:
        return False
    pa = _partial_hash(a)
    return pa is not None and pa == _partial_hash(b)


def _relocate(src: Path, dest: Path, verify_checksum: bool) -> MoveResult:
    """Move src -> dest with the fastest safe method. Assumes dest does not exist
    (caller checks collisions). Used by both safe_move and undo so they share the
    same cross-device (EXDEV) handling."""
    dest.parent.mkdir(parents=True, exist_ok=True)

    # 1) Atomic rename — instant when src & dest share a mount point.
    try:
        os.replace(src, dest)
        return MoveResult("moved", method="rename", dest=str(dest))
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            # A real problem (permissions, read-only, etc.) — not just a mount
            # boundary. Worth a warning; we still try the safe fallbacks below.
            log.warning("rename failed (%s); trying link/copy: %s -> %s", exc, src, dest)

    # 2) Hardlink + unlink — instant, no copy, for separate bind mounts on the
    #    same underlying filesystem (typical Unraid /watch <-> /watch_dest).
    try:
        os.link(src, dest)
    except OSError:
        pass  # different filesystem (or no hardlink support) -> real copy
    else:
        try:
            src.unlink()
        except OSError:
            log.warning("hardlinked but could not remove source: %s", src)
        return MoveResult("moved", method="hardlink", dest=str(dest))

    # 3) Cross-filesystem: copy -> verify -> swap -> remove source.
    tmp = dest.with_name(dest.name + ".ksort-tmp")
    try:
        shutil.copyfile(src, tmp)
        if tmp.stat().st_size != src.stat().st_size:
            tmp.unlink(missing_ok=True)
            return MoveResult("error", reason="size mismatch after copy")
        if verify_checksum and _sha256(tmp) != _sha256(src):
            tmp.unlink(missing_ok=True)
            return MoveResult("error", reason="checksum mismatch after copy")
        os.replace(tmp, dest)
        src.unlink()
        return MoveResult("moved", method="copy" + ("+sha256" if verify_checksum else ""),
                          dest=str(dest))
    except OSError as exc:
        Path(tmp).unlink(missing_ok=True)
        return MoveResult("error", reason=str(exc))


def safe_move(src: Path, dest: Path, verify_checksum: bool = False) -> MoveResult:
    src, dest = Path(src), Path(dest)
    if not src.exists():
        return MoveResult("error", reason="source disappeared")
    # Already in place? A no-op — and it MUST be caught before the dedupe check
    # below, which would otherwise see "identical file at dest" and delete it.
    try:
        if src.resolve() == dest.resolve():
            return MoveResult("moved", method="noop", dest=str(dest))
    except OSError:
        pass
    if dest.exists():
        # If the destination already holds the SAME file, the source is a
        # redundant copy in the wrong place — remove it (the move is effectively
        # already done). Different file with the same name -> never overwrite.
        if _same_file(src, dest):
            try:
                src.unlink()
            except OSError as exc:
                log.error("DEDUPE: could not remove redundant source %s: %s", src, exc)
                return MoveResult("error", reason=str(exc))
            log.info("DEDUPE: identical file already at %s; removed redundant %s", dest, src)
            return MoveResult("moved", method="dedupe", dest=str(dest))
        log.info("SKIP collision: %s already exists, different file (source kept: %s)", dest, src)
        return MoveResult("skipped", dest=str(dest), reason="destination exists (different file)")

    result = _relocate(src, dest, verify_checksum)
    if result.status == "moved":
        log.info("MOVE %s: %s -> %s", result.method, src, dest)
    else:
        log.error("MOVE failed (%s): %s -> %s", result.reason, src, dest)
    return result


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
        if action == "dedupe":
            # We removed a redundant copy at `source`; the file still lives at
            # `dest`. Restore that copy (hardlink/copy back, keep dest).
            if src_p.exists():
                return True
            if not dest_p.exists():
                log.warning("UNDO dedupe: file missing at %s", dest)
                return False
            res = replicate(dest_p, src_p)
            if res.status == "moved":
                log.info("UNDO dedupe (%s): restored %s", res.method, source)
                return True
            log.error("UNDO dedupe failed: %s", res.reason)
            return False
        # action == 'move': put it back where it came from (EXDEV-safe).
        if not dest_p.exists():
            log.warning("UNDO: dest missing, cannot restore %s", dest)
            return False
        if src_p.exists():
            log.warning("UNDO: original path occupied, not overwriting %s", source)
            return False
        result = _relocate(dest_p, src_p, verify_checksum=False)
        if result.status == "moved":
            log.info("UNDO %s: %s -> %s", result.method, dest, source)
            return True
        log.error("UNDO failed (%s): %s -> %s", result.reason, dest, source)
        return False
    except OSError:
        log.exception("UNDO failed for %s -> %s", dest, source)
        return False
