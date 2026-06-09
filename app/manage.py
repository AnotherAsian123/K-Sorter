"""Database management operations (browse / edit groups & members).

All edits persist to SQLite and rebuild the match index. User edits to seed rows
are kept on reseed only for user-created rows; renames/aliases on seed groups are
additive (aliases) so they survive, and name edits persist until the next reseed.
"""
from __future__ import annotations

from . import database as db
from .logging_setup import get_logger
from .matcher import reload_index
from .normalize import normalize

log = get_logger("ksorter.manage")


def _add_alias(etype: str, eid: str, raw: str | None) -> None:
    if raw and raw.strip():
        db.execute(
            "INSERT OR IGNORE INTO aliases(entity_type, entity_id, alias, alias_raw)"
            " VALUES(?,?,?,?)", (etype, eid, normalize(raw), raw))


# ---- read -----------------------------------------------------------------
def list_groups(q: str = "", limit: int = 300) -> list[dict]:
    like = f"%{q.strip()}%"
    if q.strip():
        rows = db.query(
            "SELECT id, name, name_ko, is_active, source, "
            "(SELECT COUNT(*) FROM group_members gm WHERE gm.group_id = g.id) nmem "
            "FROM groups g WHERE name LIKE ? OR name_ko LIKE ? "
            "ORDER BY name LIMIT ?", (like, like, limit))
    else:
        rows = db.query(
            "SELECT id, name, name_ko, is_active, source, "
            "(SELECT COUNT(*) FROM group_members gm WHERE gm.group_id = g.id) nmem "
            "FROM groups g ORDER BY name LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def get_group(gid: str) -> dict | None:
    g = db.query_one("SELECT * FROM groups WHERE id = ?", (gid,))
    if not g:
        return None
    members = db.query(
        "SELECT m.id, m.stage_name, m.stage_name_ko, gm.is_current "
        "FROM group_members gm JOIN members m ON m.id = gm.member_id "
        "WHERE gm.group_id = ? ORDER BY gm.is_current DESC, m.stage_name", (gid,))
    aliases = db.query(
        "SELECT alias_raw FROM aliases WHERE entity_type='group' AND entity_id=? "
        "ORDER BY alias_raw", (gid,))
    return {"group": dict(g), "members": [dict(m) for m in members],
            "aliases": [a["alias_raw"] for a in aliases]}


# ---- group edits ----------------------------------------------------------
def rename_group(gid: str, name: str, name_ko: str | None) -> None:
    db.execute("UPDATE groups SET name=?, name_ko=? WHERE id=?",
               (name.strip(), (name_ko or "").strip() or None, gid))
    _add_alias("group", gid, name)
    _add_alias("group", gid, name_ko)
    reload_index()


def add_group_alias(gid: str, alias: str) -> None:
    _add_alias("group", gid, alias)
    reload_index()


def set_group_active(gid: str, active: bool) -> None:
    db.execute("UPDATE groups SET is_active=? WHERE id=?", (1 if active else 0, gid))
    reload_index()


def delete_group(gid: str) -> None:
    db.execute("DELETE FROM aliases WHERE entity_type='group' AND entity_id=?", (gid,))
    db.execute("DELETE FROM group_members WHERE group_id=?", (gid,))
    db.execute("DELETE FROM groups WHERE id=?", (gid,))
    reload_index()
    log.info("Deleted group %s", gid)


# ---- member edits ---------------------------------------------------------
def rename_member(mid: str, name: str, name_ko: str | None) -> None:
    db.execute("UPDATE members SET stage_name=?, stage_name_ko=? WHERE id=?",
               (name.strip(), (name_ko or "").strip() or None, mid))
    _add_alias("member", mid, name)
    _add_alias("member", mid, name_ko)
    reload_index()


def set_member_current(gid: str, mid: str, current: bool) -> None:
    db.execute("UPDATE group_members SET is_current=? WHERE group_id=? AND member_id=?",
               (1 if current else 0, gid, mid))
    reload_index()


def remove_member(gid: str, mid: str) -> None:
    db.execute("DELETE FROM group_members WHERE group_id=? AND member_id=?", (gid, mid))
    # If the member now belongs to no group, clean them up entirely.
    if not db.query_one("SELECT 1 FROM group_members WHERE member_id=?", (mid,)):
        db.execute("DELETE FROM aliases WHERE entity_type='member' AND entity_id=?", (mid,))
        db.execute("DELETE FROM members WHERE id=?", (mid,))
    reload_index()
