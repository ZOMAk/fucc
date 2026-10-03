"""Off accepts exact saved baselines without accepting unknown file changes."""
import copy

import pytest

from fucc.state import read_checked
from fucc.transaction import (LocalTransaction, Resource, SCHEMAS, read_resource_bindings,
                              valid_document)


@pytest.fixture
def fixture(tmp_path):
    originals = {
        "a.desktop": {"text": "[Desktop Entry]\nExec=claude %U\nX-Claude-Generated=true\n", "mode": 0o600},
        "b.bash": {"region": None},
        "c.machine": {"policy": "original"},
    }
    desired = {
        "a.desktop": {"text": "[Desktop Entry]\nExec=fucc-launch claude %U\nX-FUCC-Managed=true\n", "mode": 0o644},
        "b.bash": {"region": "us"},
        "c.machine": {"policy": "en-US"},
    }
    values = copy.deepcopy(originals)
    writes = []

    def write(id, value):
        writes.append(id)
        values[id] = copy.deepcopy(value)

    resources = {id: Resource(id, str(tmp_path / id), lambda id=id: copy.deepcopy(values[id]),
                              lambda value, id=id: write(id, value)) for id in originals}
    return tmp_path / "state", originals, desired, values, resources, writes


def activate(fixture, version=1):
    root, _, desired, _, resources, writes = fixture
    engine = LocalTransaction(root, resources.values(), wire_version=version)
    assert engine.apply("us", desired) == {"kind": "committed"}
    writes.clear()
    return engine


@pytest.mark.parametrize("version", [1, 2, 3])
def test_mixed_original_and_desired_restore_skips_original_and_keeps_wire_plan(fixture, version):
    root, originals, desired, values, resources, writes = fixture
    engine = activate(fixture, version)
    values["a.desktop"] = copy.deepcopy(originals["a.desktop"])
    assert engine.status()["health"] == "degraded"

    def must_not_write(_):
        raise AssertionError("The exact original desktop must not be rewritten")

    resources["a.desktop"].write = must_not_write
    writer = resources["b.bash"].write

    def inspect_plan(value):
        current = read_checked(root, root / "local-transaction.json", SCHEMAS[version])["payload"]
        previous = read_checked(root, root / "local-transaction.json.prev", SCHEMAS[version])["payload"]
        assert current == previous and current["version"] == version
        assert valid_document(current)
        assert current["journal"]["kind"] == "restore"
        entries = {entry["id"]: entry for entry in current["journal"]["entries"]}
        assert all(entries[id]["before"] == desired[id] and entries[id]["desired"] == originals[id] for id in originals)
        writer(value)

    resources["b.bash"].write = inspect_plan
    assert engine.restore() == {"kind": "restored"}
    assert values == originals
    assert writes == ["b.bash", "c.machine"]
    assert engine.status()["active"] is False and engine.status()["health"] == "healthy"


def test_all_resources_already_original_restore_without_resource_writes(fixture):
    _, originals, _, values, _, writes = fixture
    engine = activate(fixture)
    values.update(copy.deepcopy(originals))
    assert engine.restore() == {"kind": "restored"}
    assert writes == [] and values == originals
    assert engine.restore() == {"kind": "noop"}


@pytest.mark.parametrize("changed", ["text", "mode", "later_resource"])
def test_unknown_value_aborts_before_journal_and_any_resource_write(fixture, changed, monkeypatch):
    root, originals, _, values, _, writes = fixture
    engine = activate(fixture)
    values["a.desktop"] = copy.deepcopy(originals["a.desktop"])
    if changed == "text":
        values["a.desktop"]["text"] += "Name=New user entry\n"
    elif changed == "mode":
        values["a.desktop"]["mode"] = 0o640
    else:
        values["c.machine"] = {"policy": "unknown-external-value"}
    before = {path: path.read_bytes() for path in root.iterdir() if path.is_file()}
    observed = copy.deepcopy(values)
    plans = []
    monkeypatch.setattr(engine, "_publish", lambda journal: plans.append(journal))
    assert engine.restore() == {"kind": "recovery_required"}
    assert plans == [] and writes == [] and values == observed
    assert before == {path: path.read_bytes() for path in root.iterdir() if path.is_file()}
    assert engine.status()["transaction"] == "none"


@pytest.mark.parametrize("operation", ["apply", "recover"])
def test_on_and_recover_without_pending_journal_remain_strict_and_do_not_overwrite(fixture, operation):
    root, originals, desired, values, _, writes = fixture
    engine = activate(fixture)
    values["a.desktop"] = copy.deepcopy(originals["a.desktop"])
    before = {path: path.read_bytes() for path in root.iterdir() if path.is_file()}
    result = engine.apply("us", desired) if operation == "apply" else engine.recover()
    assert result == {"kind": "recovery_required"}
    assert writes == [] and values["a.desktop"] == originals["a.desktop"]
    assert before == {path: path.read_bytes() for path in root.iterdir() if path.is_file()}
    assert engine.status()["transaction"] == "none"


@pytest.mark.parametrize("version", [1, 2, 3])
def test_interrupted_mixed_restore_recovers_forward_without_rewriting_originals(fixture, version):
    root, originals, _, values, resources, writes = fixture
    engine = activate(fixture, version)
    values["a.desktop"] = copy.deepcopy(originals["a.desktop"])
    writer = resources["c.machine"].write
    resources["c.machine"].write = lambda _: (_ for _ in ()).throw(OSError("interrupted restore"))
    assert engine.restore() == {"kind": "recovery_required"}
    assert values["a.desktop"] == originals["a.desktop"] and values["b.bash"] == originals["b.bash"]
    assert writes == ["b.bash"]
    pending = read_checked(root, root / "local-transaction.json", SCHEMAS[version])["payload"]
    assert valid_document(pending) and pending["journal"]["kind"] == "restore"
    resources["c.machine"].write = writer
    writes.clear()
    assert LocalTransaction(root, resources.values()).recover() == {"kind": "recovered"}
    assert writes == ["c.machine"] and values == originals


def test_restore_uses_projected_original_not_the_initial_live_value(fixture):
    _, originals, _, values, resources, writes = fixture
    values["b.bash"] = {"region": "jp"}
    resources["b.bash"].baseline = lambda _: {"region": None}
    engine = activate(fixture)
    assert engine.committed_original("b.bash") == {"region": None}
    values["b.bash"] = {"region": "jp"}
    assert engine.restore() == {"kind": "recovery_required"}
    assert writes == []  # The initial live value is not the projected saved baseline.
    values["b.bash"] = {"region": None}
    assert engine.restore() == {"kind": "restored"}
    assert "b.bash" not in writes and values == originals


def test_enrolled_resources_restore_immutable_old_originals_without_recapture(fixture):
    root, originals, desired, values, resources, writes = fixture
    old_ids = ["a.desktop", "c.machine"]
    old = LocalTransaction(root, [resources[id] for id in old_ids])
    assert old.apply("us", {id: desired[id] for id in old_ids}) == {"kind": "committed"}
    engine = LocalTransaction(root, resources.values())
    assert engine.apply("us", desired, enroll=True) == {"kind": "committed"}
    assert engine.committed_original("a.desktop") == originals["a.desktop"]
    values["a.desktop"] = copy.deepcopy(originals["a.desktop"])
    writes.clear()
    assert engine.restore() == {"kind": "restored"}
    assert values == originals and "a.desktop" not in writes
    assert read_resource_bindings(root) == []


def test_restore_does_not_relax_resource_identity_or_registry_checks(fixture):
    root, originals, _, values, resources, writes = fixture
    activate(fixture)
    values.update(copy.deepcopy(originals))
    fewer = LocalTransaction(root, [resources["a.desktop"], resources["b.bash"]])
    assert fewer.restore() == {"kind": "recovery_required"}
    changed = Resource("a.desktop", resources["a.desktop"].identity + "-other",
                       resources["a.desktop"].read, resources["a.desktop"].write)
    assert LocalTransaction(root, [changed, resources["b.bash"], resources["c.machine"]]).restore() == {"kind": "recovery_required"}
    assert writes == [] and values == originals
