"""Matcher unit tests over realistic fancam-style filenames.

Run: python -m pytest tests/ -q
"""
import os
import tempfile

# Point CONFIG_DIR at a temp dir BEFORE importing app modules (settings reads env
# at import time).
_TMP = tempfile.mkdtemp(prefix="ksorter-test-")
os.environ["KSORTER_CONFIG_DIR"] = _TMP

from app import database as db          # noqa: E402
from app import matcher as m            # noqa: E402
from app.normalize import normalize     # noqa: E402


def _alias(etype, eid, raw):
    db.execute(
        "INSERT OR IGNORE INTO aliases(entity_type,entity_id,alias,alias_raw)"
        " VALUES(?,?,?,?)", (etype, eid, normalize(raw), raw))


def setup_module(_module):
    db.get_conn()
    # Two groups to exercise disambiguation; TWICE is the star of the show.
    db.execute("INSERT OR REPLACE INTO groups(id,name,name_ko,is_active) VALUES"
               "('twice','TWICE','트와이스',1)")
    db.execute("INSERT OR REPLACE INTO groups(id,name,name_ko,is_active) VALUES"
               "('lsf','LE SSERAFIM','르세라핌',1)")
    _alias("group", "twice", "TWICE")
    _alias("group", "twice", "트와이스")
    _alias("group", "lsf", "LE SSERAFIM")
    _alias("group", "lsf", "르세라핌")

    members = [
        ("nayeon", "Nayeon", "나연", "twice", 1),
        ("momo", "Momo", "모모", "twice", 1),
        ("mina", "Mina", "미나", "twice", 1),
        ("jeongyeon", "Jeongyeon", "정연", "twice", 1),
        ("kazuha", "Kazuha", "카즈하", "lsf", 1),
    ]
    for mid, en, ko, gid, cur in members:
        db.execute("INSERT OR REPLACE INTO members(id,stage_name,stage_name_ko)"
                   " VALUES(?,?,?)", (mid, en, ko))
        db.execute("INSERT OR REPLACE INTO group_members(group_id,member_id,is_current)"
                   " VALUES(?,?,?)", (gid, mid, cur))
        _alias("member", mid, en)
        _alias("member", mid, ko)
    m.reload_index()


def match(name):
    return m.get_index().match(name)


def test_group_only_english():
    r = match("TWICE - The Feels MV 1080p")
    assert r.group and r.group.id == "twice"
    assert not r.is_solo
    assert r.confidence >= 90


def test_solo_fancam_english():
    r = match("230101 TWICE Nayeon fancam 4K")
    assert r.group.id == "twice"
    assert r.is_solo
    assert r.member and r.member.id == "nayeon"
    assert r.confidence >= 90


def test_solo_fancam_korean():
    r = match("트와이스 모모 직캠")
    assert r.group.id == "twice"
    assert r.is_solo
    assert r.member.id == "momo"


def test_member_belongs_to_right_group():
    # Kazuha must not be attached to TWICE.
    r = match("TWICE Kazuha focus")
    assert r.group.id == "twice"
    # Kazuha isn't a TWICE member -> member unresolved -> needs review.
    assert r.member is None
    assert r.ambiguous


def test_no_match_goes_manual():
    r = match("random_home_video_2023")
    assert r.group is None
    assert r.confidence == 0


def test_date_not_required_and_ignored():
    r = match("2023.05.06 TWICE Mina 세로직캠")
    assert r.group.id == "twice"
    assert r.member.id == "mina"


def test_member_hint_extraction():
    from app.normalize import member_hint
    aliases = {"fifty fifty", "피프티피프티"}
    fn = "[4K] 251114 피프티피프티 예원 'Pookie' 직캠 (FIFTY FIFTY YEWON FanCam) [djUH4KoEGrE]"
    assert member_hint(fn, aliases) == "yewon"
    assert member_hint("(fromis_9 Lee CHAEYOUNG FanCam)", {"fromis_9", "프로미스나인"}) \
        == "lee chaeyoung"
    assert member_hint("TWICE The Feels MV", {"twice"}) is None


def test_unresolved_member_gets_hint():
    # Member not in the DB (new line-up) -> the fancam tag supplies the hint.
    r = match("TWICE 직캠 (TWICE NEWFACE FanCam)")
    assert r.group.id == "twice"
    assert r.member is None
    assert r.member_hint == "newface"
