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
                        aliases: list[str] | None = None,
                        fetch_members: bool = False) -> str:
    """Persist a user-confirmed group (source='user' so it survives reseeds).
    With fetch_members, the roster is pulled from the web straight away."""
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
    if fetch_members:
        try:
            fetch_group_members(gid)
        except Exception:  # noqa: BLE001 — enrichment must never break the add
            log.exception("Member fetch failed for new group %s", name)
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


# A member line in a Wikipedia article's plain-text Members section, e.g.
#   "Jiwoo (지우) – leader"  or  "Carmen (카르멘)"  or  "Stella"
_MEMBER_LINE_RE = re.compile(
    r"^([A-Za-z][A-Za-z .\-']{0,28}?)\s*(?:\(([가-힣·\s]{1,20})\))?(?:\s[–—-].*)?$")


def parse_members_section(text: str) -> list[dict]:
    """Extract members from a Wikipedia article's plain-text extract.
    Returns [{name, name_ko, current}] — current=False inside a Former/Past
    subsection. Conservative: only short, name-like lines are accepted."""
    sec = re.search(r"==\s*Members\s*==\n(.*?)(?=\n==[^=]|\Z)", text, re.S)
    if not sec:
        return []
    members, current = [], True
    for line in sec.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("==="):
            current = not re.search(r"(former|past)", line, re.I)
            continue
        m = _MEMBER_LINE_RE.match(line)
        if not m or len(line) > 60:
            continue
        name = m.group(1).strip()
        if not name or len(name.split()) > 3:
            continue
        members.append({"name": name, "name_ko": (m.group(2) or "").strip() or None,
                        "current": current})
        if len(members) >= 20:   # sanity cap
            break
    return members


_WIKIDATA = "https://www.wikidata.org/w/api.php"


def _clean_label(s: str) -> str:
    """Strip Wikidata disambiguators: '승민 (2000년)' -> '승민'."""
    return re.sub(r"\s*\([^)]*\)", "", s or "").strip()


def _members_from_wikidata(title: str) -> list[dict]:
    """Members via the group's Wikidata entity (P527 'has part'). This is the
    reliable source for big groups whose member list lives in the infobox,
    which plain-text article extracts strip (e.g. Stray Kids)."""
    headers = {"User-Agent": _UA, "Api-User-Agent": _UA}
    try:
        r = httpx.get(_WIKI, params={
            "action": "query", "prop": "pageprops", "ppprop": "wikibase_item",
            "titles": title, "redirects": 1, "format": "json",
        }, timeout=15, headers=headers)
        r.raise_for_status()
        page = next(iter(r.json().get("query", {}).get("pages", {}).values()), {})
        qid = page.get("pageprops", {}).get("wikibase_item")
        if not qid:
            return []
        r2 = httpx.get(_WIKIDATA, params={
            "action": "wbgetentities", "ids": qid, "props": "claims", "format": "json",
        }, timeout=15, headers=headers)
        r2.raise_for_status()
        claims = r2.json().get("entities", {}).get(qid, {}).get("claims", {}).get("P527", [])
        mids: list[tuple[str, bool]] = []
        for c in claims:
            v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
            if isinstance(v, dict) and v.get("id"):
                # An end-time qualifier (P582) marks a former member.
                mids.append((v["id"], "P582" in c.get("qualifiers", {})))
        if not mids:
            return []
        r3 = httpx.get(_WIKIDATA, params={
            "action": "wbgetentities", "ids": "|".join(m for m, _ in mids[:50]),
            "props": "labels|aliases", "languages": "en|ko", "format": "json",
        }, timeout=15, headers=headers)
        r3.raise_for_status()
        entities = r3.json().get("entities", {})
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Wikidata members lookup failed for %r: %s", title, exc)
        return []

    out = []
    for mid, former in mids:
        e = entities.get(mid, {})
        labels = e.get("labels", {})
        en = _clean_label(labels.get("en", {}).get("value", ""))
        ko = _clean_label(labels.get("ko", {}).get("value", ""))
        name = en or ko
        if not name:
            continue
        name_ko = ko if (ko and re.search(r"[가-힣]", ko) and ko != name) else None
        aliases = []
        for lang_aliases in e.get("aliases", {}).values():
            for a in lang_aliases:
                for part in a.get("value", "").split("|"):
                    part = _clean_label(part)
                    if part and part not in (name, name_ko) and part not in aliases:
                        aliases.append(part)
        out.append({"name": name, "name_ko": name_ko, "current": not former,
                    "aliases": aliases[:6]})
    return out


def lookup_group_members(group_name: str) -> list[dict]:
    """Best-effort: members of a group — structured Wikidata first, then the
    Wikipedia article's Members section as a fallback for smaller groups.

    Tries each name-matching search result until one yields members: the top
    hit can be a namesake (e.g. 'Fifty-Fifty', the coin-toss article) while the
    real page is 'Fifty Fifty (group)'."""
    results = search_group(group_name)   # already ranked best-title-first
    nq = normalize(group_name).replace(" ", "")
    for cand in results[:4]:
        title = cand["title"]
        if nq not in normalize(title).replace(" ", ""):
            continue                     # not the group's own page
        members = _members_from_wikidata(title)
        if not members:
            try:
                members = parse_members_section(_wiki_extract(title, intro_only=False))
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("Members lookup failed for %r (%s): %s", group_name, title, exc)
                members = []
        if members:
            log.info("Members lookup %r (%s) -> %d member(s)",
                     group_name, title, len(members))
            return members
    return []


def fetch_group_members(group_id: str) -> int:
    """Sync a group's roster from the web: add missing members and update the
    current/former flag of existing ones (line-up changes). Returns the number
    of members added. Never removes anyone."""
    row = db.query_one("SELECT name FROM groups WHERE id = ?", (group_id,))
    if not row:
        return 0
    found = lookup_group_members(row["name"])
    if not found:
        return 0

    # Existing members by alias, plus compact forms so 'Lee Chae-young',
    # 'Lee Chaeyoung' and 'Chaeyoung' dedupe to the same person.
    alias_to_mid: dict[str, str] = {}
    for r in db.query(
            "SELECT a.alias, a.entity_id FROM aliases a "
            "JOIN group_members gm ON gm.member_id = a.entity_id "
            "WHERE gm.group_id = ? AND a.entity_type = 'member'", (group_id,)):
        alias_to_mid[r["alias"]] = r["entity_id"]
    compact_to_mid = {a.replace(" ", ""): m for a, m in alias_to_mid.items()}

    def _existing(m: dict) -> str | None:
        for cand in (m["name"], m.get("name_ko")):
            if not cand:
                continue
            key = normalize(cand)
            if key in alias_to_mid:
                return alias_to_mid[key]
            compact = key.replace(" ", "")
            if compact in compact_to_mid:
                return compact_to_mid[compact]
            if len(compact) >= 4:
                for ec, mid in compact_to_mid.items():
                    if len(ec) >= 4 and (compact in ec or ec in compact):
                        return mid
        return None

    current_before = [r["member_id"] for r in db.query(
        "SELECT member_id FROM group_members WHERE group_id=? AND is_current=1",
        (group_id,))]

    added = changed = 0
    matched: set[str] = set()
    for m in found:
        mid = _existing(m)
        if mid:
            matched.add(mid)
            # Keep the membership status in step with the web (rebuilt
            # line-ups: e.g. FIFTY FIFTY's original members became former).
            cur = db.query_one(
                "SELECT is_current FROM group_members WHERE group_id=? AND member_id=?",
                (group_id, mid))
            want = 1 if m["current"] else 0
            if cur and cur["is_current"] != want:
                db.execute(
                    "UPDATE group_members SET is_current=? WHERE group_id=? AND member_id=?",
                    (want, group_id, mid))
                changed += 1
            continue
        mid = add_member(group_id, m["name"], m["name_ko"], is_current=m["current"])
        if mid:
            added += 1
            for al in m.get("aliases", []):
                db.execute(
                    "INSERT OR IGNORE INTO aliases(entity_type,entity_id,alias,alias_raw)"
                    " VALUES('member',?,?,?)", (mid, normalize(al), al))

    # Wikidata often lists ONLY the current line-up (e.g. FIFTY FIFTY after its
    # relaunch). When the web roster is plausibly complete, members we have as
    # current who aren't in it have left — mark them former (never removed).
    if len(found) >= len(current_before):
        for mid in current_before:
            if mid not in matched:
                db.execute(
                    "UPDATE group_members SET is_current=0 WHERE group_id=? AND member_id=?",
                    (group_id, mid))
                changed += 1

    if added or changed:
        reload_index()
        log.info("Roster sync for %s: %d added, %d status change(s)",
                 row["name"], added, changed)
    return added


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
