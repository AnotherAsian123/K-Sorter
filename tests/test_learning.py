"""Guard against learned-alias pollution that caused misidentification
(e.g. a STAYC-only video being flagged as a fromis_9 collab)."""
import os
import tempfile

os.environ["KSORTER_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ksorter-learn-")

from app import database as db          # noqa: E402
from app import engine, matcher         # noqa: E402
from app.normalize import is_learnable_token, tokens_for_match  # noqa: E402


def _alias(t, i, raw):
    from app.normalize import normalize
    db.execute("INSERT OR IGNORE INTO aliases(entity_type,entity_id,alias,alias_raw)"
               " VALUES(?,?,?,?)", (t, i, normalize(raw), raw))


def setup_module(_m):
    for gid, en, ko in [("stayc", "STAYC", "스테이씨"), ("fromis", "fromis_9", "프로미스나인")]:
        db.execute("INSERT OR REPLACE INTO groups(id,name,name_ko,is_active) VALUES(?,?,?,1)",
                   (gid, en, ko))
        _alias("group", gid, en); _alias("group", gid, ko)
    matcher.reload_index()


def test_learnable_token_rejects_junk():
    assert is_learnable_token("올데이프로젝트")          # a real name
    assert not is_learnable_token("콘서트")               # common word (stoplist)
    assert not is_learnable_token("생일")                 # common word
    assert not is_learnable_token("cl5mmpajrhi")          # youtube id (mixed)
    assert not is_learnable_token("250413")               # numeric
    assert not is_learnable_token("a")                    # too short


def test_youtube_id_stripped():
    toks, _ = tokens_for_match("[4K] STAYC fancam [cl5mMPajrHI]")
    assert "cl5mmpajrhi" not in toks


def test_title_dump_does_not_pollute_then_collab():
    fn = "[4K] 250413 스테이씨 '윤 생일 축하 + 콘서트 소감' 직캠 (STAYC FanCam) [cl5mMPajrHI]"
    # A group-only confirm whose title is common words must NOT create aliases.
    engine.learn_correction("fromis_9 콘서트 소감 무대", "fromis", None)
    r = matcher.get_index().match(fn)
    assert not r.is_collab
    assert r.group.id == "stayc"


def test_purge_cleans_existing_pollution():
    # Inject a previously-learned junk alias, confirm it pollutes, then purge.
    db.execute("INSERT OR IGNORE INTO aliases(entity_type,entity_id,alias,alias_raw)"
               " VALUES('group','fromis','콘서트','콘서트')")
    db.execute("INSERT OR REPLACE INTO corrections(pattern,entity_type,entity_id,group_id)"
               " VALUES('콘서트','group','fromis','fromis')")
    matcher.reload_index()
    fn = "스테이씨 콘서트 (STAYC FanCam)"
    assert matcher.get_index().match(fn).is_collab          # polluted
    assert engine.purge_polluted_aliases() >= 1
    assert not matcher.get_index().match(fn).is_collab      # clean
