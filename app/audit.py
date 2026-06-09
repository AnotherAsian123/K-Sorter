"""Destination integrity audit: re-check already-sorted videos and flag likely
miscategorisations so the user can fix them.

For each sorted file we read its CURRENT folder (group/member), re-run the
matcher on the filename, and — only when the matcher is confident — flag any
disagreement. Flagged items are normal review items, so the existing resolver
moves them to the right place (journaled + undoable).
"""
from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

from . import engine
from .config import settings
from .logging_setup import get_logger
from .matcher import EntityRef, get_index
from .normalize import normalize
from .scanner import scan

log = get_logger("ksorter.audit")


def _resolve_group_folder(name: str) -> EntityRef | None:
    """Map a destination folder name back to a group (works for EN or KO names)."""
    idx = get_index()
    ids = idx.group_alias_to_ids.get(normalize(name))
    if not ids:
        return None
    uniq = list(dict.fromkeys(ids))
    return idx.groups.get(uniq[0]) if len(uniq) == 1 else None


def _resolve_member_folder(name: str | None, group_id: str) -> str | None:
    if not name:
        return None
    idx = get_index()
    members = set(idx.group_to_members.get(group_id, []))
    for mid in members:
        m = idx.members.get(mid)
        if m and normalize(m.name) == normalize(name):
            return mid
    return None


def _flag(vf, current_group, member_folder, res, location, reason) -> engine.PlanItem:
    cands = [{"id": res.group.id, "name": res.group.name,
              "name_ko": res.group.name_ko, "score": res.group_confidence}]
    if current_group and current_group.id != res.group.id:
        cands.append({"id": current_group.id, "name": current_group.name,
                      "name_ko": current_group.name_ko, "score": 0})
    item = engine.PlanItem(
        id=uuid.uuid4().hex[:12],
        source=str(vf.path),
        filename=vf.path.name,
        status="confirm",
        confidence=res.group_confidence,
        reason=f"possible miscategorisation ({reason})",
        is_collab=False,
        group_id=res.group.id,
        group_name=res.group.name,
        group_candidates=cands,
        member_candidates=engine._roster_candidates(
            res.group.id, [(res.member, res.confidence)] if res.member else []),
        current_location=location,
    )
    if res.member:
        item.member_id, item.member_name = res.member.id, res.member.name
    suggestion = res.group.name + (f" / {res.member.name}" if res.member else "")
    item.help = (f"Filed under “{location}”, but the filename looks like "
                 f"{suggestion}. Pick the correct group/member and Confirm to move "
                 f"it — or Skip to keep it where it is.")
    return item


def audit_destination(dest_root: str | Path,
                      on_scan: Callable[[], None] | None = None) -> Iterator[engine.PlanItem]:
    dest_root = Path(dest_root)
    idx = get_index()
    for vf in scan(dest_root):
        if on_scan:
            on_scan()
        try:
            parts = vf.path.relative_to(dest_root).parts
        except ValueError:
            continue
        if not parts or parts[0] == engine.SPECIAL_STAGES:
            continue  # collab folder — can't verify a single group
        top = parts[0]
        member_folder = (parts[1] if len(parts) >= 3
                         and parts[1] != engine.GROUP_SUBFOLDER else None)
        location = top + "/" + (member_folder or engine.GROUP_SUBFOLDER)

        res = idx.match(vf.stem)
        # Gate on GROUP certainty (a solo video with an unresolved member is
        # still a confident group match — we can verify the folder).
        if not res.group or res.is_collab or res.group_ambiguous:
            continue
        if res.group_confidence < settings.confirm_threshold:
            continue

        current_group = _resolve_group_folder(top)
        # 1) Wrong group folder.
        if current_group is None or res.group.id != current_group.id:
            yield _flag(vf, current_group, member_folder, res, location, "group")
            continue
        # 2) Right group, but wrong/missing member folder for a solo video.
        if res.is_solo and res.member:
            cur_mid = _resolve_member_folder(member_folder, current_group.id)
            if cur_mid != res.member.id:
                yield _flag(vf, current_group, member_folder, res, location, "member")
