"""Live web enrichment for groups not in the local DB (plan.md §4).

Free, no API key: queries the public Wikipedia API. We never auto-trust the
result for a real move — the user confirms it first, then it's cached in SQLite
so each unknown is looked up only once.
"""
from __future__ import annotations

import re
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


def _wiki_search(query: str, limit: int = 6) -> list[dict]:
    try:
        resp = httpx.get(_WIKI, params={
            "action": "query", "list": "search", "format": "json",
            "srlimit": limit, "srsearch": query,
        }, timeout=20, headers={
            "User-Agent": _UA, "Accept": "application/json", "Api-User-Agent": _UA,
        })
        resp.raise_for_status()
        return resp.json().get("query", {}).get("search", [])
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Wikipedia search failed for %r: %s", query, exc)
        return []


def search_group(name: str) -> list[dict]:
    """Return candidate matches (title, snippet, url) for a possible group name.

    Searches the bare name AND a k-pop-biased query, then ranks by how closely
    each title matches the name. Appending 'kpop group' alone wrecks relevance
    for compound names (e.g. 'Hearts2Hearts'), so the bare name is essential."""
    nq = normalize(name).replace(" ", "")
    seen: dict[str, dict] = {}
    for query in (name, f"{name} kpop group"):
        for r in _wiki_search(query):
            title = r.get("title", "")
            if not title or title in seen:
                continue
            tnorm = normalize(title).replace(" ", "")
            # Score: exact > prefix/contains > name-inside-parens > other.
            if tnorm == nq:
                score = 4
            elif tnorm.startswith(nq) or nq in tnorm:
                score = 3
            elif nq and nq in normalize(r.get("snippet", "")).replace(" ", ""):
                score = 1
            else:
                score = 0
            snippet = (r.get("snippet", "") or "").replace(
                "<span class=\"searchmatch\">", "").replace("</span>", "")
            seen[title] = {
                "title": title, "snippet": snippet, "score": score,
                "url": f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
            }
    out = sorted(seen.values(), key=lambda c: -c["score"])[:5]
    for c in out:
        c.pop("score", None)
    log.info("Enrich lookup %r -> %d candidates (top: %s)",
             name, len(out), out[0]["title"] if out else "—")
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


def _wiki_extract(title: str, intro_only: bool) -> str:
    params = {"action": "query", "prop": "extracts", "explaintext": 1,
              "format": "json", "titles": title}
    if intro_only:
        params["exintro"] = 1
    page = httpx.get(_WIKI, params=params, timeout=15,
                     headers={"User-Agent": _UA, "Api-User-Agent": _UA})
    page.raise_for_status()
    pages = page.json().get("query", {}).get("pages", {})
    return next(iter(pages.values()), {}).get("extract", "") or ""


def lookup_member_korean(member_name: str, group_name: str) -> str | None:
    """Best-effort: find the member's Korean name via the free Wikipedia API.
    Prefers the member's own page; otherwise scans the group article for the
    'Member (한글)' pattern. Returns None quietly on any failure — a wrong name
    would be worse than none."""
    try:
        resp = httpx.get(_WIKI, params={
            "action": "query", "list": "search", "format": "json",
            "srlimit": 5, "srsearch": f"{member_name} {group_name}",
        }, timeout=15, headers={"User-Agent": _UA, "Api-User-Agent": _UA})
        resp.raise_for_status()
        results = resp.json().get("query", {}).get("search", [])
        if not results:
            return None
        mem_l, grp_l = member_name.lower(), group_name.lower()
        member_page = next((r["title"] for r in results
                            if mem_l in r["title"].lower()), None)
        if member_page:
            text = _wiki_extract(member_page, intro_only=True)
            # Stage-name pattern first ("Karina (카리나)"), real name as fallback.
            for pat in (rf"{re.escape(member_name)}\s*\(([가-힣]{{2,}})\)",
                        r"(?:Korean|Hangul):\s*([가-힣]{2,})"):
                m = re.search(pat, text, re.IGNORECASE)
                if m:
                    log.info("Member lookup (own page): %s -> %s", member_name, m.group(1))
                    return m.group(1)
            return None
        group_page = next((r["title"] for r in results
                           if grp_l in r["title"].lower()), None)
        if not group_page:
            return None
        text = _wiki_extract(group_page, intro_only=False)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Member lookup failed for %r (%s): %s", member_name, group_name, exc)
        return None
    # In the group article, members are listed as "Yewon (예원)" or
    # "Yewon (Korean: 김예원…)".
    m = re.search(rf"{re.escape(member_name)}\s*\(\s*(?:Korean:\s*)?([가-힣]{{2,}})",
                  text, re.IGNORECASE)
    if m:
        log.info("Member lookup (group page): %s -> %s", member_name, m.group(1))
        return m.group(1)
    return None


def add_member(group_id: str, name: str, name_ko: str | None = None,
               is_current: bool = True, auto_lookup: bool = False) -> str | None:
    """Add a member to an existing group (for rosters the seed data missed, e.g.
    new line-up additions). Persists + aliases so it matches automatically next time.
    With auto_lookup, fetches missing info (Korean name) from the web."""
    row = db.query_one("SELECT name FROM groups WHERE id = ?", (group_id,))
    if not row:
        return None
    if auto_lookup and not name_ko:
        name_ko = lookup_member_korean(name, row["name"])
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
