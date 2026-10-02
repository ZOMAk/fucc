"""Dynamic targets require a state format that predecessor runtimes reject."""
import copy
import json

import pytest

from fucc.errors import FuccError
from fucc.state import read_checked, write_checked
from fucc.transaction import (LocalTransaction, Resource, SCHEMA, SCHEMA_V2,
                              matches_resource_claim, read_committed_target, read_resource_bindings, valid_document)
from test_state import build_old_bundle, node


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / "state"
    values = {"a": "Asia/Tokyo", "b": {"private": "baseline-secret"}}
    resources = [Resource(id, str(tmp_path / f"{id}.json"),
                          lambda id=id: copy.deepcopy(values[id]),
                          lambda value, id=id: values.__setitem__(id, copy.deepcopy(value))) for id in values]
    return root, values, resources


def document(root, schema=SCHEMA_V2, previous=False):
    path = root / ("local-transaction.json.prev" if previous else "local-transaction.json")
    return read_checked(root, path, schema)["payload"]


def test_v2_plan_is_published_to_both_generations_before_resource_write(fixture):
    root, values, resources = fixture
    engine = LocalTransaction(root, resources)
    assert engine.apply("us", {"a": "America/New_York", "b": "en-US"})["kind"] == "committed"
    original = resources[0].write

    def inspect(value):
        current, previous = document(root), document(root, previous=True)
        assert current == previous
        assert current["version"] == 2
        assert current["journal"]["kind"] == "apply"
        original(value)

    resources[0].write = inspect
    assert engine.apply("us", {"a": "America/Los_Angeles", "b": "en-US"}, wire_version=2) == {"kind": "committed"}
    assert values["a"] == "America/Los_Angeles"
    assert matches_resource_claim(root, "a", "America/Los_Angeles")
    assert read_resource_bindings(root) == [{"id": resource.id, "identity": resource.identity} for resource in resources]


def test_default_instance_preserves_v2_activity_and_restores_original_baseline(fixture):
    root, values, resources = fixture
    engine = LocalTransaction(root, resources, wire_version=2)
    assert engine.apply("us", {"a": "America/Los_Angeles", "b": "en-US"})["kind"] == "committed"
    default = LocalTransaction(root, resources)
    assert default.status()["health"] == "healthy"
    assert default.apply("us", {"a": "America/Chicago", "b": "en-US"}) == {"kind": "committed"}
    assert document(root)["version"] == 2
    assert default.committed_desired("a") == "America/Chicago"
    assert default.restore() == {"kind": "restored"}
    assert values == {"a": "Asia/Tokyo", "b": {"private": "baseline-secret"}}
    assert default.committed_desired("a") is None
    assert document(root, SCHEMA)["version"] == document(root, SCHEMA, previous=True)["version"] == 1


def test_failed_city_change_compensates_without_recapturing_baseline(fixture):
    root, values, resources = fixture
    engine = LocalTransaction(root, resources)
    engine.apply("us", {"a": "America/New_York", "b": "east"})
    engine.apply("us", {"a": "America/Los_Angeles", "b": "west"}, wire_version=2)
    writer = resources[1].write

    def fail_chicago(value):
        if value == "central":
            raise OSError("temporary resource failure")
        writer(value)

    resources[1].write = fail_chicago
    assert engine.apply("us", {"a": "America/Chicago", "b": "central"}) == {"kind": "compensated"}
    assert engine.committed_desired("a") == "America/Los_Angeles"
    assert values == {"a": "America/Los_Angeles", "b": "west"}
    assert document(root)["committed"]["resources"][0]["original"] == "Asia/Tokyo"
    assert engine.restore() == {"kind": "restored"}
    assert values == {"a": "Asia/Tokyo", "b": {"private": "baseline-secret"}}


def test_pending_v2_apply_keeps_committed_target_and_recovers_in_default_instance(fixture):
    root, values, resources = fixture
    engine = LocalTransaction(root, resources, wire_version=2)
    engine.apply("us", {"a": "America/Los_Angeles", "b": "west"})
    writer_a, writer_b = resources[0].write, resources[1].write

    def fail_rollback(value):
        if value == "America/Los_Angeles":
            raise OSError("rollback failed")
        writer_a(value)

    def fail_apply(value):
        if value == "central":
            raise OSError("apply failed")
        writer_b(value)

    resources[0].write, resources[1].write = fail_rollback, fail_apply
    assert engine.apply("us", {"a": "America/Chicago", "b": "central"}) == {"kind": "recovery_required"}
    assert engine.committed_desired("a") == "America/Los_Angeles"
    assert matches_resource_claim(root, "a", "America/Chicago")
    assert engine.status()["transaction"] == "apply"
    resources[0].write, resources[1].write = writer_a, writer_b
    default = LocalTransaction(root, resources)
    assert default.recover() == {"kind": "recovered"}
    assert document(root)["version"] == 2
    assert default.committed_desired("a") == "America/Los_Angeles"
    assert values == {"a": "America/Los_Angeles", "b": "west"}
    assert default.restore() == {"kind": "restored"}


def test_first_v2_apply_compensation_returns_both_generations_to_v1(fixture):
    root, values, resources = fixture
    engine = LocalTransaction(root, resources, wire_version=2)
    resources[1].write = lambda _: (_ for _ in ()).throw(OSError("apply failed"))
    assert engine.apply("us", {"a": "America/Los_Angeles", "b": "west"}) == {"kind": "compensated"}
    assert values == {"a": "Asia/Tokyo", "b": {"private": "baseline-secret"}}
    assert document(root, SCHEMA)["version"] == document(root, SCHEMA, previous=True)["version"] == 1


def test_interrupted_v2_restore_recovers_forward_and_downgrades_only_when_empty(fixture):
    root, values, resources = fixture
    engine = LocalTransaction(root, resources, wire_version=2)
    engine.apply("us", {"a": "America/Los_Angeles", "b": "west"})
    writer = resources[1].write
    resources[1].write = lambda _: (_ for _ in ()).throw(OSError("restore failed"))
    assert engine.restore() == {"kind": "recovery_required"}
    assert values == {"a": "Asia/Tokyo", "b": "west"}
    assert document(root)["journal"]["kind"] == "restore"
    assert document(root, previous=True)["version"] == 2
    assert engine.committed_desired("a") == "America/Los_Angeles"
    resources[1].write = writer
    assert LocalTransaction(root, resources).recover() == {"kind": "recovered"}
    assert values == {"a": "Asia/Tokyo", "b": {"private": "baseline-secret"}}
    assert document(root, SCHEMA)["committed"]["region"] is None
    assert document(root, SCHEMA, previous=True)["version"] == 1


def test_corrupt_current_v2_falls_back_to_v2_plan_without_losing_baseline(fixture):
    root, values, resources = fixture
    engine = LocalTransaction(root, resources, wire_version=2)
    engine.apply("us", {"a": "America/Los_Angeles", "b": "west"})
    (root / "local-transaction.json").write_text("damaged")
    assert engine.status()["health"] == "recovery_required"
    assert engine.status()["transaction"] == "apply"
    assert LocalTransaction(root, resources).recover() == {"kind": "recovered"}
    assert values == {"a": "Asia/Tokyo", "b": {"private": "baseline-secret"}}
    assert document(root, SCHEMA)["committed"]["region"] is None
    assert document(root, SCHEMA, previous=True)["version"] == 1


def test_failed_upgrade_publish_cannot_write_resources_before_two_v2_generations(fixture, monkeypatch):
    root, values, resources = fixture
    engine = LocalTransaction(root, resources)
    engine.apply("us", {"a": "America/New_York", "b": "east"})
    save = engine._save
    attempts = 0
    writes = []
    writer = resources[0].write
    resources[0].write = lambda value: (writes.append(value), writer(value))

    def fail_second_publish(payload):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("second journal publication failed")
        save(payload)

    monkeypatch.setattr(engine, "_save", fail_second_publish)
    assert engine.apply("us", {"a": "America/Los_Angeles", "b": "west"}, wire_version=2) == {"kind": "compensated"}
    assert writes == []
    assert values == {"a": "America/New_York", "b": "east"}
    assert document(root)["committed"]["resources"][0]["original"] == "Asia/Tokyo"
    assert engine.restore() == {"kind": "restored"}


def test_committed_desired_is_checked_bound_and_cloned(fixture):
    root, _, resources = fixture
    engine = LocalTransaction(root, resources, wire_version=2)
    assert engine.committed_desired("b") is None
    engine.apply("us", {"a": "America/Los_Angeles", "b": {"locale": "en-US"}})
    desired = engine.committed_desired("b")
    desired["locale"] = "user-edit"
    assert engine.committed_desired("b") == {"locale": "en-US"}
    assert engine.committed_desired("unmanaged") is None
    assert "baseline-secret" not in json.dumps(engine.status())
    with pytest.raises(FuccError):
        engine.committed_desired("bad/id")
    with pytest.raises(FuccError):
        LocalTransaction(root, resources[:1]).committed_desired("a")
    (root / "local-transaction.json").write_text("corrupt")
    with pytest.raises(FuccError):
        engine.committed_desired("a")


@pytest.fixture
def unified(tmp_path):
    root = tmp_path / "persist" / "local"
    values = {"bash-profile": {"region": None}, "session-environment": {"private": "baseline-secret"}}
    resources = [Resource(id, str(root.parent) if id == "bash-profile" else str(tmp_path / "session.env"),
                          lambda id=id: copy.deepcopy(values[id]),
                          lambda value, id=id: values.__setitem__(id, copy.deepcopy(value))) for id in values]
    return root, values, resources


def test_read_committed_target_is_pure_and_returns_only_checked_target(unified):
    root, _, resources = unified
    assert read_committed_target(root) is None and not root.exists()
    engine = LocalTransaction(root, resources)
    assert engine.apply("us", {"bash-profile": {"region": "us"}, "session-environment": "US"})["kind"] == "committed"
    before = {path: path.read_bytes() for path in root.iterdir() if path.is_file()}
    for resource in resources:
        resource.read = lambda: (_ for _ in ()).throw(AssertionError("must not read live resources"))
        resource.write = lambda _: (_ for _ in ()).throw(AssertionError("must not write live resources"))
    assert read_committed_target(root) == {"region": "us"}
    assert before == {path: path.read_bytes() for path in root.iterdir() if path.is_file()}
    target = read_committed_target(root)
    target["region"] = "changed"
    assert read_committed_target(root) == {"region": "us"}
    assert "baseline-secret" not in json.dumps(target)


def _pending_unified_document(root, requested_region="us", requested_zone="America/Chicago"):
    payload = document(root)
    previous = copy.deepcopy(payload["committed"])
    target = copy.deepcopy(previous)
    target["region"] = requested_region
    target["revision"] += 1
    for resource in target["resources"]:
        resource["desired"] = {"region": requested_region, "timezone": requested_zone} if resource["id"] == "bash-profile" else "pending"
    entries = [{"id": before["id"], "identity": before["identity"], "before": before["desired"], "desired": after["desired"]}
               for before, after in zip(previous["resources"], target["resources"])]
    payload["journal"] = {"kind": "apply", "previous": previous, "target": target, "entries": entries}
    write_checked(root, root / "local-transaction.json", SCHEMA_V2, payload, valid_document)


def test_read_committed_target_uses_outer_target_when_nested_bash_has_changed(unified):
    root, values, resources = unified
    saved = {"region": "us", "timezone": "America/Los_Angeles"}
    engine = LocalTransaction(root, resources, wire_version=2)
    engine.apply("us", {"bash-profile": saved, "session-environment": "saved"})
    _pending_unified_document(root)
    values["bash-profile"] = {"region": "us", "timezone": "America/Chicago"}
    assert read_committed_target(root) == saved


@pytest.mark.parametrize("fault", ["identity", "missing-bash", "region", "alias", "foreign-zone", "default-override", "v1-dynamic"])
def test_read_committed_target_rejects_invalid_authority_without_fallback(unified, fault):
    root, _, resources = unified
    engine = LocalTransaction(root, resources, wire_version=2)
    engine.apply("us", {"bash-profile": {"region": "us", "timezone": "America/Los_Angeles"}, "session-environment": "saved"})
    payload = document(root)
    bash = next(resource for resource in payload["committed"]["resources"] if resource["id"] == "bash-profile")
    if fault == "identity":
        bash["identity"] = str(root / "different-owner")
    elif fault == "missing-bash":
        payload["committed"]["resources"].remove(bash)
    elif fault == "region":
        bash["desired"]["region"] = "jp"
    elif fault == "alias":
        bash["desired"]["timezone"] = "US/Pacific"
    elif fault == "foreign-zone":
        bash["desired"]["timezone"] = "Asia/Tokyo"
    elif fault == "default-override":
        bash["desired"]["timezone"] = "America/New_York"
    elif fault == "v1-dynamic":
        payload["version"] = 1
    write_checked(root, root / "local-transaction.json", SCHEMA if fault == "v1-dynamic" else SCHEMA_V2, payload)
    with pytest.raises(FuccError) as exc:
        read_committed_target(root)
    assert exc.value.code == "RECOVERY_REQUIRED"


def test_read_committed_target_rejects_bad_journal_binding_and_degraded_generation(unified):
    root, _, resources = unified
    LocalTransaction(root, resources, wire_version=2).apply(
        "us", {"bash-profile": {"region": "us", "timezone": "America/Los_Angeles"}, "session-environment": "saved"})
    _pending_unified_document(root)
    payload = document(root)
    payload["journal"]["entries"][0]["identity"] += "-tampered"
    write_checked(root, root / "local-transaction.json", SCHEMA_V2, payload)
    with pytest.raises(FuccError):
        read_committed_target(root)


@pytest.mark.parametrize("schema,version", [("fucc-local-transaction-v3", 3), (SCHEMA_V2, 3)])
def test_read_committed_target_rejects_future_state_without_previous_fallback(unified, schema, version):
    root, _, resources = unified
    LocalTransaction(root, resources, wire_version=2).apply(
        "us", {"bash-profile": {"region": "us", "timezone": "America/Los_Angeles"}, "session-environment": "saved"})
    payload = document(root)
    payload["version"] = version
    write_checked(root, root / "local-transaction.json", schema, payload)
    before = (root / "local-transaction.json").read_bytes()
    with pytest.raises(FuccError) as exc:
        read_committed_target(root)
    assert exc.value.code == "RECOVERY_REQUIRED"
    assert (root / "local-transaction.json").read_bytes() == before


def test_read_committed_target_rejects_invalid_pending_target_even_when_committed_is_valid(unified):
    root, _, resources = unified
    LocalTransaction(root, resources, wire_version=2).apply(
        "us", {"bash-profile": {"region": "us", "timezone": "America/Los_Angeles"}, "session-environment": "saved"})
    _pending_unified_document(root, requested_zone="Asia/Tokyo")
    with pytest.raises(FuccError):
        read_committed_target(root)
    (root / "local-transaction.json").write_text("damaged")
    with pytest.raises(FuccError):
        read_committed_target(root)


def test_read_committed_target_rejects_initial_pending_and_allows_complete_inactive(unified):
    root, _, resources = unified
    engine = LocalTransaction(root, resources, wire_version=2)
    initial = {"region": "us", "timezone": "America/Los_Angeles"}
    engine.apply("us", {"bash-profile": initial, "session-environment": "saved"})
    payload = document(root)
    target = payload["committed"]
    empty = {"region": None, "revision": target["revision"] - 1, "resources": []}
    entries = [{"id": resource["id"], "identity": resource["identity"], "before": resource["original"], "desired": resource["desired"]}
               for resource in target["resources"]]
    payload = {"version": 2, "committed": empty,
               "journal": {"kind": "apply", "previous": empty, "target": target, "entries": entries}}
    write_checked(root, root / "local-transaction.json", SCHEMA_V2, payload, valid_document)
    with pytest.raises(FuccError):
        read_committed_target(root)
    assert engine.recover()["kind"] == "recovered"
    assert read_committed_target(root) is None


@pytest.mark.parametrize("schema,version", [("fucc-local-transaction-v3", 3), (SCHEMA_V2, 3)])
def test_unknown_wire_version_blocks_recovery_before_nested_mutation(fixture, schema, version):
    root, values, resources = fixture
    engine = LocalTransaction(root, resources)
    engine.apply("us", {"a": "America/New_York", "b": "east"})
    future = document(root, SCHEMA)
    future["version"] = version
    write_checked(root, root / "local-transaction.json", schema, future)
    called = []
    fresh = LocalTransaction(root, resources, before_recover=lambda: called.append(True))
    assert fresh.status()["health"] == "recovery_required"
    assert fresh.recover() == {"kind": "recovery_required"}
    assert called == []
    assert fresh.restore() == {"kind": "recovery_required"}
    with pytest.raises(FuccError):
        read_resource_bindings(root)
    with pytest.raises(FuccError):
        matches_resource_claim(root, "a", "America/New_York")
    assert values == {"a": "America/New_York", "b": "east"}


@pytest.mark.parametrize("version", [0, 3, True, "2"])
def test_constructor_and_apply_reject_invalid_wire_versions(fixture, version):
    root, _, resources = fixture
    with pytest.raises(FuccError):
        LocalTransaction(root, resources, wire_version=version)
    with pytest.raises(FuccError):
        LocalTransaction(root, resources).apply("us", {"a": "America/New_York", "b": "east"}, wire_version=version)
    assert not root.exists()


def test_document_validator_supports_only_known_payload_versions():
    empty = {"committed": {"region": None, "revision": 0, "resources": []}, "journal": None}
    assert valid_document({**empty, "version": 1})
    assert valid_document({**empty, "version": 2})
    assert not valid_document({**empty, "version": 3})


@pytest.mark.parametrize("pending", [False, True])
def test_predecessor_cannot_mutate_active_or_interrupted_v2_and_can_read_after_off(tmp_path, pending):
    bundle = build_old_bundle(tmp_path)
    root = tmp_path / "state"
    files = {id: tmp_path / f"{id}.json" for id in ("a", "b")}
    for id, path in files.items():
        path.write_text(json.dumps("baseline-" + id))
    resources = [Resource(id, str(path), lambda path=path: json.loads(path.read_text()),
                          lambda value, path=path: path.write_text(json.dumps(value))) for id, path in files.items()]
    engine = LocalTransaction(root, resources, wire_version=2)
    assert engine.apply("us", {"a": "America/Los_Angeles", "b": "west"})["kind"] == "committed"
    if pending:
        writer_a, writer_b = resources[0].write, resources[1].write

        def a(value):
            if value == "America/Los_Angeles":
                raise OSError("rollback failed")
            writer_a(value)

        def b(value):
            if value == "central":
                raise OSError("apply failed")
            writer_b(value)

        resources[0].write, resources[1].write = a, b
        assert engine.apply("us", {"a": "America/Chicago", "b": "central"})["kind"] == "recovery_required"
        resources[0].write, resources[1].write = writer_a, writer_b
    before = {path: path.read_text() for path in [*files.values(), root / "local-transaction.json", root / "local-transaction.json.prev"]}
    response = json.loads(node(bundle, """
import {readFile,writeFile} from 'node:fs/promises';
const root=process.argv[1],base=process.argv[2];let writes=0;
const resources=['a','b'].map(id=>({id,identity:`${base}/${id}.json`,read:async()=>JSON.parse(await readFile(`${base}/${id}.json`,'utf8')),write:async value=>{writes++;await writeFile(`${base}/${id}.json`,JSON.stringify(value));}}));
const engine=old.createLocalTransaction({root,resources});
console.log(JSON.stringify({recover:(await engine.recover()).kind,restore:(await engine.restore()).kind,apply:(await engine.apply('us',{a:'NY',b:'east'})).kind,health:(await engine.status()).health,writes}));
""", root, tmp_path))
    assert response == {"recover": "recovery_required", "restore": "recovery_required",
                        "apply": "recovery_required", "health": "recovery_required", "writes": 0}
    assert all(path.read_text() == text for path, text in before.items())
    default = LocalTransaction(root, resources)
    assert default.recover()["kind"] == ("recovered" if pending else "noop")
    assert default.restore() == {"kind": "restored"}
    assert [json.loads(path.read_text()) for path in files.values()] == ["baseline-a", "baseline-b"]
    assert node(bundle, """
import {readFile,writeFile} from 'node:fs/promises';
const root=process.argv[1],base=process.argv[2];
const resources=['a','b'].map(id=>({id,identity:`${base}/${id}.json`,read:async()=>JSON.parse(await readFile(`${base}/${id}.json`,'utf8')),write:async value=>writeFile(`${base}/${id}.json`,JSON.stringify(value))}));
const engine=old.createLocalTransaction({root,resources});
console.log([ (await engine.status()).health,(await engine.apply('jp',{a:'JP',b:'ja-JP'})).kind,(await engine.restore()).kind ].join(','));
""", root, tmp_path) == "healthy,committed,restored"
    assert [json.loads(path.read_text()) for path in files.values()] == ["baseline-a", "baseline-b"]
