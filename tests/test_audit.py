"""Destination integrity audit: flag mis-sorted videos, leave correct ones."""
import os
import tempfile

os.environ["KSORTER_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ksorter-audit-")

from app import audit, database as db, matcher  # noqa: E402
from app.normalize import normalize             # noqa: E402


def _alias(t, i, raw):
    db.execute("INSERT OR IGNORE INTO aliases(entity_type,entity_id,alias,alias_raw)"
               " VALUES(?,?,?,?)", (t, i, normalize(raw), raw))


def setup_module(_m):
    for gid, en in [("stayc", "STAYC"), ("fromis", "fromis_9")]:
        db.execute("INSERT OR REPLACE INTO groups(id,name,is_active) VALUES(?,?,1)", (gid, en))
        _alias("group", gid, en)
    for mid, en, gid in [("yoon", "Yoon", "stayc"), ("sumin", "Sumin", "stayc")]:
        db.execute("INSERT OR REPLACE INTO members(id,stage_name) VALUES(?,?)", (mid, en))
        db.execute("INSERT OR REPLACE INTO group_members(group_id,member_id,is_current)"
                   " VALUES(?,?,1)", (gid, mid))
        _alias("member", mid, en)
    matcher.reload_index()


def _put(root, *parts):
    p = root.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * 64)
    return p


def test_flags_wrong_group(tmp_path):
    _put(tmp_path, "fromis_9", "Group", "STAYC Yoon fancam.mkv")   # mis-sorted
    _put(tmp_path, "STAYC", "Sumin", "STAYC Sumin focus.mkv")      # correct
    flags = list(audit.audit_destination(tmp_path))
    assert len(flags) == 1
    f = flags[0]
    assert f.filename == "STAYC Yoon fancam.mkv"
    assert f.current_location == "fromis_9/Group"
    assert f.group_id == "stayc" and f.member_id == "yoon"


def test_flags_wrong_group_even_without_member(tmp_path):
    # A solo fancam with no resolvable member is still a confident GROUP match —
    # the wrong folder must still be flagged.
    _put(tmp_path, "fromis_9", "Group", "STAYC concert fancam.mkv")
    flags = list(audit.audit_destination(tmp_path))
    assert len(flags) == 1
    assert flags[0].group_id == "stayc"
    assert flags[0].member_id is None


def test_flags_wrong_member(tmp_path):
    # Right group, wrong member folder.
    _put(tmp_path, "STAYC", "Sumin", "STAYC Yoon fancam.mkv")
    flags = list(audit.audit_destination(tmp_path))
    assert len(flags) == 1
    assert "member" in flags[0].reason
    assert flags[0].member_id == "yoon"


def test_true_collab_in_special_stages_not_flagged(tmp_path):
    _put(tmp_path, "_Special Stages", "STAYC x fromis_9 합동무대.mkv")
    assert list(audit.audit_destination(tmp_path)) == []


def test_single_group_in_special_stages_flagged(tmp_path):
    # A non-collab that landed in _Special Stages must be flagged for review.
    _put(tmp_path, "_Special Stages", "STAYC Yoon fancam.mkv")
    flags = list(audit.audit_destination(tmp_path))
    assert len(flags) == 1
    f = flags[0]
    assert f.current_location == "_Special Stages"
    assert f.group_id == "stayc" and f.member_id == "yoon"


def test_markerless_multigroup_in_special_stages_offers_options(tmp_path):
    # Two group names, no collab marker -> review with the collab options.
    _put(tmp_path, "_Special Stages", "STAYC fromis_9 mention.mkv")
    flags = list(audit.audit_destination(tmp_path))
    assert len(flags) == 1
    f = flags[0]
    assert f.is_collab
    assert {g["id"] for g in f.collab_groups} == {"stayc", "fromis"}
    assert f.current_location == "_Special Stages"


def test_markerless_multigroup_in_named_group_left_alone(tmp_path):
    # Sitting under one of the named groups is a plausible home -> not flagged.
    _put(tmp_path, "STAYC", "Group", "STAYC fromis_9 mention.mkv")
    assert list(audit.audit_destination(tmp_path)) == []


def test_multigroup_solo_in_group_folder_flags_member(tmp_path):
    # Multi-group filename (song-title noise) already filed under the right
    # group, but it's a SOLO fancam -> must still be flagged for the member.
    _put(tmp_path, "STAYC", "Group", "STAYC Yoon fromis_9 fancam.mkv")
    flags = list(audit.audit_destination(tmp_path))
    assert len(flags) == 1
    f = flags[0]
    assert not f.is_collab
    assert f.group_id == "stayc" and f.member_id == "yoon"
    assert "member" in f.reason
    assert f.current_location == "STAYC/Group"


def test_multigroup_solo_in_correct_member_folder_left_alone(tmp_path):
    _put(tmp_path, "STAYC", "Yoon", "STAYC Yoon fromis_9 fancam.mkv")
    assert list(audit.audit_destination(tmp_path)) == []


def test_markerless_multigroup_in_unrelated_group_flagged(tmp_path):
    # Sitting under a folder that is NONE of the named groups -> needs a decision.
    _put(tmp_path, "Weeekly", "Group", "STAYC fromis_9 mention.mkv")
    flags = list(audit.audit_destination(tmp_path))
    assert len(flags) == 1
    assert flags[0].is_collab
    assert flags[0].current_location == "Weeekly/Group"


def test_unidentifiable_not_flagged(tmp_path):
    _put(tmp_path, "STAYC", "Group", "random home clip.mkv")
    assert list(audit.audit_destination(tmp_path)) == []


def test_unknown_hashtag_placement_flagged(tmp_path):
    # A file placed via a learned alias whose hashtags name an unknown entity
    # must be flagged wherever it sits.
    db.execute("INSERT OR IGNORE INTO aliases(entity_type,entity_id,alias,alias_raw)"
               " VALUES('group','stayc','이안이','이안이')")
    db.execute("INSERT OR REPLACE INTO corrections(pattern,entity_type,entity_id,group_id)"
               " VALUES('이안이','group','stayc','stayc')")
    matcher.reload_index()
    try:
        _put(tmp_path, "STAYC", "Group", "이안이 메롱 #Hearts2Hearts #IAN.mkv")
        flags = list(audit.audit_destination(tmp_path))
        assert len(flags) == 1
        assert "hashtag" in flags[0].reason
        assert flags[0].current_location == "STAYC/Group"
    finally:
        db.execute("DELETE FROM corrections WHERE pattern='이안이'")
        db.execute("DELETE FROM aliases WHERE alias='이안이'")
        matcher.reload_index()


def test_skipped_decision_persists(tmp_path):
    from app import engine
    from app.jobs import manager
    engine.reset_decisions()
    _put(tmp_path, "fromis_9", "Group", "STAYC concert fancam.mkv")
    flags = list(audit.audit_destination(tmp_path))
    assert len(flags) == 1

    manager.state.review = [flags[0].as_dict()]
    manager.state.manual = []
    manager.state.dest = str(tmp_path)
    manager.skip(flags[0].id)
    assert engine.get_decision("STAYC concert fancam.mkv") == "fromis_9/Group"

    # Future audits leave it alone — until approvals are reset.
    assert list(audit.audit_destination(tmp_path)) == []
    engine.reset_decisions()
    assert len(list(audit.audit_destination(tmp_path))) == 1
