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
from .normalize import normalize, tokens_for_match
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


def _flag_member(vf, group: EntityRef, member: EntityRef, score: int,
                 location: str) -> engine.PlanItem:
    """Right group, but a solo fancam sitting outside its member's folder."""
    item = engine.PlanItem(
        id=uuid.uuid4().hex[:12],
        source=str(vf.path),
        filename=vf.path.name,
        status="confirm",
        confidence=score,
        reason="possible miscategorisation (member)",
        group_id=group.id,
        group_name=group.name,
        group_candidates=[{"id": group.id, "name": group.name,
                           "name_ko": group.name_ko, "score": 100}],
        member_candidates=engine._roster_candidates(group.id, [(member, score)]),
        member_id=member.id,
        member_name=member.name,
        current_location=location,
    )
    item.help = (f"Filed under “{location}”, but this looks like a solo video of "
                 f"{member.name}. Confirm to move it into the member's folder — "
                 f"or Skip to keep it where it is.")
    return item


def _flag_hashtag(vf, current_group, member_folder, res, location) -> engine.PlanItem:
    """File whose hashtags the database doesn't recognise — placement can't be
    trusted (likely a group/member that isn't in the database yet)."""
    item = _flag(vf, current_group, member_folder, res, location, "unrecognised hashtags")
    item.help = ("This filename has hashtag(s) the database doesn't recognise — "
                 "possibly a new group or member. Verify where it belongs: search "
                 "or look the group up online and add it, then Confirm — or Skip "
                 "to keep it here.")
    return item


def _flag_collab(vf, res, dest_root: Path, location: str) -> engine.PlanItem:
    """A multi-group filename needing a human decision: present the collab
    options (Special Stages + all groups / Special Stages only / one group)."""
    lang, _template = engine.get_naming()
    item = engine.PlanItem(
        id=uuid.uuid4().hex[:12],
        source=str(vf.path),
        filename=vf.path.name,
        status="confirm",
        confidence=res.confidence,
        reason="multiple group names — needs your decision",
        is_collab=True,
        group_name=" + ".join(g.name for g in res.groups),
        collab_groups=[{"id": g.id, "name": g.name} for g in res.groups],
        primary_dest=str(dest_root / engine.SPECIAL_STAGES / vf.path.name),
        replica_dests=[
            str(dest_root / engine._name(g, lang) / engine.GROUP_SUBFOLDER / vf.path.name)
            for g in res.groups
        ],
        current_location=location,
    )
    item.help = ("Multiple group names in this filename with no collab marker — "
                 "decide: Special Stages (with or without copies in each group), "
                 "or file it under one group. Skip keeps it where it is.")
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
        if not parts:
            continue
        top = parts[0]

        # _Special Stages: should be rare. Marked collabs belong here; anything
        # else is offered back to the user for a decision.
        if top == engine.SPECIAL_STAGES:
            location = engine.SPECIAL_STAGES
            if engine.get_decision(vf.path.name) == location:
                continue
            res = idx.match(vf.stem)
            if res.is_collab:
                if not res.collab_marker:
                    yield _flag_collab(vf, res, dest_root, location)
                continue
            if "hashtag" in res.reason and res.group:
                yield _flag_hashtag(vf, None, None, res, location)
                continue
            if (res.group and not res.group_ambiguous
                    and res.group_confidence >= settings.confirm_threshold):
                yield _flag(vf, None, None, res, location, "single group, not a collab")
            continue
        member_folder = (parts[1] if len(parts) >= 3
                         and parts[1] != engine.GROUP_SUBFOLDER else None)
        location = top + "/" + (member_folder or engine.GROUP_SUBFOLDER)

        # Respect earlier decisions: if the user already approved this file at
        # this location (skipped or sorted it here), don't re-flag it.
        if engine.get_decision(vf.path.name) == location:
            continue

        res = idx.match(vf.stem)
        if res.is_collab:
            # If this folder is one of the named groups (or it's a marked,
            # genuine collab) it's a plausible home — leave it at GROUP level.
            # Otherwise the file sits under an unrelated group: ask the user.
            cg = _resolve_group_folder(top)
            in_named_group = bool(cg and any(g.id == cg.id for g in res.groups))
            if not res.collab_marker and not in_named_group:
                yield _flag_collab(vf, res, dest_root, location)
                continue
            # Group placement accepted — but a solo fancam still belongs in its
            # member's folder. Resolve the member WITHIN the current group (the
            # collab match never resolves members, so do it here).
            if in_named_group and res.is_solo:
                tokens, _solo = tokens_for_match(vf.stem)
                norm_full = normalize(" ".join(tokens))
                m, m_score, m_amb, _cands = idx.match_member(norm_full, cg.id)
                if (m and not m_amb and m_score >= settings.confirm_threshold
                        and _resolve_member_folder(member_folder, cg.id) != m.id):
                    yield _flag_member(vf, cg, m, m_score, location)
            continue
        # Unrecognised hashtags = the placement can't be trusted (the match may
        # rest on a learned alias or a fuzzy guess) — ask the user.
        if "hashtag" in res.reason and res.group:
            yield _flag_hashtag(vf, _resolve_group_folder(top), member_folder,
                                res, location)
            continue
        # Gate on GROUP certainty (a solo video with an unresolved member is
        # still a confident group match — we can verify the folder).
        if not res.group or res.group_ambiguous:
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
