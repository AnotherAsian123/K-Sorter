"""Live web enrichment for groups not in the local DB (plan.md §4).

Free, no API key: queries the public Wikipedia API. We never auto-trust the
result for a real move — the user confirms it first, then it's cached in SQLite
so each unknown is looked up only once.
"""
from __future__ import annotations

import uuid

import httpx

from . import database as db
from .logging_setup import get_logger
from .matcher import reload_index
from .normalize import normalize

log = get_logger("ksorter.enrich")

_WIKI = "https://en.wikipedia.org/w/api.php"
# Wikipedia's API policy requires a descriptive User-Agent with a contact URL;
# a generic one is rejected with HTTP 403.
_UA = "K-Sorter/1.0 (https://github.com/AnotherAsian123/K-Sorter; self-hosted k-pop sorter)"


def search_group(name: str) -> list[dict]:
    """Return candidate matches (title, snippet, url) for a possible group name."""
    try:
        resp = httpx.get(_WIKI, params={
            "action": "query", "list": "search", "format": "json",
            "srlimit": 5, "srsearch": f"{name} kpop group",
        }, timeout=20, headers={
            "User-Agent": _UA,
            "Accept": "application/json",
            "Api-User-Agent": _UA,
        })
        resp.raise_for_status()
        results = resp.json().get("query", {}).get("search", [])
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Wikipedia lookup failed for %r: %s", name, exc)
        return []
    out = []
    for r in results:
        title = r.get("title", "")
        snippet = (r.get("snippet", "") or "").replace("<span class=\"searchmatch\">", "").replace("</span>", "")
        out.append({
            "title": title,
            "snippet": snippet,
            "url": f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
        })
    log.info("Enrich lookup %r -> %d candidates", name, len(out))
    return out


def add_confirmed_group(name: str, name_ko: str | None = None,
                        aliases: list[str] | None = None) -> str:
    """Persist a user-confirmed group (source='user' so it survives reseeds)."""
    gid = "user-" + uuid.uuid4().hex[:8]
    db.execute(
        "INSERT INTO groups(id,name,name_ko,source,confirmed,is_active)"
        " VALUES(?,?,?,?,?,?)", (gid, name, name_ko, "user", 1, 1))
    for raw in [name, name_ko, *(aliases or [])]:
        if raw and raw.strip():
            db.execute(
                "INSERT OR IGNORE INTO aliases(entity_type,entity_id,alias,alias_raw)"
                " VALUES(?,?,?,?)", ("group", gid, normalize(raw), raw))
    reload_index()
    log.info("Added user-confirmed group %s (%s)", name, gid)
    return gid


def add_member(group_id: str, name: str, name_ko: str | None = None,
               is_current: bool = True) -> str | None:
    """Add a member to an existing group (for rosters the seed data missed, e.g.
    new line-up additions). Persists + aliases so it matches automatically next time."""
    if not db.query_one("SELECT 1 FROM groups WHERE id = ?", (group_id,)):
        return None
    mid = "user-" + uuid.uuid4().hex[:8]
    db.execute("INSERT INTO members(id, stage_name, stage_name_ko) VALUES(?,?,?)",
               (mid, name, name_ko))
    db.execute("INSERT OR IGNORE INTO group_members(group_id, member_id, is_current)"
               " VALUES(?,?,?)", (group_id, mid, 1 if is_current else 0))
    for raw in (name, name_ko):
        if raw and raw.strip():
            db.execute("INSERT OR IGNORE INTO aliases(entity_type,entity_id,alias,alias_raw)"
                       " VALUES('member',?,?,?)", (mid, normalize(raw), raw))
    reload_index()
    log.info("Added member %s to group %s (%s)", name, group_id, mid)
    return mid
