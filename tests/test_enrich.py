"""Offline tests for the Wikipedia members-section parser."""
import os
import tempfile

os.environ["KSORTER_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ksorter-enrich-")

from app.enrich import parse_members_section  # noqa: E402

H2H = (
    "Hearts2Hearts is a South Korean girl group.\n\n"
    "== Members ==\nCarmen (카르멘)\nJiwoo (지우) – leader\nYuha (유하)\n"
    "Stella (스텔라)\nJuun (주은)\nA-na (에이나)\nIan (이안)\nYe-on (예온)\n\n\n"
    "== Discography ==\n\n=== Extended plays ===\n"
)

SPLIT = (
    "== Members ==\n\n=== Current ===\nKeena (키나)\nAthena (아테나)\n\n"
    "=== Former ===\nSaena (새나)\n\n== History ==\nLong prose that should "
    "never be parsed as a member name because it is a full sentence.\n"
)


def test_parse_plain_member_list():
    members = parse_members_section(H2H)
    assert [m["name"] for m in members] == [
        "Carmen", "Jiwoo", "Yuha", "Stella", "Juun", "A-na", "Ian", "Ye-on"]
    by_name = {m["name"]: m for m in members}
    assert by_name["Ian"]["name_ko"] == "이안"
    assert by_name["Jiwoo"]["name_ko"] == "지우"   # role suffix stripped
    assert all(m["current"] for m in members)


def test_parse_current_former_subsections():
    members = parse_members_section(SPLIT)
    by_name = {m["name"]: m for m in members}
    assert by_name["Keena"]["current"] is True
    assert by_name["Saena"]["current"] is False
    assert "Long" not in by_name and len(members) == 3


def test_no_members_section():
    assert parse_members_section("== History ==\nJust prose.") == []


def test_fetch_dedupes_and_syncs_status(monkeypatch):
    # Roster sync must: not duplicate existing members across name variants,
    # flip current/former to match the web, and add genuinely new members.
    from app import database as db, enrich
    from app.normalize import normalize

    db.execute("INSERT OR REPLACE INTO groups(id,name,is_active,source) "
               "VALUES('ff','FIFTY FIFTY',1,'seed')")
    for mid, en, ko in [("keena", "Keena", "키나"), ("sio", "Sio", "시오"),
                        ("chae", "Lee Chaeyoung", None), ("aran", "Aran", "아란")]:
        db.execute("INSERT OR REPLACE INTO members(id,stage_name,stage_name_ko)"
                   " VALUES(?,?,?)", (mid, en, ko))
        db.execute("INSERT OR REPLACE INTO group_members(group_id,member_id,is_current)"
                   " VALUES('ff',?,1)", (mid,))
        for raw in (en, ko):
            if raw:
                db.execute("INSERT OR IGNORE INTO aliases(entity_type,entity_id,alias,alias_raw)"
                           " VALUES('member',?,?,?)", (mid, normalize(raw), raw))

    canned = [
        {"name": "Keena", "name_ko": "키나", "current": True, "aliases": []},
        {"name": "Sio", "name_ko": None, "current": False, "aliases": []},      # now former
        {"name": "Lee Chae-young", "name_ko": None, "current": False,           # compact dedupe
         "aliases": []},
        {"name": "Yewon", "name_ko": "예원", "current": True,                    # genuinely new
         "aliases": ["Park Ye-won"]},
    ]
    monkeypatch.setattr(enrich, "lookup_group_members", lambda _n: canned)
    added = enrich.fetch_group_members("ff")
    assert added == 1   # only Yewon

    rows = {r["stage_name"]: r["is_current"] for r in db.query(
        "SELECT m.stage_name, gm.is_current FROM group_members gm "
        "JOIN members m ON m.id=gm.member_id WHERE gm.group_id='ff'")}
    assert rows["Keena"] == 1
    assert rows["Sio"] == 0                  # flipped to former
    assert rows["Lee Chaeyoung"] == 0        # matched via compact name, flipped
    assert rows["Yewon"] == 1
    # Aran isn't in the (complete) web roster at all -> demoted to former.
    assert rows["Aran"] == 0
    assert len(rows) == 5                    # no duplicates created


def test_clean_label_strips_disambiguators():
    from app.enrich import _clean_label
    assert _clean_label("승민 (2000년)") == "승민"
    assert _clean_label("아이엔 (가수)") == "아이엔"
    assert _clean_label("Hyunjin") == "Hyunjin"
    assert _clean_label("") == ""
