"""Recursive video scanner (plan.md §1, §5). Streams results so huge libraries
never have to be held in memory at once (safe-mode)."""
from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".ts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".ogv",
}


@dataclass
class VideoFile:
    path: Path
    size: int
    stem: str


def is_video(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in VIDEO_EXTS


def scan(root: str | Path) -> Iterator[VideoFile]:
    """Yield every video under ``root`` (recursively), streaming."""
    root = Path(root)
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False) and is_video(entry.name):
                            st = entry.stat()
                            yield VideoFile(
                                path=Path(entry.path),
                                size=st.st_size,
                                stem=os.path.splitext(entry.name)[0],
                            )
                    except OSError:
                        continue  # unreadable entry; skip, logged by caller
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            continue


def count_videos(root: str | Path) -> int:
    return sum(1 for _ in scan(root))
