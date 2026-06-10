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
from .normalize import (COLLAB_MARKERS, TAG_IGNORE, hashtags, member_hint,
                        normalize, tokens_for_match)

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
    # Group-level certainty, independent of whether a member was resolved.
    group_confidence: int = 0
    group_ambiguous: bool = False
    is_collab: bool = False
    collab_marker: bool = False   # explicit 'x'/'feat'/'합동'… marker present
    member_hint: str | None = None  # likely member name the DB doesn't know yet
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
        self.gm_current: dict[tuple[str, str], bool] = {}
        self.group_parent: dict[str, str] = {}

        for r in db.query("SELECT id, name, name_ko, parent_id FROM groups"):
            self.groups[r["id"]] = EntityRef(r["id"], "group", r["name"], r["name_ko"])
            if r["parent_id"]:
                self.group_parent[r["id"]] = r["parent_id"]
        for r in db.query("SELECT id, stage_name, stage_name_ko FROM members"):
            self.members[r["id"]] = EntityRef(r["id"], "member", r["stage_name"], r["stage_name_ko"])
        for r in db.query("SELECT group_id, member_id, is_current FROM group_members"):
            self.group_to_members.setdefault(r["group_id"], []).append(r["member_id"])
            self.gm_current[(r["group_id"], r["member_id"])] = bool(r["is_current"])

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
        # Learned group aliases (from corrections): weaker evidence than a real
        # name — they don't count as "explicit" when unknown hashtags are present.
        self.learned_group_aliases = {
            (eid, pattern) for pattern, etype, eid, _gid in self.corrections
            if etype == "group"
        }

        # Every known name in hashtag-friendly forms ("le sserafim" ->
        # "lesserafim"), to recognise tags like #LE_SSERAFIM or #fromis_9.
        self._known_tag_tokens: set[str] = set()
        for alias in (*self.group_alias_to_ids, *self.member_alias_to_ids):
            compact = alias.replace(" ", "")
            self._known_tag_tokens.update({alias, compact, compact.replace("_", "")})

        # Reverse map for member-hint extraction (strip the group's own names).
        self.group_aliases_by_id: dict[str, set[str]] = {}
        for alias, ids in self.group_alias_to_ids.items():
            for gid in ids:
                self.group_aliases_by_id.setdefault(gid, set()).add(alias)

    def _tag_known(self, tag: str) -> bool:
        """Does this hashtag correspond to any known group/member name?"""
        if {tag, tag.replace("_", " ").strip(), tag.replace("_", "")} \
                & self._known_tag_tokens:
            return True
        # Compound tags like #스테이씨_윤: known if every part is known/generic.
        parts = [p for p in tag.split("_") if p]
        return bool(parts) and all(
            p in self._known_tag_tokens or p in TAG_IGNORE for p in parts)

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

    def _filter_nested_hits(self, hits: list[tuple[EntityRef, str]]
                            ) -> list[tuple[EntityRef, str]]:
        """Drop hits that are really one group seen twice: a group whose matched
        alias is contained inside another hit's alias (e.g. 'NCT' in 'NCT Dream'),
        or who is the parent group of another hit (sub-units win)."""
        best: dict[str, tuple[EntityRef, str]] = {}
        for g, a in hits:
            cur = best.get(g.id)
            if cur is None or len(a) > len(cur[1]):
                best[g.id] = (g, a)
        items = list(best.values())
        out = []
        for g, a in items:
            drop = False
            for g2, a2 in items:
                if g2.id == g.id:
                    continue
                if len(a2) > len(a) and f" {a} " in f" {a2} ":
                    drop = True
                    break
                if self.group_parent.get(g2.id) == g.id:
                    drop = True
                    break
            if not drop:
                out.append((g, a))
        return out

    def match_group(self, norm_full: str) -> tuple[EntityRef | None, int, bool, list]:
        padded = f" {norm_full} "

        # 1) learned corrections win.
        for pattern, etype, eid, _gid in self.corrections:
            if etype == "group" and pattern and f" {pattern} " in padded:
                g = self.groups.get(eid)
                if g:
                    return g, 100, False, [(g, 100)]

        # 2) exact whole-phrase alias matches. When several distinct groups hit
        # (after dropping nested/sub-unit echoes), the one appearing EARLIEST in
        # the filename wins — fancam names lead with the group, while later hits
        # are usually song titles that happen to be group names (e.g. 'Secret').
        all_hits = self.exact_group_hits(norm_full)
        kept_ids = {g.id for g, _ in self._filter_nested_hits(all_hits)}
        if kept_ids:
            # Rank by each group's EARLIEST hit across all its aliases.
            pos: dict[str, tuple[int, EntityRef]] = {}
            for g, a in all_hits:
                if g.id not in kept_ids:
                    continue
                p = padded.find(f" {a} ")
                if g.id not in pos or p < pos[g.id][0]:
                    pos[g.id] = (p, g)
            ranked = sorted(pos.values(), key=lambda t: t[0])
            cands = [(g, 100) for _p, g in ranked]
            if len(ranked) == 1 or ranked[0][0] < ranked[1][0]:
                return ranked[0][1], 100, False, cands
            # Two different groups at the same spot (shared name) — ambiguous.
            return ranked[0][1], 60, True, cands

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

        # Multi-group (plan.md §10): two+ *different* group names present (after
        # dropping nested/sub-unit echoes like NCT in NCT Dream). These are NEVER
        # auto-applied — the user decides. We record whether an explicit collab
        # marker ('x', 'vs', 'feat', '합동'…) was present: without one it is often
        # a song title that doubles as a group name (e.g. 'Secret Code' vs the
        # group Secret), so the review options matter even more.
        all_hits = self.exact_group_hits(norm_full)
        hits = self._filter_nested_hits(all_hits)
        distinct_gids = {g.id for g, _ in hits}
        distinct_aliases = {a for _, a in hits}
        if len(distinct_gids) >= 2 and len(distinct_aliases) >= 2:
            has_marker = any(t in COLLAB_MARKERS for t in tokens)
            padded = f" {norm_full} "
            # Rank by each surviving group's EARLIEST hit across ALL its aliases.
            first_pos: dict[str, tuple[int, EntityRef]] = {}
            for g, a in all_hits:
                if g.id not in distinct_gids:
                    continue
                p = padded.find(f" {a} ")
                if g.id not in first_pos or p < first_pos[g.id][0]:
                    first_pos[g.id] = (p, g)
            # Order by first appearance — the leading group is the likeliest home.
            groups = [g for _p, g in sorted(first_pos.values(), key=lambda t: t[0])]
            res.is_collab = True
            res.collab_marker = has_marker
            res.groups = groups
            res.group = groups[0]
            res.confidence = 100 if has_marker else 50
            res.reason = (f"multi-group collab ({len(groups)} groups)" if has_marker
                          else f"{len(groups)} group names, no collab marker")
            return res

        g, g_score, g_amb, g_cands = self.match_group(norm_full)
        res.group_candidates = g_cands
        if not g:
            res.reason = "no group matched"
            return res
        res.group = g
        res.group_confidence = 0 if g_amb else g_score
        res.group_ambiguous = g_amb

        # Hashtag guard: tags name the group/member, so an unrecognised tag
        # (e.g. #Hearts2Hearts for a group not in the DB) means we may be
        # looking at an unknown entity. Unless the matched group is backed by
        # its REAL name in the filename (not just a learned alias or a fuzzy
        # guess), never auto-sort — ask the user.
        unknown_tags = [t for t in hashtags(filename_stem)
                        if t not in TAG_IGNORE and not self._tag_known(t)]
        explicit = any(h.id == g.id and (h.id, a) not in self.learned_group_aliases
                       for h, a in all_hits)
        tag_doubt = bool(unknown_tags) and not explicit
        if tag_doubt:
            res.group_ambiguous = True

        if not is_solo:
            res.confidence = 0 if (g_amb or tag_doubt) else g_score
            res.ambiguous = g_amb or tag_doubt
            res.reason = "group match" + (" (ambiguous)" if g_amb else "")
        else:
            # solo video -> also resolve member within the matched group.
            m, m_score, m_amb, m_cands = self.match_member(norm_full, g.id)
            res.member_candidates = m_cands
            res.member = m
            res.ambiguous = g_amb or m_amb or (m is None) or tag_doubt
            res.confidence = 0 if res.ambiguous else min(g_score, m_score)
            if m is None:
                res.reason = "group matched, member unresolved"
                # The "(GROUP MEMBER FanCam)" tag often names a member the DB
                # doesn't know yet (new / rebuilt line-ups) — extract a hint.
                res.member_hint = member_hint(
                    filename_stem, self.group_aliases_by_id.get(g.id, set()))
            else:
                res.reason = "group + member match" + (" (ambiguous)" if res.ambiguous else "")
        if tag_doubt:
            res.reason = ("group uncertain — unrecognised hashtag(s): "
                          + ", ".join("#" + t for t in unknown_tags))
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
