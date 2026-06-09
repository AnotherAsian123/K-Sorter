"""End-to-end engine tests: real temp files moved into real folders, then undone.
Offline — uses a tiny hand-seeded DB (no network)."""
import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ksorter-eng-")
os.environ["KSORTER_CONFIG_DIR"] = _TMP

from app import database as db          # noqa: E402
from app import engine, matcher         # noqa: E402
from app.normalize import normalize     # noqa: E402
from app.scanner import VideoFile       # noqa: E402


def _alias(t, i, raw):
    db.execute("INSERT OR IGNORE INTO aliases(entity_type,entity_id,alias,alias_raw)"
               " VALUES(?,?,?,?)", (t, i, normalize(raw), raw))


def setup_module(_m):
    db.get_conn()
    for gid, name, ko in [("twice", "TWICE", "트와이스"), ("itzy", "ITZY", "있지")]:
        db.execute("INSERT OR REPLACE INTO groups(id,name,name_ko,is_active) VALUES(?,?,?,1)",
                   (gid, name, ko))
        _alias("group", gid, name); _alias("group", gid, ko)
    for mid, en, ko, gid in [("nayeon", "Nayeon", "나연", "twice"),
                             ("momo", "Momo", "모모", "twice"),
                             ("yeji", "Yeji", "예지", "itzy")]:
        db.execute("INSERT OR REPLACE INTO members(id,stage_name,stage_name_ko) VALUES(?,?,?)",
                   (mid, en, ko))
        db.execute("INSERT OR REPLACE INTO group_members(group_id,member_id,is_current)"
                   " VALUES(?,?,1)", (gid, mid))
        _alias("member", mid, en); _alias("member", mid, ko)
    matcher.reload_index()


def _make(src: Path, name: str):
    f = src / name
    f.write_bytes(b"x" * 2048)
    return f


def test_full_sort_and_undo(tmp_path):
    src = tmp_path / "src"; dst = tmp_path / "dst"
    src.mkdir(); dst.mkdir()
    solo = _make(src, "230101 TWICE Nayeon fancam 4K.mp4")
    group_video = _make(src, "TWICE The Feels MV.mp4")

    batch = "test-batch"
    for f in (solo, group_video):
        vf = VideoFile(path=f, size=f.stat().st_size, stem=f.stem)
        item = engine.build_plan_item(vf, dst)
        assert item.status == "auto", f"{f.name} -> {item.status} ({item.reason})"
        res = engine.apply_item(item, batch)
        assert res["status"] == "moved", res

    assert (dst / "TWICE" / "Nayeon" / "230101 TWICE Nayeon fancam 4K.mp4").exists()
    assert (dst / "TWICE" / "Group" / "TWICE The Feels MV.mp4").exists()
    assert not solo.exists() and not group_video.exists()  # sources moved

    # Undo restores originals.
    out = engine.undo_batch(batch)
    assert out["restored"] == 2 and out["failed"] == 0
    assert solo.exists() and group_video.exists()


def test_history_records_labels(tmp_path):
    src = tmp_path / "h1"; dst = tmp_path / "h2"
    src.mkdir(); dst.mkdir()
    f = _make(src, "230101 TWICE Momo fancam.mp4")
    vf = VideoFile(path=f, size=f.stat().st_size, stem=f.stem)
    engine.apply_item(engine.build_plan_item(vf, dst), "hist-batch")
    moves = engine.get_batch_moves("hist-batch")
    assert len(moves) == 1
    assert moves[0]["filename"] == "230101 TWICE Momo fancam.mp4"
    assert moves[0]["group_name"] == "TWICE"
    assert moves[0]["member_name"] == "Momo"


def test_collab_replicates(tmp_path):
    src = tmp_path / "s2"; dst = tmp_path / "d2"
    src.mkdir(); dst.mkdir()
    f = _make(src, "TWICE x ITZY special stage.mp4")
    vf = VideoFile(path=f, size=f.stat().st_size, stem=f.stem)
    item = engine.build_plan_item(vf, dst)
    assert item.is_collab and item.status == "auto"
    res = engine.apply_item(item, "collab-batch")
    assert res["status"] == "moved"
    assert (dst / "_Special Stages" / f.name).exists()
    # Replicated into both groups' Group/ folders.
    assert (dst / "TWICE" / "Group" / f.name).exists()
    assert (dst / "ITZY" / "Group" / f.name).exists()


def test_resolve_from_manual_queue(tmp_path):
    # Regression: items in the manual queue (not just the confirm queue) must
    # be resolvable.
    from app.jobs import manager
    src = tmp_path / "mq"; dst = tmp_path / "mqd"
    src.mkdir(); dst.mkdir()
    f = _make(src, "totally unknown video.mp4")
    item = engine.build_plan_item(VideoFile(path=f, size=f.stat().st_size, stem=f.stem), dst)
    assert item.status == "manual"
    manager.state.manual = [item.as_dict()]
    manager.state.review = []
    manager.state.dest = str(dst)
    manager.state.batch_id = "mqb"
    res = manager.resolve(item.id, "twice", "momo", learn=False)
    assert res["ok"], res
    assert (dst / "TWICE" / "Momo" / "totally unknown video.mp4").exists()
    assert not manager.state.manual  # removed from the queue


def test_add_missing_member(tmp_path):
    # Seed data may miss new line-up additions; adding one should make it match.
    from app import enrich
    mid = enrich.add_member("twice", "Newbie")
    assert mid
    r = matcher.get_index().match("TWICE Newbie fancam")
    assert r.group.id == "twice"
    assert r.member and r.member.id == mid
    assert r.confidence >= 90


def test_unknown_goes_manual(tmp_path):
    src = tmp_path / "s3"; dst = tmp_path / "d3"
    src.mkdir(); dst.mkdir()
    f = _make(src, "family_picnic_clip.mp4")
    vf = VideoFile(path=f, size=f.stat().st_size, stem=f.stem)
    item = engine.build_plan_item(vf, dst)
    assert item.status == "manual"
    assert item.primary_dest is None
