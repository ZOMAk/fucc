import os
import stat

import pytest

from fucc.errors import FuccError
from fucc.files import read_snapshot, replace_snapshot


def test_replace_and_restore_snapshots(tmp_path):
    path = str(tmp_path / "nested" / "file")
    absent = {"text": None, "mode": None}
    assert read_snapshot(path) == absent
    target = {"text": "hello 世界\n", "mode": 0o755}
    replace_snapshot(path, target, absent)
    assert read_snapshot(path) == target
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o755
    replace_snapshot(path, absent, target)
    assert not os.path.exists(path)
    assert not list(tmp_path.rglob("*.tmp"))


def test_refuses_stale_and_unsafe_paths(tmp_path):
    file = tmp_path / "file"
    file.write_text("before")
    before = read_snapshot(str(file))
    file.write_text("changed")
    with pytest.raises(FuccError, match="写入前"):
        replace_snapshot(str(file), {"text": "after", "mode": 0o600}, before)
    symbolic = tmp_path / "linked"
    symbolic.symlink_to(file)
    with pytest.raises(FuccError):
        read_snapshot(str(symbolic))
    os.link(file, tmp_path / "hardlink")
    with pytest.raises(FuccError):
        read_snapshot(str(file))
    with pytest.raises(FuccError):
        read_snapshot(str(tmp_path))
    directory_link = tmp_path / "directory-link"
    directory_link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(FuccError):
        replace_snapshot(str(directory_link / "missing"), {"text": "x", "mode": 0o600}, {"text": None, "mode": None})
    with pytest.raises(FuccError):
        read_snapshot("relative")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(FuccError):
        read_snapshot(str(fifo))


@pytest.mark.parametrize("desired", [{"text": "x", "mode": True}, {"text": "x", "mode": None}, {"text": None, "mode": 0o600}, {"text": "x", "mode": 0o1000}])
def test_invalid_snapshots_do_not_write(tmp_path, desired):
    path = str(tmp_path / "file")
    with pytest.raises(TypeError):
        replace_snapshot(path, desired, {"text": None, "mode": None})
    assert not os.path.exists(path)


def test_cleans_temporary_file_on_concurrent_change(tmp_path, monkeypatch):
    path = str(tmp_path / "file")
    absent = {"text": None, "mode": None}
    original = os.fsync
    changed = False

    def change_during_fsync(fd):
        nonlocal changed
        original(fd)
        if not changed:
            changed = True
            with open(path, "w") as stream:
                stream.write("external")

    monkeypatch.setattr(os, "fsync", change_during_fsync)
    with pytest.raises(FuccError, match="写入期间"):
        replace_snapshot(path, {"text": "ours", "mode": 0o600}, absent)
    assert (tmp_path / "file").read_text() == "external"
    assert not list(tmp_path.glob("*.tmp"))
