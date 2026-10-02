import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from fucc.errors import FuccError
from fucc.state import MutationLock, canonical_json, process_start_ticks, read_checked, write_checked


def build_old_bundle(tmp_path):
    """Bundle the actual predecessor into a temporary file; never modify it."""
    old = Path(__file__).resolve().parents[2] / "fucc"
    esbuild = next((old / "node_modules/.pnpm").glob("esbuild@*/node_modules/esbuild/bin/esbuild"), None)
    if not shutil.which("node") or esbuild is None:
        pytest.skip("Predecessor source/runtime unavailable for cross-language validation")
    entry = tmp_path / "legacy-entry.ts"
    entry.write_text("\n".join(
        f"export * from {json.dumps(str(old / 'src' / source))};"
        for source in ("state/checksum.ts", "state/mutation-coordinator.ts", "local/transaction.ts")
    ))
    bundle = tmp_path / "legacy.mjs"
    subprocess.run([str(esbuild), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}"],
                   check=True, capture_output=True, text=True)
    return bundle


@pytest.fixture
def old_fucc_bundle(tmp_path):
    return build_old_bundle(tmp_path)


def node(bundle, code, *arguments):
    return subprocess.run(["node", "--input-type=module", "-e",
                           f"import * as old from {json.dumps(bundle.as_uri())};\n" + code, *map(str, arguments)],
                          check=True, capture_output=True, text=True).stdout.strip()


def test_ecmascript_number_and_utf16_canonicalization(old_fucc_bundle):
    source = {"\ufffd": 1, "😀": 2, "a": [-0.0, 1e-7, 1e-6, 1e20, 1e21, 1e30, 333333333.33333329], "文": "\n雪"}
    expected = node(old_fucc_bundle, "console.log(old.canonicalJson(JSON.parse(process.argv[1])))", json.dumps(source))
    assert canonical_json(source) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 2**53 + 1, {1: "bad"}, (1, 2), "\ud800"])
def test_unsupported_canonical_values_fail_closed(value):
    with pytest.raises(FuccError):
        canonical_json(value)


def test_cycles_fail_closed():
    value = []
    value.append(value)
    with pytest.raises(FuccError):
        canonical_json(value)


def test_checked_format_exactly_matches_predecessor(tmp_path, old_fucc_bundle):
    target = tmp_path / "state.json"
    payload = {"number": 1e21, "a": [None, True, "雪"], "😀": "text"}
    expected = node(old_fucc_bundle,
                    "console.log(old.serializeCheckedEnvelope(old.createCheckedEnvelope('fixture-v1',JSON.parse(process.argv[1]))))",
                    json.dumps(payload)) + "\n"
    write_checked(tmp_path, target, "fixture-v1", payload)
    assert target.read_text() == expected
    assert target.stat().st_mode & 0o777 == 0o600
    assert read_checked(tmp_path, target, "fixture-v1")["payload"] == payload


def test_previous_generation_and_corruption(tmp_path):
    path = tmp_path / "state.json"
    assert read_checked(tmp_path, path, "fixture-v1") == {"kind": "missing"}
    write_checked(tmp_path, path, "fixture-v1", {"revision": 1})
    write_checked(tmp_path, path, "fixture-v1", {"revision": 2})
    assert read_checked(tmp_path, path, "fixture-v1")["payload"] == {"revision": 2}
    path.write_text("{damaged")
    previous = read_checked(tmp_path, path, "fixture-v1")
    assert previous == {"kind": "ok", "source": "previous", "degraded": True, "payload": {"revision": 1}}
    # Repairing a broken current generation retains the valid fallback evidence.
    write_checked(tmp_path, path, "fixture-v1", {"revision": 3})
    assert read_checked(tmp_path, str(path) + ".prev", "fixture-v1")["payload"] == {"revision": 1}
    path.write_bytes(b"\xff")
    assert read_checked(tmp_path, path, "fixture-v1")["source"] == "previous"
    Path(str(path) + ".prev").write_text("corrupt")
    with pytest.raises(FuccError):
        read_checked(tmp_path, path, "fixture-v1")


def test_validator_and_wrong_checksum_cannot_be_trusted(tmp_path):
    path = tmp_path / "state.json"
    validator = lambda value: type(value) is dict and set(value) == {"ok"} and value["ok"] is True
    with pytest.raises(FuccError):
        write_checked(tmp_path, path, "fixture-v1", {"ok": True, "extra": 1}, validator)
    write_checked(tmp_path, path, "fixture-v1", {"ok": True}, validator)
    envelope = json.loads(path.read_text())
    envelope["payload"] = {"ok": False}
    path.write_text(json.dumps(envelope))
    with pytest.raises(FuccError):
        read_checked(tmp_path, path, "fixture-v1", validator)


@pytest.mark.parametrize("which", ["root", "parent", "current", "previous"])
def test_symlinks_are_rejected(tmp_path, which):
    root = tmp_path / "state"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = root / "value.json"
    if which == "root":
        root.rmdir()
        root.symlink_to(outside, target_is_directory=True)
    elif which == "parent":
        (root / "nested").symlink_to(outside, target_is_directory=True)
        target = root / "nested/value.json"
    elif which == "current":
        (outside / "value").write_text("do not overwrite")
        target.symlink_to(outside / "value")
    else:
        (outside / "value").write_text("do not overwrite")
        Path(str(target) + ".prev").symlink_to(outside / "value")
    with pytest.raises(FuccError):
        read_checked(root, target, "fixture-v1")
    # Both current and previous are rejected before replacement.
    if which != "previous":
        with pytest.raises(FuccError):
            write_checked(root, target, "fixture-v1", {})
    assert not (outside / "value.json").exists()


def test_literal_descendant_required(tmp_path):
    with pytest.raises(FuccError):
        write_checked(tmp_path, tmp_path / "../outside.json", "fixture-v1", {})
    with pytest.raises(FuccError):
        read_checked(tmp_path, tmp_path, "fixture-v1")


def test_directory_replacement_and_intermediate_symlink_races_fail_closed(tmp_path):
    import fucc.state as state
    root = tmp_path / "state"
    parent = root / "nested/deep"
    parent.mkdir(parents=True)
    path = parent / "value.json"
    write_checked(root, path, "fixture-v1", {"original": True})
    with state._Boundary(root, path) as boundary:
        (root / "nested").rename(root / "saved")
        (root / "nested").symlink_to(root / "saved", target_is_directory=True)
        # The terminal parent retains its inode. The changed intermediate
        # directory must still be detected before using the retained fd.
        with pytest.raises(FuccError):
            boundary.read(path.name)


def test_failed_atomic_replace_preserves_current_and_cleans_owned_temporary(tmp_path, monkeypatch):
    import fucc.state as state
    path = tmp_path / "value.json"
    write_checked(tmp_path, path, "fixture-v1", {"revision": 1})
    replace = state.os.replace

    def fail_current(source, destination, **kwargs):
        if destination == path.name:
            raise OSError("replace unavailable")
        return replace(source, destination, **kwargs)

    monkeypatch.setattr(state.os, "replace", fail_current)
    with pytest.raises(FuccError):
        write_checked(tmp_path, path, "fixture-v1", {"revision": 2})
    assert read_checked(tmp_path, path, "fixture-v1")["payload"] == {"revision": 1}
    assert not any(".fucc-tmp-" in candidate.name for candidate in tmp_path.iterdir())


def test_file_fsync_error_is_not_treated_as_supported_success(tmp_path, monkeypatch):
    import fucc.state as state
    path = tmp_path / "value.json"

    def unavailable(_):
        raise OSError("fsync unavailable")

    monkeypatch.setattr(state.os, "fsync", unavailable)
    with pytest.raises(FuccError):
        write_checked(tmp_path, path, "fixture-v1", {"revision": 1})
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_current_io_failure_can_read_previous_but_cannot_write(tmp_path, monkeypatch):
    import fucc.state as state
    path = tmp_path / "value.json"
    write_checked(tmp_path, path, "fixture-v1", {"revision": 1})
    write_checked(tmp_path, path, "fixture-v1", {"revision": 2})
    read = state._Boundary.read

    def denied(boundary, name):
        if name == path.name:
            raise PermissionError("cannot read current")
        return read(boundary, name)

    monkeypatch.setattr(state._Boundary, "read", denied)
    assert read_checked(tmp_path, path, "fixture-v1")["source"] == "previous"
    with pytest.raises(FuccError):
        write_checked(tmp_path, path, "fixture-v1", {"revision": 3})


def test_lock_record_linux_identity_and_cas_release(tmp_path):
    lock = MutationLock(tmp_path, "resource-key")
    with lock:
        record = json.loads(lock.path.read_text())
        assert set(record) == {"pid", "startedAtMs", "heartbeatAtMs", "lockId"}
        assert record["pid"] == os.getpid()
        assert record["startedAtMs"] == process_start_ticks(os.getpid())
        with pytest.raises(FuccError) as caught:
            MutationLock(tmp_path, "resource-key").acquire()
        assert caught.value.exit_code == 20
        lock.heartbeat()
        # Heartbeat advances independently. Release keys ownership by lockId.
        record["heartbeatAtMs"] += 1
        lock.path.write_text(json.dumps(record))
    assert list((tmp_path / "locks").iterdir()) == []


def test_dead_and_pid_reused_owner_require_explicit_recovery(tmp_path):
    lock = MutationLock(tmp_path, "resource-key")
    lock.path.parent.mkdir()
    record = {"pid": os.getpid(), "startedAtMs": process_start_ticks(os.getpid()) + 1,
              "heartbeatAtMs": 1, "lockId": "prior-owner"}
    lock.path.write_text(json.dumps(record))
    with pytest.raises(FuccError) as caught:
        lock.acquire()
    assert caught.value.exit_code == 21
    assert json.loads(lock.path.read_text()) == record
    with MutationLock(tmp_path, "resource-key", recover=True):
        assert json.loads(lock.path.read_text())["lockId"] != "prior-owner"
    assert not lock.path.exists()


def test_unknown_process_liveness_never_allows_takeover(tmp_path, monkeypatch):
    import fucc.state as state
    lock = MutationLock(tmp_path, "resource-key", recover=True)
    lock.path.parent.mkdir()
    record = {"pid": 2147483647, "startedAtMs": 1, "heartbeatAtMs": 1, "lockId": "owner"}
    lock.path.write_text(json.dumps(record))
    original_query = state.process_start_ticks

    def inaccessible(pid):
        if pid == record["pid"]:
            raise PermissionError("inaccessible process")
        return original_query(pid)

    monkeypatch.setattr(state, "process_start_ticks", inaccessible)
    with pytest.raises(FuccError):
        lock.acquire()
    assert json.loads(lock.path.read_text()) == record


def test_live_legacy_process_blocks_python(tmp_path, old_fucc_bundle):
    root = tmp_path / "state"
    root.mkdir()
    (root / "locks").mkdir()
    code = f"import * as old from {json.dumps(old_fucc_bundle.as_uri())};\n" + """
const root=process.argv[1];
const lock=await old.createFileMutationCoordinator().acquire({lockKey:`${root}${String.fromCharCode(0)}local-transaction`,stateRoot:root,filePath:`${root}/local-transaction.json`,operation:'persist.protect'});
console.log('locked');
await new Promise(resolve => process.stdin.once('data',resolve));
await lock.release();
"""
    process = subprocess.Popen(["node", "--input-type=module", "-e", code, str(root)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(FuccError) as caught:
            MutationLock(root, str(root) + "\0local-transaction").acquire()
        assert caught.value.exit_code == 20
    finally:
        process.communicate("release\n", timeout=10)
    assert process.returncode == 0
    assert list((root / "locks").iterdir()) == []


def test_python_process_blocks_legacy_coordinator(tmp_path, old_fucc_bundle):
    with MutationLock(tmp_path, str(tmp_path) + "\0local-transaction"):
        result = node(old_fucc_bundle, """
const root=process.argv[1];
try { await old.createFileMutationCoordinator().acquire({lockKey:`${root}${String.fromCharCode(0)}local-transaction`,stateRoot:root,filePath:`${root}/local-transaction.json`,operation:'persist.restore'}); console.log('unsafe'); }
catch(error) { console.log(error.constructor.name); }
""", tmp_path)
        assert result == "MutationBusyError"


def test_lock_name_is_legacy_sha256(tmp_path):
    key = str(tmp_path) + "\0local-transaction"
    lock = MutationLock(tmp_path, key)
    assert lock.path.name == hashlib.sha256(key.encode()).hexdigest() + ".lock"
