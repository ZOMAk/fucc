"""Enrollment preserves existing baselines while admitting new resources."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from fucc.errors import FuccError
from fucc.state import read_checked, write_checked
from fucc.transaction import (LocalTransaction, Resource, SCHEMA, SCHEMA_V3,
                              read_committed_target, read_resource_bindings, valid_document)


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / "state"
    values = {"a": "original-a", "b": {"secret": "original-b"}, "c": "original-c"}
    resources = {id: Resource(id, str(tmp_path / f"{id}.json"),
                              lambda id=id: copy.deepcopy(values[id]),
                              lambda value, id=id: values.__setitem__(id, copy.deepcopy(value))) for id in values}
    return root, values, resources


def document(root, schema=SCHEMA_V3, previous=False):
    return read_checked(root, root / ("local-transaction.json.prev" if previous else "local-transaction.json"), schema)["payload"]


def old_registry(resources):
    return [resources["a"], resources["c"]]


@pytest.mark.parametrize("version", [1, 2])
def test_enrollment_preserves_old_baselines_by_id_and_captures_new_original_once(fixture, version):
    root, values, resources = fixture
    old = LocalTransaction(root, old_registry(resources), wire_version=version)
    assert old.apply("us", {"a": "A-us", "c": "C-us"})["kind"] == "committed"
    captures = []
    resources["b"].baseline = lambda before: captures.append(copy.deepcopy(before)) or before
    engine = LocalTransaction(root, resources.values())
    assert engine.apply("us", {"a": "A-us", "b": "B-us", "c": "C-us"}, enroll=True) == {"kind": "committed"}
    originals = {item["id"]: item["original"] for item in document(root)["committed"]["resources"]}
    assert originals == {"a": "original-a", "b": {"secret": "original-b"}, "c": "original-c"}
    assert captures == [{"secret": "original-b"}]
    assert engine.apply("us", {"a": "A-us", "b": "B-us", "c": "C-us"}, enroll=True) == {"kind": "noop"}
    assert engine.apply("jp", {"a": "A-jp", "b": "B-jp", "c": "C-jp"}) == {"kind": "committed"}
    assert document(root)["version"] == 3
    assert captures == [{"secret": "original-b"}]
    assert engine.restore() == {"kind": "restored"}
    assert values == {"a": "original-a", "b": {"secret": "original-b"}, "c": "original-c"}
    assert document(root, SCHEMA)["version"] == document(root, SCHEMA, previous=True)["version"] == 1


def test_enrollment_publishes_both_v3_generations_before_any_resource_write(fixture):
    root, _, resources = fixture
    LocalTransaction(root, old_registry(resources)).apply("us", {"a": "A-us", "c": "C-us"})
    writer = resources["a"].write

    def inspect(value):
        current, previous = document(root), document(root, previous=True)
        assert current == previous
        assert current["version"] == 3
        assert [item["id"] for item in current["journal"]["previous"]["resources"]] == ["a", "c"]
        assert [item["id"] for item in current["journal"]["target"]["resources"]] == ["a", "b", "c"]
        assert [item["id"] for item in current["journal"]["entries"]] == ["a", "b", "c"]
        writer(value)

    resources["a"].write = inspect
    assert LocalTransaction(root, resources.values()).apply("jp", {"a": "A-jp", "b": "B-jp", "c": "C-jp"}, enroll=True) == {"kind": "committed"}


def test_enrollment_requires_explicit_opt_in_and_cannot_remove_or_rebind_old_resources(fixture):
    root, values, resources = fixture
    LocalTransaction(root, old_registry(resources)).apply("us", {"a": "A-us", "c": "C-us"})
    before = (root / "local-transaction.json").read_bytes()
    assert LocalTransaction(root, resources.values()).apply("us", {"a": "A-us", "b": "B-us", "c": "C-us"}) == {"kind": "recovery_required"}
    assert LocalTransaction(root, [resources["a"], resources["b"]]).apply("us", {"a": "A-us", "b": "B-us"}, enroll=True) == {"kind": "recovery_required"}
    changed = Resource("c", resources["c"].identity + "-other", resources["c"].read, resources["c"].write)
    assert LocalTransaction(root, [resources["a"], resources["b"], changed]).apply("us", {"a": "A-us", "b": "B-us", "c": "C-us"}, enroll=True) == {"kind": "recovery_required"}
    assert (root / "local-transaction.json").read_bytes() == before
    assert values == {"a": "A-us", "b": {"secret": "original-b"}, "c": "C-us"}


def test_unregistered_new_resource_is_untouched_by_saved_registry_off(fixture):
    root, values, resources = fixture
    LocalTransaction(root, old_registry(resources)).apply("us", {"a": "A-us", "c": "C-us"})
    resources["b"].read = lambda: (_ for _ in ()).throw(AssertionError("not enrolled"))
    resources["b"].write = lambda _: (_ for _ in ()).throw(AssertionError("not enrolled"))
    assert LocalTransaction(root, old_registry(resources)).restore() == {"kind": "restored"}
    assert values["b"] == {"secret": "original-b"}


def test_complete_enrollment_compensation_commits_previous_registry(fixture):
    root, values, resources = fixture
    LocalTransaction(root, old_registry(resources)).apply("us", {"a": "A-us", "c": "C-us"})
    writer = resources["c"].write

    def fail(value):
        if value == "C-jp":
            raise OSError("apply failure")
        writer(value)

    resources["c"].write = fail
    engine = LocalTransaction(root, resources.values())
    assert engine.apply("jp", {"a": "A-jp", "b": "B-jp", "c": "C-jp"}, enroll=True) == {"kind": "compensated"}
    assert values == {"a": "A-us", "b": {"secret": "original-b"}, "c": "C-us"}
    assert [binding["id"] for binding in read_resource_bindings(root)] == ["a", "c"]
    assert engine.status()["health"] == "recovery_required"  # Caller reconstructs the saved registry.
    saved = LocalTransaction(root, old_registry(resources))
    assert saved.status()["health"] == "healthy"
    assert saved.committed_original("c") == "original-c"
    assert saved.restore() == {"kind": "restored"}
    assert values["a"] == "original-a" and values["c"] == "original-c"


def interrupt_enrollment(root, resources):
    LocalTransaction(root, old_registry(resources)).apply("us", {"a": "A-us", "c": "C-us"})
    writer_b, writer_c = resources["b"].write, resources["c"].write

    def fail_rollback(value):
        if type(value) is dict:
            raise OSError("rollback failure")
        writer_b(value)

    def fail_apply(value):
        if value == "C-jp":
            raise OSError("apply failure")
        writer_c(value)

    resources["b"].write, resources["c"].write = fail_rollback, fail_apply
    result = LocalTransaction(root, resources.values()).apply("jp", {"a": "A-jp", "b": "B-jp", "c": "C-jp"}, enroll=True)
    resources["b"].write, resources["c"].write = writer_b, writer_c
    assert result == {"kind": "recovery_required"}


def test_pending_enrollment_recovers_union_back_to_saved_previous_registry(fixture):
    root, values, resources = fixture
    interrupt_enrollment(root, resources)
    assert [binding["id"] for binding in read_resource_bindings(root)] == ["a", "b", "c"]
    assert LocalTransaction(root, old_registry(resources)).recover() == {"kind": "recovery_required"}
    engine = LocalTransaction(root, resources.values())
    assert engine.committed_desired("c") == "C-us"
    assert engine.committed_original("c") == "original-c"
    assert engine.committed_original("b") is None
    assert engine.recover() == {"kind": "recovered"}
    assert values == {"a": "A-us", "b": {"secret": "original-b"}, "c": "C-us"}
    assert [binding["id"] for binding in read_resource_bindings(root)] == ["a", "c"]
    assert LocalTransaction(root, old_registry(resources)).status()["health"] == "healthy"
    assert LocalTransaction(root, old_registry(resources)).restore() == {"kind": "restored"}


def test_corrupt_current_enrollment_recovers_from_previous_v3_journal(fixture):
    root, values, resources = fixture
    LocalTransaction(root, old_registry(resources)).apply("us", {"a": "A-us", "c": "C-us"})
    LocalTransaction(root, resources.values()).apply("jp", {"a": "A-jp", "b": "B-jp", "c": "C-jp"}, enroll=True)
    (root / "local-transaction.json").write_text("damaged")
    engine = LocalTransaction(root, resources.values())
    with pytest.raises(FuccError):
        engine.committed_original("c")
    assert engine.recover() == {"kind": "recovered"}
    assert values == {"a": "A-us", "b": {"secret": "original-b"}, "c": "C-us"}
    assert [binding["id"] for binding in read_resource_bindings(root)] == ["a", "c"]


def test_committed_original_clones_validates_and_remains_internal(fixture):
    root, _, resources = fixture
    engine = LocalTransaction(root, resources.values())
    engine.apply("us", {"a": "A-us", "b": "B-us", "c": "C-us"}, enroll=True)
    original = engine.committed_original("b")
    original["secret"] = "modified"
    assert engine.committed_original("b") == {"secret": "original-b"}
    assert "original-b" not in json.dumps(engine.status())
    assert engine.committed_original("missing") is None
    with pytest.raises(FuccError):
        engine.committed_original("unsafe/id")
    with pytest.raises(FuccError):
        LocalTransaction(root, old_registry(resources)).committed_original("b")


@pytest.mark.parametrize("change", ["original", "before", "identity", "removal", "v2"])
def test_enrollment_validator_rejects_old_baseline_or_binding_changes(fixture, change):
    root, _, resources = fixture
    interrupt_enrollment(root, resources)
    payload = document(root)
    journal = payload["journal"]
    if change == "original":
        journal["target"]["resources"][0]["original"] = "recaptured-active-value"
    elif change == "before":
        journal["entries"][0]["before"] = "other"
    elif change == "identity":
        journal["entries"][0]["identity"] += "-changed"
        journal["target"]["resources"][0]["identity"] += "-changed"
    elif change == "removal":
        journal["entries"].pop()
        journal["target"]["resources"].pop()
    else:
        payload["version"] = 2
    assert not valid_document(payload)


def test_read_committed_target_accepts_canonical_dynamic_timezone_in_v3(tmp_path):
    root = tmp_path / "persist" / "local"
    values = {"bash-profile": {"region": None}, "session": None, "new-app": None}
    resources = {id: Resource(id, str(root.parent) if id == "bash-profile" else str(tmp_path / id),
                              lambda id=id: copy.deepcopy(values[id]),
                              lambda value, id=id: values.__setitem__(id, copy.deepcopy(value))) for id in values}
    old = LocalTransaction(root, [resources["bash-profile"], resources["session"]], wire_version=2)
    target = {"region": "us", "timezone": "America/Los_Angeles"}
    old.apply("us", {"bash-profile": target, "session": "west"})
    engine = LocalTransaction(root, resources.values())
    assert engine.apply("us", {"bash-profile": target, "session": "west", "new-app": "enabled"}, enroll=True)["kind"] == "committed"
    assert read_committed_target(root) == target


@pytest.mark.parametrize("pending", [False, True])
def test_actual_python_002_rejects_active_and_interrupted_v3_without_resource_writes(tmp_path, pending):
    old_wheel = Path(__file__).resolve().parents[1] / "dist" / "fucc-0.0.2-py3-none-any.whl"
    if not old_wheel.is_file():
        pytest.skip("Predecessor Python 0.0.2 wheel unavailable")
    root = tmp_path / "state"
    files = {id: tmp_path / f"{id}.json" for id in ("a", "b", "c")}
    for id, path in files.items():
        path.write_text(json.dumps("original-" + id))
    resources = {id: Resource(id, str(path), lambda path=path: json.loads(path.read_text()),
                              lambda value, path=path: path.write_text(json.dumps(value))) for id, path in files.items()}
    LocalTransaction(root, old_registry(resources)).apply("us", {"a": "A-us", "c": "C-us"})
    if pending:
        writer_b, writer_c = resources["b"].write, resources["c"].write
        resources["b"].write = lambda value: (_ for _ in ()).throw(OSError("rollback")) if value == "original-b" else writer_b(value)
        resources["c"].write = lambda value: (_ for _ in ()).throw(OSError("apply")) if value == "C-jp" else writer_c(value)
        assert LocalTransaction(root, resources.values()).apply("jp", {"a": "A-jp", "b": "B-jp", "c": "C-jp"}, enroll=True)["kind"] == "recovery_required"
        resources["b"].write, resources["c"].write = writer_b, writer_c
    else:
        assert LocalTransaction(root, resources.values()).apply("us", {"a": "A-us", "b": "B-us", "c": "C-us"}, enroll=True)["kind"] == "committed"
    before = {path: path.read_bytes() for path in [*files.values(), root / "local-transaction.json", root / "local-transaction.json.prev"]}
    code = """
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from fucc import __version__
from fucc.transaction import LocalTransaction,Resource,SCHEMAS
if __version__!='0.0.2' or 3 in SCHEMAS:
    raise SystemExit(77)
root,base=Path(sys.argv[2]),Path(sys.argv[3]);writes=[]
def write(id,value):
    writes.append(id);(base/(id+'.json')).write_text(json.dumps(value))
resources=[Resource(id,str(base/(id+'.json')),lambda id=id:json.loads((base/(id+'.json')).read_text()),lambda value,id=id:write(id,value)) for id in ['a','b','c']]
engine=LocalTransaction(root,resources)
print(json.dumps({'recover':engine.recover()['kind'],'off':engine.restore()['kind'],'on':engine.apply('us',{'a':'A-us','b':'B-us','c':'C-us'})['kind'],'health':engine.status()['health'],'writes':writes}))
"""
    process = subprocess.run([sys.executable, "-I", "-c", code, str(old_wheel), str(root), str(tmp_path)],
                             capture_output=True, text=True, env={**os.environ, "PYTHONPATH": ""})
    if process.returncode == 77:
        pytest.skip("Available 0.0.2 wheel already contains newer transaction format")
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {"recover": "recovery_required", "off": "recovery_required", "on": "recovery_required",
                                         "health": "recovery_required", "writes": []}
    assert all(path.read_bytes() == contents for path, contents in before.items())
    current = LocalTransaction(root, resources.values())
    assert current.recover()["kind"] == ("recovered" if pending else "noop")
    restored = LocalTransaction(root, [resources[binding["id"]] for binding in read_resource_bindings(root)])
    assert restored.restore() == {"kind": "restored"}
    assert [json.loads(path.read_text()) for path in files.values()] == ["original-a", "original-b", "original-c"]


@pytest.mark.parametrize("flag", [1, "true", None])
def test_enrollment_flag_rejects_non_bool(fixture, flag):
    root, _, resources = fixture
    with pytest.raises(FuccError):
        LocalTransaction(root, resources.values()).apply("us", {"a": "A-us", "b": "B-us", "c": "C-us"}, enroll=flag)
    assert not root.exists()


def test_initial_v3_apply_restores_all_originals(fixture):
    root, values, resources = fixture
    engine = LocalTransaction(root, resources.values())
    assert engine.apply("us", {"a": "A-us", "b": "B-us", "c": "C-us"}, enroll=True) == {"kind": "committed"}
    assert document(root)["version"] == 3
    assert engine.restore() == {"kind": "restored"}
    assert values == {"a": "original-a", "b": {"secret": "original-b"}, "c": "original-c"}


def test_reading_v3_does_not_downgrade_a_no_change_apply(fixture):
    root, _, resources = fixture
    target = {"a": "A-us", "b": "B-us", "c": "C-us"}
    LocalTransaction(root, resources.values()).apply("us", target, enroll=True)
    engine = LocalTransaction(root, resources.values(), wire_version=1)
    assert engine.apply("us", target) == {"kind": "noop"}
    assert document(root)["version"] == 3
    assert engine.apply("jp", {"a": "A-jp", "b": "B-jp", "c": "C-jp"}) == {"kind": "committed"}
    assert document(root)["version"] == 3


def test_unknown_v4_rejected_without_falling_back_to_prior_v3(fixture):
    root, _, resources = fixture
    engine = LocalTransaction(root, resources.values())
    engine.apply("us", {"a": "A-us", "b": "B-us", "c": "C-us"}, enroll=True)
    future = document(root)
    future["version"] = 4
    write_checked(root, root / "local-transaction.json", "fucc-local-transaction-v4", future)
    assert engine.recover() == {"kind": "recovery_required"}
    with pytest.raises(FuccError):
        read_resource_bindings(root)
