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
