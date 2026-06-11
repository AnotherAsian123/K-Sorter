"""Database management operations (browse / edit groups & members).

All edits persist to SQLite and rebuild the match index. User edits to seed rows
are kept on reseed only for user-created rows; renames/aliases on seed groups are
additive (aliases) so they survive, and name edits persist until the next reseed.

Renames also migrate already-sorted content: the old destination folder is moved
under the new name (journaled, so it's undoable like any other move).
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from . import database as db
from . import engine, mover
from .config import settings
from .logging_setup import get_logger
from .matcher import reload_index
from .normalize import normalize

log = get_logger("ksorter.manage")


def _add_alias(etype: str, eid: str, raw: str | None) -> None:
    if raw and raw.strip():
        db.execute(
            "INSERT OR IGNORE INTO aliases(entity_type, entity_id, alias, alias_raw)"
            " VALUES(?,?,?,?)", (etype, eid, normalize(raw), raw))


# ---- rename folder migration ------------------------------------------------
def _dest_root() -> Path | None:
    """Best-known destination root: the /destination mount (env) or the last
    destination a sort/audit ran against."""
    cand = settings.dest_default or db.get_meta("last_dest", "") or ""
    p = Path(cand) if cand else None
    return p if (p and p.is_dir()) else None


def _display(name: str | None, name_ko: str | None) -> str:
    lang, _template = engine.get_naming()
    return (name_ko if (lang == "ko" and name_ko) else name) or ""


def _migrate_dir(old_dir: Path, new_dir: Path, group_label: str) -> int:
    """Move everything under old_dir to new_dir (rename-fast when possible,
    per-file merge otherwise). Returns number of journal entries written."""
    if old_dir == new_dir or not old_dir.is_dir():
        return 0
    batch = "rename-" + uuid.uuid4().hex[:8]

    # Fast path: target doesn't exist yet -> rename the whole tree at once.
    if not new_dir.exists():
        try:
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            os.rename(old_dir, new_dir)
            engine._journal(batch, str(old_dir), str(new_dir), "move", "rename",
                            old_dir.name, group_label, None)
            log.info("RENAME migrate: %s -> %s", old_dir, new_dir)
            return 1
        except OSError:
            pass  # cross-device or other -> fall through to per-file merge

    moved = 0
    for f in sorted(p for p in old_dir.rglob("*") if p.is_file()):
        rel = f.relative_to(old_dir)
        res = mover.safe_move(f, new_dir / rel)
        if res.status == "moved" and res.method != "noop":
            engine._journal(batch, str(f), res.dest,
                            "dedupe" if res.method == "dedupe" else "move",
                            res.method, f.name, group_label, None)
            moved += 1
    # Tidy now-empty directories left behind.
    for d in sorted((p for p in old_dir.rglob("*") if p.is_dir()), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    try:
        old_dir.rmdir()
    except OSError:
        pass
    log.info("RENAME migrate (merge): %s -> %s, %d file(s)", old_dir, new_dir, moved)
    return moved


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
    # The group's current names are protected: deleting them would break
    # matching for the very name shown on the folder.
    protected = {normalize(g["name"] or ""), normalize(g["name_ko"] or "")} - {""}
    aliases = [
        {"raw": a["alias_raw"], "key": a["alias"], "protected": a["alias"] in protected}
        for a in db.query(
            "SELECT alias, alias_raw FROM aliases WHERE entity_type='group' "
            "AND entity_id=? ORDER BY alias_raw", (gid,))
    ]
    return {"group": dict(g), "members": [dict(m) for m in members],
            "aliases": aliases}


# ---- group edits ----------------------------------------------------------
def rename_group(gid: str, name: str, name_ko: str | None) -> None:
    old = db.query_one("SELECT name, name_ko FROM groups WHERE id=?", (gid,))
    db.execute("UPDATE groups SET name=?, name_ko=? WHERE id=?",
               (name.strip(), (name_ko or "").strip() or None, gid))
    _add_alias("group", gid, name)
    _add_alias("group", gid, name_ko)
    reload_index()
    # Migrate already-sorted content to the new folder name.
    root = _dest_root()
    if old and root:
        old_disp = mover.sanitize_component(_display(old["name"], old["name_ko"]))
        new_disp = mover.sanitize_component(_display(name, name_ko))
        if old_disp and new_disp and old_disp != new_disp:
            _migrate_dir(root / old_disp, root / new_disp, name.strip())


def add_group_alias(gid: str, alias: str) -> None:
    _add_alias("group", gid, alias)
    reload_index()


def remove_group_alias(gid: str, alias_key: str) -> None:
    """Delete one alias (by normalized key) — and any learned correction that
    created it, so it doesn't come back via the corrections table."""
    db.execute("DELETE FROM aliases WHERE entity_type='group' AND entity_id=? AND alias=?",
               (gid, alias_key))
    db.execute("DELETE FROM corrections WHERE entity_type='group' AND entity_id=? AND pattern=?",
               (gid, alias_key))
    reload_index()
    log.info("Removed alias %r from group %s", alias_key, gid)


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
    old = db.query_one("SELECT stage_name, stage_name_ko FROM members WHERE id=?", (mid,))
    db.execute("UPDATE members SET stage_name=?, stage_name_ko=? WHERE id=?",
               (name.strip(), (name_ko or "").strip() or None, mid))
    _add_alias("member", mid, name)
    _add_alias("member", mid, name_ko)
    reload_index()
    # Migrate the member's folder inside every group they belong to.
    root = _dest_root()
    if old and root:
        old_disp = mover.sanitize_component(_display(old["stage_name"], old["stage_name_ko"]))
        new_disp = mover.sanitize_component(_display(name, name_ko))
        if old_disp and new_disp and old_disp != new_disp:
            for r in db.query(
                    "SELECT g.name, g.name_ko FROM group_members gm "
                    "JOIN groups g ON g.id = gm.group_id WHERE gm.member_id=?", (mid,)):
                gdisp = mover.sanitize_component(_display(r["name"], r["name_ko"]))
                if gdisp:
                    _migrate_dir(root / gdisp / old_disp, root / gdisp / new_disp,
                                 r["name"])


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
