"""Database manager CRUD tests."""
import os
import tempfile

os.environ["KSORTER_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ksorter-manage-")

from app import database as db          # noqa: E402
from app import enrich, manage, matcher  # noqa: E402


def setup_module(_m):
    db.execute("INSERT OR REPLACE INTO groups(id,name,name_ko,is_active,source) "
               "VALUES('g','Some Group','그룹',1,'seed')")
    matcher.reload_index()


def test_rename_group_keeps_old_alias():
    manage.rename_group("g", "Renamed Group", "리네임")
    g = manage.get_group("g")["group"]
    assert g["name"] == "Renamed Group"
    # Both the new and (additively) old names resolve.
    assert matcher.get_index().match("Renamed Group stage").group.id == "g"


def test_add_and_toggle_member():
    enrich.add_member("g", "Alpha")
    data = manage.get_group("g")
    mid = data["members"][0]["id"]
    assert data["members"][0]["is_current"] == 1
    manage.set_member_current("g", mid, False)
    assert manage.get_group("g")["members"][0]["is_current"] == 0
    manage.remove_member("g", mid)
    assert manage.get_group("g")["members"] == []


def test_alias_then_match():
    manage.add_group_alias("g", "GG")
    assert matcher.get_index().match("GG comeback").group.id == "g"


def test_delete_group():
    gid = enrich.add_confirmed_group("Temp Group", None, ["TG"])
    assert manage.get_group(gid) is not None
    manage.delete_group(gid)
    assert manage.get_group(gid) is None
