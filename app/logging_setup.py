"""Logging per CLAUDE.md: a friendly summary surfaces in the UI, while the full
detail goes to dedicated log files. We keep several logs split by purpose so the
signal isn't drowned out (plan.md §9).

  - k-sorter.log            main app log (every decision, score, path, traceback)
  - moves.log               every move performed
  - needs_review.log        uncertain / ambiguous videos awaiting confirmation
  - manual_intervention.log confidence == None videos needing a human
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import settings

_FMT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_configured = False


def _file_handler(filename: str, level: int = logging.INFO) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        settings.logs_dir / filename,
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_FMT))
    handler.setLevel(level)
    return handler


def setup_logging() -> None:
    """Idempotent; safe to call on every startup."""
    global _configured
    if _configured:
        return
    settings.ensure_dirs()

    root = logging.getLogger("ksorter")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    # Main log (everything) + console.
    root.addHandler(_file_handler("k-sorter.log", logging.DEBUG))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_FMT))
    console.setLevel(logging.INFO)
    root.addHandler(console)

    # Purpose-specific logs are separate, non-propagating loggers.
    for name, fname in (
        ("ksorter.moves", "moves.log"),
        ("ksorter.review", "needs_review.log"),
        ("ksorter.manual", "manual_intervention.log"),
    ):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.handlers.clear()
        lg.addHandler(_file_handler(fname))
        lg.propagate = True  # also lands in the main log

    _configured = True


def get_logger(name: str = "ksorter") -> logging.Logger:
    return logging.getLogger(name)
