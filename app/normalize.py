"""Text normalization shared by the seeder and matcher.

We never touch the real files — this only normalizes strings *in memory* for
comparison (plan.md §2).
"""
from __future__ import annotations

import re
import unicodedata

# Filler words (EN + KO) that describe the *kind* of video, not the group/member.
# Used to strip noise before matching. Files are never renamed.
FILLER = {
    "fancam", "focus", "fan", "cam", "stage", "mix", "stagemix", "performance",
    "perf", "live", "mv", "m", "v", "official", "teaser", "dance", "practice",
    "ver", "version", "comeback", "debut", "special", "ending", "clip", "video",
    "full", "hd", "fhd", "uhd", "4k", "8k", "1080p", "720p", "2160p", "60fps",
    "x264", "x265", "h264", "h265", "hevc", "aac", "webm", "mp4", "mkv",
    # Korean
    "직캠", "포커스", "세로직캠", "교차편집", "무대", "직캠세로", "공식",
    "예능", "방송", "음악방송", "직캠모음",
}

# Tokens that mark a *solo* video (member-level), plan.md §5 step 3.
SOLO_MARKERS = {"fancam", "focus", "직캠", "포커스", "세로직캠", "1인샷", "직캠세로"}

_DATE_RE = re.compile(
    r"\b(?:19|20)?\d{2}[.\-_/]?(?:0?[1-9]|1[0-2])[.\-_/]?(?:0?[1-9]|[12]\d|3[01])\b"
)
_BRACKET_RE = re.compile(r"[\[\](){}<>]")
_PUNCT_RE = re.compile(r"[^\w가-힣\s]")  # keep word chars, Hangul, space
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lower-case, strip accents/punctuation, collapse whitespace. Keeps Hangul."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    text = _BRACKET_RE.sub(" ", text)
    text = _PUNCT_RE.sub(" ", text)
    # Strip Latin accents (combining marks) but recompose afterwards so Hangul
    # syllables are NOT left decomposed into jamo (which would break KO matching).
    text = "".join(
        c for c in unicodedata.normalize("NFD", text)
        if not unicodedata.combining(c)
    )
    text = unicodedata.normalize("NFC", text)
    return _WS_RE.sub(" ", text).strip()


def strip_dates(text: str) -> str:
    """Remove date-like tokens for matching only (the filename keeps its date)."""
    return _DATE_RE.sub(" ", text)


def tokens_for_match(filename_stem: str) -> tuple[list[str], bool]:
    """Return (meaningful_tokens, is_solo_video) from a filename stem.

    Pure in-memory parsing — the file itself is never modified.
    """
    cleaned = strip_dates(filename_stem)
    norm = normalize(cleaned)
    raw_tokens = [t for t in norm.split() if t]
    is_solo = any(t in SOLO_MARKERS for t in raw_tokens)
    meaningful = [t for t in raw_tokens if t not in FILLER and len(t) > 0]
    return meaningful, is_solo
