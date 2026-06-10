"""Move-strategy tests, including the cross-mount (EXDEV) fallbacks that the
Unraid /watch -> /watch_dest setup triggers."""
import errno
import os
import tempfile

os.environ["KSORTER_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ksorter-mover-")

from app import mover  # noqa: E402

_REAL_REPLACE = os.replace
_REAL_LINK = os.link


def _exdev_when_moving(src):
    """Raise EXDEV only for the initial src->dest move, so the copy path's
    own temp-swap (tmp->dest) still works normally."""
    def _inner(a, b):
        if str(a) == str(src):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return _REAL_REPLACE(a, b)
    return _inner


def test_same_mount_uses_rename(tmp_path):
    src = tmp_path / "a.mp4"; src.write_bytes(b"x" * 1024)
    dest = tmp_path / "out" / "a.mp4"
    r = mover.safe_move(src, dest)
    assert r.status == "moved" and r.method == "rename"
    assert dest.exists() and not src.exists()


def test_cross_mount_falls_back_to_hardlink(tmp_path, monkeypatch):
    # Separate bind mounts: rename raises EXDEV, but hardlink works (same fs).
    src = tmp_path / "b.mp4"; src.write_bytes(b"y" * 2048)
    dest = tmp_path / "dst" / "b.mp4"
    monkeypatch.setattr(mover.os, "replace", _exdev_when_moving(src))
    r = mover.safe_move(src, dest)
    assert r.status == "moved" and r.method == "hardlink", r
    assert dest.exists() and not src.exists()
    assert dest.read_bytes() == b"y" * 2048


def test_same_path_is_noop_not_delete(tmp_path):
    # Regression: resolving a file to its CURRENT location must be a no-op —
    # the dedupe branch would otherwise see "identical file" and delete it.
    f = tmp_path / "v.mp4"
    f.write_bytes(b"precious" * 512)
    r = mover.safe_move(f, f)
    assert r.status == "moved" and r.method == "noop"
    assert f.exists() and f.read_bytes() == b"precious" * 512


def test_dedupe_when_same_file_at_dest(tmp_path):
    # The destination already holds an IDENTICAL copy -> remove the redundant
    # source instead of skipping (so corrections actually take effect).
    data = b"same-content" * 4096
    src = tmp_path / "wrong" / "v.mp4"
    dest = tmp_path / "right" / "v.mp4"
    src.parent.mkdir(parents=True); dest.parent.mkdir(parents=True)
    src.write_bytes(data); dest.write_bytes(data)
    r = mover.safe_move(src, dest)
    assert r.status == "moved" and r.method == "dedupe"
    assert not src.exists() and dest.exists()


def test_collision_different_file_is_skipped(tmp_path):
    # Same name but DIFFERENT content -> never overwrite, keep the source.
    src = tmp_path / "a" / "v.mp4"
    dest = tmp_path / "b" / "v.mp4"
    src.parent.mkdir(parents=True); dest.parent.mkdir(parents=True)
    src.write_bytes(b"A" * 4096); dest.write_bytes(b"B" * 4096)
    r = mover.safe_move(src, dest)
    assert r.status == "skipped"
    assert src.exists() and dest.read_bytes() == b"B" * 4096


def test_undo_survives_cross_device(tmp_path, monkeypatch):
    # Regression: undo across separate bind mounts must not fail with EXDEV.
    src = tmp_path / "orig" / "v.mp4"
    dest = tmp_path / "sorted" / "Group" / "v.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"data" * 256)
    mover.safe_move(src, dest)
    assert dest.exists() and not src.exists()

    monkeypatch.setattr(mover.os, "replace", _exdev_when_moving(dest))
    ok = mover.undo_one(str(src), str(dest), "move")
    assert ok is True
    assert src.exists() and not dest.exists()
    assert src.read_bytes() == b"data" * 256


def test_truly_different_fs_falls_back_to_copy(tmp_path, monkeypatch):
    # Both rename AND hardlink fail with EXDEV -> safe copy+verify+delete.
    src = tmp_path / "c.mp4"; src.write_bytes(b"z" * 4096)
    dest = tmp_path / "dst2" / "c.mp4"
    monkeypatch.setattr(mover.os, "replace", _exdev_when_moving(src))

    def link_exdev(*_a, **_k):
        raise OSError(errno.EXDEV, "Invalid cross-device link")
    monkeypatch.setattr(mover.os, "link", link_exdev)

    r = mover.safe_move(src, dest)
    assert r.status == "moved" and r.method == "copy", r
    assert dest.exists() and not src.exists()
    assert dest.read_bytes() == b"z" * 4096
