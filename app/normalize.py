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

# Tokens that signal a genuine multi-group collab stage. Without one of these,
# a second group name in a filename is treated as noise (song titles often
# contain words that are also group names, e.g. 'Secret Code' vs the group
# Secret) — Special Stages should be rare.
COLLAB_MARKERS = {
    "x", "vs", "feat", "ft", "with", "collab", "collaboration",
    "콜라보", "합동", "합동무대", "합동공연", "스페셜",
}

# Common fancam-title words that are NOT names. We must never learn these as
# aliases (doing so caused groups to be misidentified, e.g. false collabs).
LEARN_STOP = {
    # English
    "concert", "birthday", "congrats", "congratulations", "stage", "ending",
    "opening", "intro", "outro", "live", "behind", "making", "rehearsal",
    "cover", "interview", "talk", "highlight", "vlog", "showcase", "encore",
    "moment", "moments", "compilation", "reaction", "event", "festival",
    "tour", "day", "night", "ment", "selca", "vertical", "horizontal",
    # Korean
    "생일", "축하", "생일축하", "콘서트", "소감", "응원", "멘트", "엔딩", "오프닝",
    "인사", "행사", "팬싸", "팬사인회", "브이로그", "비하인드", "메이킹", "리허설",
    "셀카", "직찍", "안무", "안무영상", "커버", "라이브", "풀버전", "데뷔", "컴백",
    "음방", "인터뷰", "토크", "하이라이트", "축하공연", "공연", "모음", "현장",
    "직캠모음", "교차편집본", "응원법", "쇼케이스", "앵콜", "앙코르", "소속사",
}

_DATE_RE = re.compile(
    r"\b(?:19|20)?\d{2}[.\-_/]?(?:0?[1-9]|1[0-2])[.\-_/]?(?:0?[1-9]|[12]\d|3[01])\b"
)
# Square-bracket segments are almost always metadata (resolution, uploader,
# YouTube IDs like [cl5mMPajrHI]) — drop them entirely before matching.
_SQUARE_RE = re.compile(r"\[[^\]]*\]")
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
    cleaned = _SQUARE_RE.sub(" ", filename_stem)   # drop [4K], [youtube-id], …
    cleaned = strip_dates(cleaned)
    norm = normalize(cleaned)
    raw_tokens = [t for t in norm.split() if t]
    is_solo = any(t in SOLO_MARKERS for t in raw_tokens)
    meaningful = [t for t in raw_tokens if t not in FILLER and len(t) > 0]
    return meaningful, is_solo


def is_learnable_token(tok: str) -> bool:
    """Is this token safe to remember as a group/member alias? Excludes common
    title words, numbers, and id-like gibberish that would cause misidentification."""
    if not tok or len(tok) < 2:
        return False
    if tok in FILLER or tok in LEARN_STOP or tok in SOLO_MARKERS:
        return False
    if tok.isdigit():
        return False
    has_digit = any(c.isdigit() for c in tok)
    has_alpha = any(c.isalpha() and c.isascii() for c in tok)
    if has_digit and has_alpha:          # e.g. a YouTube id like cl5mmpajrhi
        return False
    if has_digit:                        # pure-ish numeric / counts
        return False
    if tok.isascii() and len(tok) >= 12:  # very long latin run = likely an id
        return False
    return True
