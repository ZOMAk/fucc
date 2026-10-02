import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from fucc.errors import FuccError
from fucc.state import read_checked, write_checked
from fucc.transaction import (LocalTransaction, Resource, SCHEMA, _locale_key,
                              matches_resource_claim, read_resource_bindings, valid_document)
from test_state import node, build_old_bundle


@pytest.fixture
def old_fucc_bundle(tmp_path):
    return build_old_bundle(tmp_path)


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / "state"
    values = {"a": "original-a-secret", "b": {"original": "b-secret"}}
    resources = [Resource(id, str(tmp_path / (id + ".json")),
                          lambda id=id: copy.deepcopy(values[id]),
                          lambda value, id=id: values.__setitem__(id, copy.deepcopy(value))) for id in values]
    transaction = LocalTransaction(root, resources)
    return root, values, resources, transaction


def test_baseline_switch_and_restore(fixture):
    root, values, resources, engine = fixture
    assert engine.status()["health"] == "healthy"
    assert engine.apply("us", {"a": "US", "b": {"locale": "en-US"}}) == {"kind": "committed"}
    assert engine.apply("us", {"a": "US", "b": {"locale": "en-US"}}) == {"kind": "noop"}
    assert engine.apply("jp", {"a": "JP", "b": {"locale": "ja-JP"}}) == {"kind": "committed"}
    status = engine.status()
    assert (status["region"], status["revision"], status["health"]) == ("jp", 2, "healthy")
    assert all(item["aligned"] for item in status["resources"])
    assert not any(text in json.dumps(status) for text in ("secret", "locale", "ja-JP"))
    assert (root / "local-transaction.json").stat().st_mode & 0o777 == 0o600
    assert engine.restore() == {"kind": "restored"}
    assert values == {"a": "original-a-secret", "b": {"original": "b-secret"}}
    assert engine.status()["revision"] == 3
    assert engine.restore() == {"kind": "noop"}
    resources[0].baseline = lambda current: None
    assert engine.apply("sg", {"a": "SG", "b": "SG"}) == {"kind": "committed"}
    assert engine.restore() == {"kind": "restored"}
    assert values["a"] is None
    resources[0].baseline = None
    values["a"] = "new-baseline"
    assert engine.apply("eu", {"a": "EU", "b": "EU"}) == {"kind": "committed"}
    assert engine.restore() == {"kind": "restored"}
    assert values["a"] == "new-baseline"


def test_invalid_target_and_registry_are_rejected(fixture):
    root, _, resources, engine = fixture
    with pytest.raises(FuccError):
        LocalTransaction(root, [])
    with pytest.raises(FuccError):
        LocalTransaction(root, [resources[0], resources[0]])
    for region, desired in (("invalid", {"a": "US", "b": "US"}), ("us", {"a": "US"}),
                            ("us", {"a": "US", "b": "US", "extra": "private"})):
        with pytest.raises(FuccError):
            engine.apply(region, desired)


def test_failed_apply_compensates_all_resources(fixture):
    _, values, resources, engine = fixture
    assert engine.apply("us", {"a": "US", "b": "US"})["kind"] == "committed"
    write_b = resources[1].write

    def fail_jp(value):
        if value == "JP":
            raise RuntimeError("private adapter data")
        write_b(value)

    resources[1].write = fail_jp
    assert engine.apply("jp", {"a": "JP", "b": "JP"}) == {"kind": "compensated"}
    assert values == {"a": "US", "b": "US"}
    assert engine.status()["health"] == "healthy"
    assert engine.status()["revision"] == 1


def test_unfinished_apply_recovers_backwards(fixture):
    root, values, resources, engine = fixture
    engine.apply("us", {"a": "US", "b": "US"})
    write_a, write_b = resources[0].write, resources[1].write

    def fail_rollback(value):
        if value == "US":
            raise RuntimeError("rollback")
        write_a(value)

    def fail_apply(value):
        if value == "JP":
            raise RuntimeError("apply")
        write_b(value)

    resources[0].write, resources[1].write = fail_rollback, fail_apply
    assert engine.apply("jp", {"a": "JP", "b": "JP"}) == {"kind": "recovery_required"}
    assert values == {"a": "JP", "b": "US"}
    assert engine.status()["transaction"] == "apply"
    assert matches_resource_claim(root, "a", "JP")
    assert not matches_resource_claim(root, "a", "US")
    assert read_resource_bindings(root) == [{"id": item.id, "identity": item.identity} for item in resources]
    assert engine.restore() == {"kind": "recovery_required"}
    resources[0].write, resources[1].write = write_a, write_b
    fresh = LocalTransaction(root, reversed(resources))
    assert fresh.recover() == {"kind": "recovered"}
    assert values == {"a": "US", "b": "US"}
    assert fresh.status()["health"] == "healthy"


def test_unfinished_restore_recovers_forwards(fixture):
    root, values, resources, engine = fixture
    engine.apply("us", {"a": "US", "b": "US"})
    write_b = resources[1].write
    resources[1].write = lambda value: (_ for _ in ()).throw(RuntimeError("restore fails"))
    assert engine.restore() == {"kind": "recovery_required"}
    assert values == {"a": "original-a-secret", "b": "US"}
    assert matches_resource_claim(root, "a", "US")
    assert engine.status()["transaction"] == "restore"
    resources[1].write = write_b
    assert engine.recover() == {"kind": "recovered"}
    assert values == {"a": "original-a-secret", "b": {"original": "b-secret"}}
    assert engine.status()["active"] is False


def test_external_changes_and_changed_bindings_are_preserved(fixture):
    root, values, resources, engine = fixture
    engine.apply("us", {"a": "US", "b": "US"})
    values["a"] = "user-edited"
    assert engine.status()["health"] == "degraded"
    assert engine.apply("jp", {"a": "JP", "b": "JP"}) == {"kind": "recovery_required"}
    assert engine.restore() == engine.recover() == {"kind": "recovery_required"}
    assert values["a"] == "user-edited"
    values["a"] = "US"
    fewer = LocalTransaction(root, [resources[0]])
    assert fewer.status()["health"] == "recovery_required"
    assert fewer.restore() == fewer.recover() == {"kind": "recovery_required"}
    assert engine.restore() == {"kind": "restored"}
    assert fewer.apply("jp", {"a": "JP"}) == {"kind": "committed"}
    assert fewer.restore() == {"kind": "restored"}


def test_degraded_generation_with_plan_recovers_conservatively(fixture):
    root, values, _, engine = fixture
    engine.apply("us", {"a": "US", "b": "US"})
    (root / "local-transaction.json").write_text("corrupt")
    assert engine.status()["health"] == "recovery_required"
    assert engine.status()["transaction"] == "apply"
    assert engine.recover() == {"kind": "recovered"}
    assert values == {"a": "original-a-secret", "b": {"original": "b-secret"}}


def test_degraded_generation_without_plan_is_not_republished(fixture):
    root, values, _, engine = fixture
    engine.apply("us", {"a": "US", "b": "US"})
    path = root / "local-transaction.json"
    document = read_checked(root, path, SCHEMA)["payload"]
    write_checked(root, path, SCHEMA, document)
    path.write_text("corrupt")
    assert engine.recover() == {"kind": "recovery_required"}
    assert path.read_text() == "corrupt"
    assert values == {"a": "US", "b": "US"}


def test_initialized_marker_prevents_recapture_after_state_loss(fixture):
    root, values, _, engine = fixture
    engine.apply("us", {"a": "US", "b": "US"})
    for name in ("local-transaction.json", "local-transaction.json.prev"):
        (root / name).unlink()
    assert engine.status()["health"] == "recovery_required"
    assert engine.apply("jp", {"a": "JP", "b": "JP"}) == {"kind": "recovery_required"}
    assert engine.recover() == {"kind": "recovery_required"}
    assert values == {"a": "US", "b": "US"}
    with pytest.raises(FuccError):
        read_resource_bindings(root)


def test_checked_schema_rejects_unknown_fields_and_inconsistent_journals(fixture):
    root, values, _, engine = fixture
    engine.apply("us", {"a": "US", "b": "US"})
    invalid = {"version": 1, "committed": {"region": "us", "revision": 1, "resources": []}, "journal": None, "extra": "private"}
    path = root / "local-transaction.json"
    write_checked(root, path, SCHEMA, invalid)
    write_checked(root, str(path) + ".prev", SCHEMA, invalid)
    assert engine.restore() == {"kind": "recovery_required"}
    assert engine.status()["health"] == "recovery_required"
    assert values == {"a": "US", "b": "US"}
    assert not valid_document({"version": True, "committed": {"region": None, "revision": 0, "resources": []}, "journal": None})


def test_failed_commit_after_atomic_replace_reinstates_plan_before_compensation(fixture, monkeypatch):
    root, values, _, engine = fixture
    engine.apply("us", {"a": "US", "b": "US"})
    save = engine._save
    failed = False

    def flush_fails_after_replace(document):
        nonlocal failed
        save(document)
        if document["journal"] is None and document["committed"]["region"] == "jp" and not failed:
            failed = True
            raise OSError("parent directory flush failed after replace")

    monkeypatch.setattr(engine, "_save", flush_fails_after_replace)
    assert engine.apply("jp", {"a": "JP", "b": "JP"}) == {"kind": "compensated"}
    assert values == {"a": "US", "b": "US"}
    assert engine.status()["region"] == "us"
    assert engine.status()["revision"] == 1
    assert read_checked(root, str(engine.path) + ".prev", SCHEMA)["payload"]["journal"]["kind"] == "apply"


def test_both_generations_have_plan_before_first_resource_write(fixture):
    root, _, resources, engine = fixture
    original = resources[0].write

    def inspect_plan(value):
        current = read_checked(root, root / "local-transaction.json", SCHEMA)["payload"]
        previous = read_checked(root, root / "local-transaction.json.prev", SCHEMA)["payload"]
        assert current == previous
        assert current["journal"]["kind"] == "apply"
        original(value)

    resources[0].write = inspect_plan
    assert engine.apply("us", {"a": "US", "b": "US"}) == {"kind": "committed"}


def test_outer_lock_is_held_through_nested_recovery(fixture):
    root, _, resources, engine = fixture
    entered, release = threading.Event(), threading.Event()

    def nested():
        entered.set()
        assert release.wait(10)
        raise RuntimeError("nested failed")

    nested_engine = LocalTransaction(root, resources, before_recover=nested)
    result = []
    worker = threading.Thread(target=lambda: result.append(nested_engine.recover()))
    worker.start()
    assert entered.wait(10)
    try:
        with pytest.raises(FuccError) as caught:
            engine.apply("us", {"a": "US", "b": "US"})
        assert caught.value.exit_code == 20
    finally:
        release.set()
        worker.join(10)
    assert result == [{"kind": "recovery_required"}]
    assert list((root / "locks").iterdir()) == []


def test_ascii_resource_order_matches_icu_locale_compare(old_fucc_bundle):
    ids = [a + b + c for a in "0AaBbZz" for b in "_-.0AaBbZz" for c in "_-.0AaBbZz"]
    expected = json.loads(node(old_fucc_bundle,
                               "console.log(JSON.stringify(JSON.parse(process.argv[1]).sort((a,b)=>a.localeCompare(b))))",
                               json.dumps(ids)))
    assert sorted(ids, key=_locale_key) == expected


def test_legacy_state_can_switch_and_restore_in_python(tmp_path, old_fucc_bundle):
    files = {id: tmp_path / (id + ".json") for id in ("a", "b")}
    for id, path in files.items():
        path.write_text(json.dumps("baseline-" + id))
    root = tmp_path / "state"
    assert node(old_fucc_bundle, """
import {readFile,writeFile} from 'node:fs/promises';
const root=process.argv[1],base=process.argv[2];
const resources=['a','b'].map(id=>({id,identity:`${base}/${id}.json`,read:async()=>JSON.parse(await readFile(`${base}/${id}.json`,'utf8')),write:async value=>writeFile(`${base}/${id}.json`,JSON.stringify(value))}));
const engine=old.createLocalTransaction({root,resources});
console.log((await engine.apply('jp',{a:'JP',b:'JP'})).kind);
""", root, tmp_path) == "committed"
    resources = [Resource(id, str(path), lambda path=path: json.loads(path.read_text()),
                          lambda value, path=path: path.write_text(json.dumps(value))) for id, path in files.items()]
    engine = LocalTransaction(root, resources)
    assert engine.status()["health"] == "healthy"
    assert engine.apply("eu", {"a": "EU", "b": "EU"}) == {"kind": "committed"}
    assert engine.restore() == {"kind": "restored"}
    assert [json.loads(path.read_text()) for path in files.values()] == ["baseline-a", "baseline-b"]


def test_python_state_can_restore_in_legacy_runtime(tmp_path, old_fucc_bundle):
    files = {id: tmp_path / (id + ".json") for id in ("a", "b")}
    for id, path in files.items():
        path.write_text(json.dumps("baseline-" + id))
    resources = [Resource(id, str(path), lambda path=path: json.loads(path.read_text()),
                          lambda value, path=path: path.write_text(json.dumps(value))) for id, path in files.items()]
    root = tmp_path / "state"
    assert LocalTransaction(root, resources).apply("us", {"a": "US", "b": "US"}) == {"kind": "committed"}
    assert node(old_fucc_bundle, """
import {readFile,writeFile} from 'node:fs/promises';
const root=process.argv[1],base=process.argv[2];
const resources=['a','b'].map(id=>({id,identity:`${base}/${id}.json`,read:async()=>JSON.parse(await readFile(`${base}/${id}.json`,'utf8')),write:async value=>writeFile(`${base}/${id}.json`,JSON.stringify(value))}));
console.log((await old.createLocalTransaction({root,resources}).restore()).kind);
""", root, tmp_path) == "restored"
    assert [json.loads(path.read_text()) for path in files.values()] == ["baseline-a", "baseline-b"]


def test_killed_legacy_apply_can_recover_in_python(tmp_path, old_fucc_bundle):
    files = {id: tmp_path / (id + ".json") for id in ("a", "b")}
    for id, path in files.items():
        path.write_text(json.dumps("baseline-" + id))
    root = tmp_path / "state"
    code = "import * as old from " + json.dumps(old_fucc_bundle.as_uri()) + ";\n" + """
import {readFile,writeFile} from 'node:fs/promises';
const root=process.argv[1],base=process.argv[2];
const resources=['a','b'].map(id=>({id,identity:`${base}/${id}.json`,read:async()=>JSON.parse(await readFile(`${base}/${id}.json`,'utf8')),write:async value=>{
 if(id==='b') { setInterval(()=>{},1000); await new Promise(()=>{}); }
 await writeFile(`${base}/${id}.json`,JSON.stringify(value));console.log('mutated');
}}));
await old.createLocalTransaction({root,resources}).apply('us',{a:'US',b:'US'});
"""
    process = subprocess.Popen(["node", "--input-type=module", "-e", code, str(root), str(tmp_path)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "mutated"
    finally:
        process.kill()
        process.communicate(timeout=10)
    resources = [Resource(id, str(path), lambda path=path: json.loads(path.read_text()),
                          lambda value, path=path: path.write_text(json.dumps(value))) for id, path in files.items()]
    engine = LocalTransaction(root, resources)
    assert engine.restore() == {"kind": "recovery_required"}
    assert engine.recover() == {"kind": "recovered"}
    assert [json.loads(path.read_text()) for path in files.values()] == ["baseline-a", "baseline-b"]
    assert list((root / "locks").iterdir()) == []


def test_crashed_python_apply_can_recover_in_legacy_runtime(tmp_path, old_fucc_bundle):
    files = {id: tmp_path / (id + ".json") for id in ("a", "b")}
    for id, path in files.items():
        path.write_text(json.dumps("baseline-" + id))
    root = tmp_path / "state"
    script = """
import json,os,sys
from pathlib import Path
from fucc.transaction import LocalTransaction,Resource
root,base=Path(sys.argv[1]),Path(sys.argv[2])
def writer(id,path,value):
    path.write_text(json.dumps(value))
    if id=='a': os._exit(77)
resources=[Resource(id,str(base/(id+'.json')),lambda id=id:json.loads((base/(id+'.json')).read_text()),lambda value,id=id:writer(id,base/(id+'.json'),value))for id in ['a','b']]
LocalTransaction(root,resources).apply('us',{'a':'US','b':'US'})
"""
    process = subprocess.run([sys.executable, "-c", script, str(root), str(tmp_path)],
                             capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    assert process.returncode == 77
    assert node(old_fucc_bundle, """
import {readFile,writeFile} from 'node:fs/promises';
const root=process.argv[1],base=process.argv[2];
const resources=['a','b'].map(id=>({id,identity:`${base}/${id}.json`,read:async()=>JSON.parse(await readFile(`${base}/${id}.json`,'utf8')),write:async value=>writeFile(`${base}/${id}.json`,JSON.stringify(value))}));
console.log((await old.createLocalTransaction({root,resources}).recover()).kind);
""", root, tmp_path) == "recovered"
    assert [json.loads(path.read_text()) for path in files.values()] == ["baseline-a", "baseline-b"]
    assert list((root / "locks").iterdir()) == []
