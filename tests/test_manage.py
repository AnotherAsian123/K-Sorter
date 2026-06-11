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


def test_remove_alias():
    manage.add_group_alias("g", "Wrongy")
    assert matcher.get_index().match("Wrongy stage").group.id == "g"
    data = manage.get_group("g")
    bad = next(a for a in data["aliases"] if a["raw"] == "Wrongy")
    assert not bad["protected"]
    manage.remove_group_alias("g", bad["key"])
    assert matcher.get_index().match("Wrongy stage").group is None
    # The group's own (current) name is protected from deletion in the UI.
    cur = manage.get_group("g")["group"]["name"]
    assert any(a["protected"] and a["raw"] == cur
               for a in manage.get_group("g")["aliases"])


def test_delete_group():
    gid = enrich.add_confirmed_group("Temp Group", None, ["TG"])
    assert manage.get_group(gid) is not None
    manage.delete_group(gid)
    assert manage.get_group(gid) is None


def test_rename_group_migrates_folder(tmp_path):
    # Renaming a group moves its existing sorted content under the new name.
    db.set_meta("last_dest", str(tmp_path))
    gid = enrich.add_confirmed_group("I'll-it", None)
    old = tmp_path / "I'll-it" / "Group"
    old.mkdir(parents=True)
    (old / "v.mp4").write_bytes(b"x" * 64)
    manage.rename_group(gid, "ILLIT", None)
    assert (tmp_path / "ILLIT" / "Group" / "v.mp4").exists()
    assert not (tmp_path / "I'll-it").exists()


def test_rename_group_merges_into_existing_folder(tmp_path):
    # New-name folder already has files (e.g. sorted after adding the alias):
    # old content merges in, nothing is lost or overwritten.
    db.set_meta("last_dest", str(tmp_path))
    gid = enrich.add_confirmed_group("Old AAA", None)
    old = tmp_path / "Old AAA" / "Group"; old.mkdir(parents=True)
    (old / "a.mp4").write_bytes(b"a" * 64)
    new = tmp_path / "New BBB" / "Group"; new.mkdir(parents=True)
    (new / "b.mp4").write_bytes(b"b" * 64)
    manage.rename_group(gid, "New BBB", None)
    assert (new / "a.mp4").exists() and (new / "b.mp4").exists()
    assert not (tmp_path / "Old AAA").exists()


def test_rename_member_migrates_folder(tmp_path):
    db.set_meta("last_dest", str(tmp_path))
    gid = enrich.add_confirmed_group("MigGroup", None)
    mid = enrich.add_member(gid, "Oldie")
    old = tmp_path / "MigGroup" / "Oldie"
    old.mkdir(parents=True)
    (old / "fancam.mp4").write_bytes(b"m" * 64)
    manage.rename_member(mid, "Newbie", None)
    assert (tmp_path / "MigGroup" / "Newbie" / "fancam.mp4").exists()
    assert not old.exists()
