"""Seed / refresh the K-pop database from the open CC0 kpopnet.json dataset.

Refresh happens only on container restart or when the user clicks
"Update Database" (plan.md §4) — never on every sort, for speed.

kpopnet.json schema (relevant fields):
    Group { id, name, name_original, agency_name, name_alias?, debut_date?,
            disband_date?, members: [{ idol_id, current, roles? }], parent_id? }
    Idol  { id, name, name_original, real_name, name_alias?, birth_date, groups[] }
"""
from __future__ import annotations

import json
import time
from datetime import datetime

import httpx

from . import database as db
from .config import settings
from .logging_setup import get_logger
from .normalize import normalize

log = get_logger("ksorter.seed")


def _split_aliases(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


def _load_data(force_download: bool) -> dict:
    """Return the parsed dataset, downloading + caching it if needed."""
    cache = settings.seed_cache
    if force_download or not cache.exists():
        log.info("Downloading seed dataset from %s", settings.seed_url)
        resp = httpx.get(settings.seed_url, follow_redirects=True, timeout=60)
        resp.raise_for_status()
        cache.write_bytes(resp.content)
        log.info("Cached seed dataset (%d bytes) at %s", len(resp.content), cache)
    return json.loads(cache.read_text(encoding="utf-8"))


def _add_alias(rows: list, entity_type: str, entity_id: str, raw: str | None) -> None:
    for alias_raw in _split_aliases(raw):
        norm = normalize(alias_raw)
        if norm:
            rows.append((entity_type, entity_id, norm, alias_raw))


def refresh_seed(force_download: bool = False, sync_rosters: bool = True) -> dict:
    """(Re)build the database from the dataset. Returns row counts.

    User-confirmed rows (source='user') are preserved — we only replace
    seed-sourced rows so your confirmations and corrections survive a refresh.
    """
    data = _load_data(force_download)
    idols = data.get("idols") or data.get("idol") or []
    groups = data.get("groups") or data.get("group") or []
    log.info("Parsed dataset: %d idols, %d groups", len(idols), len(groups))

    idol_by_id = {i["id"]: i for i in idols}

    group_rows, member_rows, gm_rows, alias_rows = [], [], [], []
    seen_members: set[str] = set()

    for g in groups:
        gid = g["id"]
        is_active = 0 if g.get("disband_date") else 1
        group_rows.append((
            gid, g.get("name") or gid, g.get("name_original"),
            g.get("agency_name"), g.get("debut_date"), g.get("disband_date"),
            is_active, g.get("parent_id"), "seed", 1,
        ))
        _add_alias(alias_rows, "group", gid, g.get("name"))
        _add_alias(alias_rows, "group", gid, g.get("name_original"))
        _add_alias(alias_rows, "group", gid, g.get("name_alias"))

        for gm in g.get("members", []):
            iid = gm["idol_id"]
            gm_rows.append((gid, iid, 1 if gm.get("current") else 0, gm.get("roles")))
            if iid in seen_members:
                continue
            seen_members.add(iid)
            idol = idol_by_id.get(iid)
            if not idol:
                continue
            member_rows.append((
                iid, idol.get("name") or iid, idol.get("name_original"),
                idol.get("real_name"), idol.get("birth_date"),
            ))
            _add_alias(alias_rows, "member", iid, idol.get("name"))
            _add_alias(alias_rows, "member", iid, idol.get("name_original"))
            _add_alias(alias_rows, "member", iid, idol.get("name_alias"))

    with db._lock:
        conn = db.get_conn()
        conn.execute("BEGIN")
        try:
            # Replace seed rows only; keep user-confirmed groups + their links.
            conn.execute("DELETE FROM aliases WHERE entity_id IN "
                         "(SELECT id FROM groups WHERE source='seed')")
            conn.execute("DELETE FROM group_members WHERE group_id IN "
                         "(SELECT id FROM groups WHERE source='seed')")
            conn.execute("DELETE FROM groups WHERE source='seed'")
            # Members are shared; safe to upsert and leave orphans harmless.
            conn.executemany(
                "INSERT OR REPLACE INTO groups"
                "(id,name,name_ko,agency,debut_date,disband_date,is_active,parent_id,source,confirmed)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)", group_rows)
            conn.executemany(
                "INSERT OR REPLACE INTO members"
                "(id,stage_name,stage_name_ko,real_name,birth_date) VALUES(?,?,?,?,?)",
                member_rows)
            conn.executemany(
                "INSERT OR REPLACE INTO group_members"
                "(group_id,member_id,is_current,roles) VALUES(?,?,?,?)", gm_rows)
            conn.executemany(
                "INSERT OR IGNORE INTO aliases"
                "(entity_type,entity_id,alias,alias_raw) VALUES(?,?,?,?)", alias_rows)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            log.exception("Seed import failed; rolled back")
            raise

    db.set_meta("last_seed_status", "ok")
    db.set_meta("last_seed_at", datetime.now().strftime("%Y-%m-%d %H:%M"))

    # Sync every group's roster from the web: the dataset is a snapshot, so it
    # misses new members and rebuilt line-ups (e.g. tripleS growing to 24, or
    # FIFTY FIFTY's 2024 relaunch). Adds missing members and updates the
    # current/former flags; never removes anyone. Runs in the background
    # thread that called us, throttled to stay polite.
    if sync_rosters:
        from . import enrich
        rows = db.query("SELECT id, name FROM groups ORDER BY name")
        log.info("Roster sync: checking %d group(s) against the web…", len(rows))
        for i, row in enumerate(rows, 1):
            try:
                enrich.fetch_group_members(row["id"])
            except Exception:  # noqa: BLE001 — enrichment must never break a seed
                log.exception("Roster sync failed for group %s", row["name"])
            time.sleep(0.1)
            if i % 25 == 0:
                log.info("Roster sync: %d/%d groups checked", i, len(rows))
        log.info("Roster sync complete.")

    counts = db.counts()
    log.info("Seed complete: %s", counts)
    return counts
