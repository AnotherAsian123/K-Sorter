"""Sorting orchestration: turn matches into a safe, routed plan and apply it.

Routing (plan.md §2, §5):
  auto     -> confident & unambiguous: sorted automatically
  confirm  -> uncertain/ambiguous: ask the user a precise question
  manual   -> no usable match: parked for a human
"""
from __future__ import annotations

import csv
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import database as db
from . import mover
from .config import settings
from .logging_setup import get_logger
from .matcher import EntityRef, MatchResult, get_index, reload_index
from .normalize import FILLER, normalize, tokens_for_match
from .scanner import VideoFile

log = get_logger("ksorter")
review_log = get_logger("ksorter.review")
manual_log = get_logger("ksorter.manual")

SPECIAL_STAGES = "_Special Stages"
GROUP_SUBFOLDER = "Group"


# ---- naming configuration -------------------------------------------------
def get_naming() -> tuple[str, str]:
    lang = db.get_meta("naming_language", "en") or "en"
    template = db.get_meta("naming_template", "nested") or "nested"
    return lang, template


def set_naming(language: str, template: str) -> None:
    db.set_meta("naming_language", language)
    db.set_meta("naming_template", template)


def _name(entity: EntityRef, lang: str) -> str:
    raw = entity.name_ko if (lang == "ko" and entity.name_ko) else entity.name
    return mover.sanitize_component(raw)


def _solo_dir(root: Path, group: EntityRef, member: EntityRef,
              lang: str, template: str) -> Path:
    g, m = _name(group, lang), _name(member, lang)
    if template == "flat":
        return root / mover.sanitize_component(f"{g} - {m}")
    if template not in ("nested", "flat"):
        # custom template, e.g. "{group}/{member}" or "{group} - {member}"
        rendered = template.replace("{group}", g).replace("{member}", m)
        parts = [mover.sanitize_component(p) for p in rendered.split("/") if p.strip()]
        return root.joinpath(*parts) if parts else root / g / m
    return root / g / m  # nested default


# ---- plan item ------------------------------------------------------------
@dataclass
class PlanItem:
    id: str
    source: str
    filename: str
    status: str                       # auto | confirm | manual
    confidence: int
    reason: str
    help: str = ""                    # plain-language "what to do" for the user
    is_collab: bool = False
    group_id: str | None = None
    group_name: str | None = None
    member_id: str | None = None
    member_name: str | None = None
    primary_dest: str | None = None
    replica_dests: list[str] = field(default_factory=list)
    group_candidates: list[dict] = field(default_factory=list)
    member_candidates: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _route(res: MatchResult) -> str:
    if res.group is None:
        return "manual"
    if res.is_collab:
        return "auto"
    if res.ambiguous or (res.is_solo and res.member is None):
        return "confirm"
    if res.confidence >= settings.auto_threshold:
        return "auto"
    if res.confidence >= settings.confirm_threshold:
        return "confirm"
    return "manual"


def _roster_candidates(group_id: str, fuzzy: list[tuple[EntityRef, int]]) -> list[dict]:
    """Full member roster for a group, with any fuzzy guesses scored and sorted
    to the top — so the confirm dropdown is always usable, not just when a fuzzy
    guess existed."""
    idx = get_index()
    scores = {e.id: s for e, s in fuzzy}
    out = []
    for mid in idx.group_to_members.get(group_id, []):
        m = idx.members.get(mid)
        if m:
            out.append({"id": m.id, "name": m.name, "name_ko": m.name_ko,
                        "score": scores.get(m.id, 0),
                        "current": idx.gm_current.get((group_id, mid), True)})
    # Best fuzzy guesses first, then current members, then former.
    out.sort(key=lambda c: (-c["score"], not c["current"], c["name"]))
    return out


def _cands(pairs: list[tuple[EntityRef, int]]) -> list[dict]:
    return [{"id": e.id, "name": e.name, "name_ko": e.name_ko, "score": s}
            for e, s in pairs]


def build_plan_item(vf: VideoFile, dest_root: Path) -> PlanItem:
    res = get_index().match(vf.stem)
    status = _route(res)
    item = PlanItem(
        id=uuid.uuid4().hex[:12],
        source=str(vf.path),
        filename=vf.path.name,
        status=status,
        confidence=res.confidence,
        reason=res.reason,
        is_collab=res.is_collab,
        group_candidates=_cands(res.group_candidates),
        member_candidates=_cands(res.member_candidates),
    )
    lang, template = get_naming()

    if res.is_collab:
        item.group_name = " + ".join(g.name for g in res.groups)
        item.primary_dest = str(dest_root / SPECIAL_STAGES / vf.path.name)
        item.replica_dests = [
            str(dest_root / _name(g, lang) / GROUP_SUBFOLDER / vf.path.name)
            for g in res.groups
        ]
        return item

    if res.group:
        item.group_id = res.group.id
        item.group_name = res.group.name
    if res.member:
        item.member_id = res.member.id
        item.member_name = res.member.name

    if status == "auto":
        if res.member:
            item.primary_dest = str(_solo_dir(dest_root, res.group, res.member, lang, template) / vf.path.name)
        else:
            item.primary_dest = str(dest_root / _name(res.group, lang) / GROUP_SUBFOLDER / vf.path.name)
        return item

    # --- explain (in plain language) why this one needs a human, and make the
    #     confirm UI immediately actionable ---
    if status == "manual":
        item.help = ("We couldn't match a group from the filename. "
                     "Search the database below, or look the group up online.")
    elif res.ambiguous and len(item.group_candidates) > 1:
        item.help = "More than one group could match — pick the right one."
    elif res.is_solo and res.member is None and res.group:
        # Group is known; just need the member. Offer the FULL roster.
        item.help = (f"Found {res.group.name}, but couldn't tell which member. "
                     f"Pick the member — or choose “group folder” if it's the whole group.")
        item.member_candidates = _roster_candidates(res.group.id, res.member_candidates)
    else:
        item.help = "Low confidence — please confirm the group (and member)."
    return item


# ---- applying -------------------------------------------------------------
def apply_item(item: PlanItem, batch_id: str) -> dict:
    """Execute a single (already-resolved) plan item. Returns a result dict."""
    if item.is_collab:
        primary = mover.safe_move(Path(item.source), Path(item.primary_dest),
                                  settings.verify_checksum)
        if primary.status != "moved":
            return {"id": item.id, "status": primary.status, "reason": primary.reason}
        _journal(batch_id, item.source, primary.dest, "move", primary.method,
                 item.filename, item.group_name, None)
        for rep in item.replica_dests:
            r = mover.replicate(Path(primary.dest), Path(rep))
            if r.status == "moved":
                _journal(batch_id, primary.dest, r.dest, "replica", r.method,
                         item.filename, _group_from_dest(r.dest), None)
        return {"id": item.id, "status": "moved", "dest": primary.dest,
                "replicas": len(item.replica_dests)}

    if not item.primary_dest:
        return {"id": item.id, "status": "error", "reason": "no destination resolved"}
    result = mover.safe_move(Path(item.source), Path(item.primary_dest),
                             settings.verify_checksum)
    if result.status == "moved":
        _journal(batch_id, item.source, result.dest, "move", result.method,
                 item.filename, item.group_name, item.member_name)
    return {"id": item.id, "status": result.status, "dest": result.dest,
            "reason": result.reason}


def _group_from_dest(dest: str) -> str:
    """For a collab replica path <root>/<Group>/Group/<file>, the group folder."""
    parts = Path(dest).parts
    return parts[-3] if len(parts) >= 3 else ""


def _journal(batch_id, source, dest, action, method,
             filename=None, group_name=None, member_name=None) -> None:
    db.execute(
        "INSERT INTO move_journal(batch_id, source, dest, action, method,"
        " filename, group_name, member_name) VALUES(?,?,?,?,?,?,?,?)",
        (batch_id, source, dest, action, method, filename, group_name, member_name))


# ---- undo -----------------------------------------------------------------
def list_batches(limit: int = 20) -> list[dict]:
    rows = db.query(
        "SELECT batch_id, COUNT(*) n, MIN(created_at) at, MAX(created_at) at_end, "
        "SUM(undone) undone, COUNT(DISTINCT member_name) nmembers "
        "FROM move_journal GROUP BY batch_id ORDER BY at_end DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        d = dict(r)
        d["groups"] = [g["group_name"] for g in db.query(
            "SELECT DISTINCT group_name FROM move_journal WHERE batch_id=? "
            "AND group_name IS NOT NULL AND group_name != '' "
            "ORDER BY group_name", (d["batch_id"],))]
        out.append(d)
    return out


def get_batch_moves(batch_id: str) -> list[dict]:
    """All moves in a batch, with friendly labels for the expandable table."""
    rows = db.query(
        "SELECT filename, group_name, member_name, action, method, undone, "
        "source, dest FROM move_journal WHERE batch_id=? ORDER BY id", (batch_id,))
    out = []
    for r in rows:
        d = dict(r)
        # Fall back to the file/folder names if older rows lack labels.
        d["filename"] = d["filename"] or Path(d["source"]).name
        if not d["group_name"]:
            parts = Path(d["dest"]).parts
            d["group_name"] = parts[-3] if len(parts) >= 3 else ""
        out.append(d)
    return out


def undo_batch(batch_id: str) -> dict:
    rows = db.query(
        "SELECT id, source, dest, action FROM move_journal "
        "WHERE batch_id=? AND undone=0 ORDER BY id DESC", (batch_id,))
    ok = fail = 0
    for r in rows:
        if mover.undo_one(r["source"], r["dest"], r["action"]):
            db.execute("UPDATE move_journal SET undone=1 WHERE id=?", (r["id"],))
            ok += 1
        else:
            fail += 1
    log.info("Undo batch %s: %d restored, %d failed", batch_id, ok, fail)
    return {"batch_id": batch_id, "restored": ok, "failed": fail}


# ---- dry-run export -------------------------------------------------------
def export_plan_csv(items: list[PlanItem]) -> Path:
    out = settings.logs_dir / "dry_run_plan.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["status", "confidence", "group", "member", "reason",
                    "source", "proposed_destination"])
        for it in items:
            w.writerow([it.status, it.confidence, it.group_name or "",
                        it.member_name or "", it.reason, it.source,
                        it.primary_dest or ""])
    log.info("Dry-run plan exported: %s (%d rows)", out, len(items))
    return out


# ---- learning from corrections (plan.md §10) ------------------------------
def learn_correction(filename_stem: str, group_id: str,
                     member_id: str | None = None) -> None:
    """Teach K-Sorter from your fix: any unknown token in the filename becomes a
    new alias for the entity you chose, so it matches automatically next time."""
    idx = get_index()
    tokens, _ = tokens_for_match(filename_stem)
    known = set()
    for r in db.query("SELECT alias FROM aliases"):
        known.update(r["alias"].split())
    leftover = [t for t in tokens if t not in known and t not in FILLER and len(t) > 1]

    target_type = "member" if member_id else "group"
    target_id = member_id or group_id
    for tok in leftover:
        db.execute(
            "INSERT OR IGNORE INTO aliases(entity_type,entity_id,alias,alias_raw)"
            " VALUES(?,?,?,?)", (target_type, target_id, normalize(tok), tok))
        db.execute(
            "INSERT OR REPLACE INTO corrections(pattern,entity_type,entity_id,group_id)"
            " VALUES(?,?,?,?)", (normalize(tok), target_type, target_id, group_id))
    if leftover:
        log.info("Learned %d alias(es) for %s %s: %s",
                 len(leftover), target_type, target_id, leftover)
        reload_index()
