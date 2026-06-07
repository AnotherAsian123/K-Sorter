"""Filename -> group/member matching. The careful part (plan.md §5).

Accuracy first: exact alias matches win; fuzzy matches are scored and only
trusted above a high threshold; learned corrections override everything; ties
are reported as ambiguous so the caller can ask the user instead of guessing.

No files are ever modified here — we only read filename strings.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from . import database as db
from .normalize import normalize, tokens_for_match

# Below this fuzzy score we treat it as "no confident match" rather than guess.
FUZZY_FLOOR = 65


@dataclass
class EntityRef:
    id: str
    type: str          # 'group' | 'member'
    name: str
    name_ko: str | None = None


@dataclass
class MatchResult:
    filename: str
    is_solo: bool
    group: EntityRef | None = None
    member: EntityRef | None = None
    confidence: int = 0
    reason: str = ""
    ambiguous: bool = False
    is_collab: bool = False
    groups: list[EntityRef] = field(default_factory=list)   # all groups for a collab
    group_candidates: list[tuple[EntityRef, int]] = field(default_factory=list)
    member_candidates: list[tuple[EntityRef, int]] = field(default_factory=list)


class MatchIndex:
    """In-memory index rebuilt after seed / learning. Cheap for a few thousand names."""

    def __init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        self.groups: dict[str, EntityRef] = {}
        self.members: dict[str, EntityRef] = {}
        self.group_to_members: dict[str, list[str]] = {}

        for r in db.query("SELECT id, name, name_ko FROM groups"):
            self.groups[r["id"]] = EntityRef(r["id"], "group", r["name"], r["name_ko"])
        for r in db.query("SELECT id, stage_name, stage_name_ko FROM members"):
            self.members[r["id"]] = EntityRef(r["id"], "member", r["stage_name"], r["stage_name_ko"])
        for r in db.query("SELECT group_id, member_id FROM group_members"):
            self.group_to_members.setdefault(r["group_id"], []).append(r["member_id"])

        # alias (normalized) -> list of entity ids, plus parallel lists for fuzzy.
        self.group_alias_to_ids: dict[str, list[str]] = {}
        self.member_alias_to_ids: dict[str, list[str]] = {}
        self._g_alias_list: list[str] = []
        self._g_alias_ids: list[str] = []
        self._m_alias_list: list[str] = []
        self._m_alias_ids: list[str] = []
        for r in db.query("SELECT entity_type, entity_id, alias FROM aliases"):
            alias = r["alias"]
            if not alias:
                continue
            if r["entity_type"] == "group":
                self.group_alias_to_ids.setdefault(alias, []).append(r["entity_id"])
                self._g_alias_list.append(alias)
                self._g_alias_ids.append(r["entity_id"])
            else:
                self.member_alias_to_ids.setdefault(alias, []).append(r["entity_id"])
                self._m_alias_list.append(alias)
                self._m_alias_ids.append(r["entity_id"])

        # corrections: pattern -> (type, entity_id, group_id)
        self.corrections = [
            (r["pattern"], r["entity_type"], r["entity_id"], r["group_id"])
            for r in db.query(
                "SELECT pattern, entity_type, entity_id, group_id FROM corrections")
        ]

    # ---- group matching -------------------------------------------------
    def exact_group_hits(self, norm_full: str) -> list[tuple[EntityRef, str]]:
        """Every group whose alias appears as a whole phrase: (group, alias)."""
        padded = f" {norm_full} "
        hits: list[tuple[EntityRef, str]] = []
        for alias, ids in self.group_alias_to_ids.items():
            if len(alias) < 2 or f" {alias} " not in padded:
                continue
            for gid in ids:
                g = self.groups.get(gid)
                if g:
                    hits.append((g, alias))
        return hits

    def _exact_groups(self, padded: str) -> list[tuple[str, int]]:
        """Return (group_id, alias_len) for every alias present as a whole phrase."""
        return [(g.id, len(alias)) for g, alias in self.exact_group_hits(padded.strip())]

    def match_group(self, norm_full: str) -> tuple[EntityRef | None, int, bool, list]:
        padded = f" {norm_full} "

        # 1) learned corrections win.
        for pattern, etype, eid, _gid in self.corrections:
            if etype == "group" and pattern and f" {pattern} " in padded:
                g = self.groups.get(eid)
                if g:
                    return g, 100, False, [(g, 100)]

        # 2) exact whole-phrase alias matches.
        exact = self._exact_groups(padded)
        if exact:
            # Prefer the most specific (longest) alias; detect genuine ties.
            best_len = max(l for _, l in exact)
            winners = sorted({gid for gid, l in exact if l == best_len})
            cands = [(self.groups[g], 100) for g in winners if g in self.groups]
            if len(winners) == 1:
                return self.groups[winners[0]], 100, False, cands
            # Ambiguous: multiple different groups matched equally.
            return self.groups.get(winners[0]), 60, True, cands

        # 3) fuzzy fallback.
        if not self._g_alias_list:
            return None, 0, False, []
        results = process.extract(
            norm_full, self._g_alias_list, scorer=fuzz.WRatio, limit=5)
        cands: list[tuple[EntityRef, int]] = []
        seen: set[str] = set()
        for _alias, score, idx in results:
            gid = self._g_alias_ids[idx]
            if gid in seen or gid not in self.groups:
                continue
            seen.add(gid)
            cands.append((self.groups[gid], int(score)))
        if not cands:
            return None, 0, False, []
        best, best_score = cands[0]
        if best_score < FUZZY_FLOOR:
            return None, 0, False, cands  # keep candidates for the UI, but don't claim a match
        ambiguous = len(cands) > 1 and cands[1][1] >= best_score - 3
        return best, best_score, ambiguous, cands

    # ---- member matching ------------------------------------------------
    def match_member(self, norm_full: str, group_id: str
                     ) -> tuple[EntityRef | None, int, bool, list]:
        member_ids = set(self.group_to_members.get(group_id, []))
        if not member_ids:
            return None, 0, False, []
        padded = f" {norm_full} "

        # learned member corrections within this group.
        for pattern, etype, eid, gid in self.corrections:
            if etype == "member" and gid == group_id and pattern and f" {pattern} " in padded:
                m = self.members.get(eid)
                if m:
                    return m, 100, False, [(m, 100)]

        # exact alias match restricted to this group's members.
        exact_hits: list[tuple[str, int]] = []
        for alias, ids in self.member_alias_to_ids.items():
            if len(alias) < 2 or f" {alias} " not in padded:
                continue
            for mid in ids:
                if mid in member_ids:
                    exact_hits.append((mid, len(alias)))
        if exact_hits:
            best_len = max(l for _, l in exact_hits)
            winners = sorted({mid for mid, l in exact_hits if l == best_len})
            cands = [(self.members[m], 100) for m in winners if m in self.members]
            if len(winners) == 1:
                return self.members[winners[0]], 100, False, cands
            return self.members.get(winners[0]), 60, True, cands

        # fuzzy within group members.
        local = [(a, mid) for a, ids in self.member_alias_to_ids.items()
                 for mid in ids if mid in member_ids]
        if not local:
            return None, 0, False, []
        alias_list = [a for a, _ in local]
        results = process.extract(norm_full, alias_list, scorer=fuzz.WRatio, limit=5)
        cands, seen = [], set()
        for _alias, score, idx in results:
            mid = local[idx][1]
            if mid in seen or mid not in self.members:
                continue
            seen.add(mid)
            cands.append((self.members[mid], int(score)))
        if not cands:
            return None, 0, False, []
        best, best_score = cands[0]
        if best_score < FUZZY_FLOOR:
            return None, 0, False, cands
        ambiguous = len(cands) > 1 and cands[1][1] >= best_score - 3
        return best, best_score, ambiguous, cands

    # ---- top level ------------------------------------------------------
    def match(self, filename_stem: str) -> MatchResult:
        tokens, is_solo = tokens_for_match(filename_stem)
        norm_full = normalize(" ".join(tokens)) if tokens else ""
        res = MatchResult(filename=filename_stem, is_solo=is_solo)
        if not norm_full:
            res.reason = "no usable name in filename"
            return res

        # Collab / multi-group: two+ *different* group names present by exact match
        # -> Special Stages + replicate per group (plan.md §10). A single name that
        # maps to several groups is "ambiguous", handled below, not a collab.
        hits = self.exact_group_hits(norm_full)
        distinct_gids = {g.id for g, _ in hits}
        distinct_aliases = {a for _, a in hits}
        if len(distinct_gids) >= 2 and len(distinct_aliases) >= 2:
            seen: set[str] = set()
            groups: list[EntityRef] = []
            for g, _a in hits:
                if g.id not in seen:
                    seen.add(g.id)
                    groups.append(g)
            res.is_collab = True
            res.groups = groups
            res.group = groups[0]
            res.confidence = 100
            res.reason = f"multi-group collab ({len(groups)} groups)"
            return res

        g, g_score, g_amb, g_cands = self.match_group(norm_full)
        res.group_candidates = g_cands
        if not g:
            res.reason = "no group matched"
            return res
        res.group = g

        if not is_solo:
            res.confidence = 0 if g_amb else g_score
            res.ambiguous = g_amb
            res.reason = "group match" + (" (ambiguous)" if g_amb else "")
            return res

        # solo video -> also resolve member within the matched group.
        m, m_score, m_amb, m_cands = self.match_member(norm_full, g.id)
        res.member_candidates = m_cands
        res.member = m
        res.ambiguous = g_amb or m_amb or (m is None)
        res.confidence = 0 if res.ambiguous else min(g_score, m_score)
        if m is None:
            res.reason = "group matched, member unresolved"
        else:
            res.reason = "group + member match" + (" (ambiguous)" if res.ambiguous else "")
        return res


_index: MatchIndex | None = None


def get_index() -> MatchIndex:
    global _index
    if _index is None:
        _index = MatchIndex()
    return _index


def reload_index() -> None:
    if _index is not None:
        _index.reload()
