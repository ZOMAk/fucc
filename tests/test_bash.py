import json
from pathlib import Path
import shutil
import select
import subprocess
from unittest.mock import patch

import pytest

from fucc.bash import (BACKUP_SCHEMA, BashRuntime, EMPTY_ENV, JOURNAL_SCHEMA, STATE_SCHEMA, STATE_SCHEMA_V2,
                       VALUES_SCHEMA, _backup_valid, _context_valid, _journal_valid, _state_valid,
                       _store, _values_valid, environment_script, parse_environment, read_persist_state)
from fucc.errors import FuccError
from fucc.regions import target_environment
from fucc.state import MutationLock, read_checked, write_checked


@pytest.fixture
def setup(tmp_path):
    environment = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config with ' quote"),
                   "SHELL": "/bin/bash", "LANG": "zh_CN.UTF-8"}
    original = "export CUDA_HOME=/opt/cuda\n# original no final newline"
    (tmp_path / ".bashrc").write_text(original)
    options = {"environment": environment, "list_locales": lambda: ["en_US.utf8", "en_GB.utf8", "en_SG.utf8"]}
    return BashRuntime(**options), original, options


def test_lifecycle_preserves_first_backup_and_user_bashrc_edits(setup):
    runtime, original, options = setup
    assert runtime.status()["health"] == "healthy"
    assert runtime.set_preferred_region("sg") == {"kind": "updated", "preferredRegion": "sg"}
    assert runtime.protect("us")["kind"] == "committable"
    backup_path = runtime.root / "linux-backup.json"
    backup = backup_path.read_bytes()
    assert runtime.protect({"mode": "standard", "region": "us"})["kind"] == "noop"
    assert backup_path.read_bytes() == backup
    runtime.bashrc_path.write_text(runtime.bashrc_path.read_text() + '\nexport NVM_DIR="$HOME/.nvm"\n')
    assert runtime.protect("eu")["kind"] == "committable"
    assert backup_path.read_bytes() == backup
    assert BashRuntime(**options).restore()["kind"] == "restored"
    assert runtime.bashrc_path.read_text() == original + '\nexport NVM_DIR="$HOME/.nvm"\n'
    assert not runtime.env_path.exists()
    assert json.loads(backup_path.read_text())["payload"]["active"] is False
    assert runtime.recover()["kind"] == "noop"
    assert runtime.restore()["kind"] == "noop"
    assert runtime.status()["preferredRegion"] == "eu"


def test_dynamic_us_timezone_preserves_baseline_and_survives_restart(setup):
    runtime, original, options = setup
    runtime.protect("us")
    backup = (runtime.root / "linux-backup.json").read_bytes()
    target = {"mode": "standard", "region": "us", "timezone": "America/Los_Angeles"}
    assert runtime.protect(target)["kind"] == "committable"
    assert (runtime.root / "linux-backup.json").read_bytes() == backup
    for name in ("state.json", "state.json.prev"):
        assert json.loads((runtime.root / name).read_text())["schema"] == STATE_SCHEMA_V2
    resumed = BashRuntime(**options)
    assert resumed.status()["target"] == target
    assert resumed.status()["platform"]["configured"] == target_environment("us", "America/Los_Angeles")
    assert resumed.status()["health"] == "healthy"
    assert resumed.protect(target)["kind"] == "noop"
    assert resumed.protect("us")["kind"] == "committable"
    assert resumed.status()["target"] == {"mode": "standard", "region": "us"}
    assert resumed.restore()["kind"] == "restored"
    assert resumed.bashrc_path.read_text() == original and not resumed.env_path.exists()
    assert read_persist_state(runtime.root)[0]["schemaVersion"] == 2


def test_dynamic_bash_alias_is_canonical_and_default_keeps_original_schema(setup):
    runtime, _, _ = setup
    runtime.protect({"mode": "standard", "region": "us", "timezone": "US/Eastern"})
    assert runtime.status()["target"] == {"mode": "standard", "region": "us"}
    assert json.loads((runtime.root / "state.json").read_text())["schema"] == STATE_SCHEMA
    runtime.protect({"mode": "standard", "region": "us", "timezone": "US/Pacific"})
    assert runtime.status()["target"]["timezone"] == "America/Los_Angeles"


@pytest.mark.parametrize("zone", ["Asia/Tokyo", "../../etc/passwd", "America/Los_Angeles\n", "US", None, True])
def test_invalid_dynamic_timezone_never_writes_configuration(setup, zone):
    runtime, original, _ = setup
    with pytest.raises(FuccError):
        runtime.protect({"mode": "standard", "region": "us", "timezone": zone})
    assert runtime.bashrc_path.read_text() == original and not runtime.env_path.exists()
    assert not (runtime.root / "linux-backup.json").exists()


def test_dynamic_apply_compensation_returns_saved_timezone(setup):
    runtime, _, options = setup
    saved = {"mode": "standard", "region": "us", "timezone": "America/Los_Angeles"}
    runtime.protect(saved)
    real_write = runtime._write_environment

    def fail_new(value):
        if value["value"]["TZ"] == "America/Chicago":
            raise OSError("simulated write failure")
        return real_write(value)

    with patch.object(runtime, "_write_environment", side_effect=fail_new):
        assert runtime.protect({**saved, "timezone": "America/Chicago"})["kind"] == "compensated"
    resumed = BashRuntime(**options)
    assert resumed.status()["target"] == saved
    assert resumed.status()["platform"]["configured"]["TZ"] == "America/Los_Angeles"


def test_dynamic_restore_recovers_original_without_ip_lookup(setup):
    runtime, original, options = setup
    runtime.protect({"mode": "standard", "region": "us", "timezone": "America/Los_Angeles"})
    from fucc.bash import replace_snapshot as real_replace

    def fail_unlink(path, desired, expected):
        if Path(path) == runtime.env_path and desired["text"] is None:
            raise OSError("interrupted restore")
        return real_replace(path, desired, expected)

    with patch("fucc.bash.replace_snapshot", side_effect=fail_unlink):
        assert runtime.restore()["kind"] == "recovery_required"
    assert BashRuntime(**options).recover()["kind"] == "recovered"
    assert runtime.bashrc_path.read_text() == original and not runtime.env_path.exists()


def test_dynamic_incomplete_apply_recovers_last_committed_timezone(setup):
    runtime, _, options = setup
    saved = {"mode": "standard", "region": "us", "timezone": "America/Los_Angeles"}
    runtime.protect(saved)
    backup = (runtime.root / "linux-backup.json").read_bytes()
    real_write = runtime._write_environment

    def fail_write(value):
        if value["value"]["TZ"] == "America/Chicago":
            real_write(value)
        raise OSError("interrupted apply and compensation")

    with patch.object(runtime, "_write_environment", side_effect=fail_write):
        assert runtime.protect({**saved, "timezone": "America/Chicago"})["kind"] == "recovery_required"
    resumed = BashRuntime(**options)
    assert resumed.status()["target"] == saved and resumed.status()["health"] == "recovery_required"
    assert resumed.recover()["kind"] == "recovered"
    assert resumed.status()["platform"]["configured"]["TZ"] == "America/Los_Angeles"
    assert (runtime.root / "linux-backup.json").read_bytes() == backup


def test_read_persist_state_is_read_only_and_keeps_v2_gate(setup, tmp_path):
    absent = tmp_path / "never-created"
    assert read_persist_state(absent) == (None, False) and not absent.exists()
    runtime, _, _ = setup
    runtime.protect({"mode": "standard", "region": "us", "timezone": "America/Los_Angeles"})
    before = {path: path.read_bytes() for path in runtime.root.iterdir() if path.is_file()}
    state, degraded = read_persist_state(runtime.root)
    assert state["committedTarget"]["timezone"] == "America/Los_Angeles" and not degraded
    assert before == {path: path.read_bytes() for path in runtime.root.iterdir() if path.is_file()}
    (runtime.root / "state.json").write_text("{corrupt")
    state, degraded = read_persist_state(runtime.root)
    assert state["schemaVersion"] == 2 and degraded


def test_new_bash_reads_exact_quoted_hook(setup):
    import subprocess
    runtime, _, _ = setup
    runtime.protect("us")
    result = subprocess.run(["/bin/bash", "--noprofile", "--rcfile", str(runtime.bashrc_path), "-ic",
                             'printf "%s\\n" "$TZ" "$LANG" "$LC_ALL" "$CUDA_HOME"'],
                            capture_output=True, text=True, check=True, env=runtime.environment)
    assert result.stdout.strip().splitlines()[-4:] == ["America/New_York", "en_US.UTF-8", "en_US.UTF-8", "/opt/cuda"]
    assert runtime.status()["platform"]["currentProcessAligned"] is False


@pytest.mark.parametrize("change", ["env", "hook", "duplicate"])
def test_conflicts_are_reported_and_never_overwritten(setup, change):
    runtime, _, _ = setup
    runtime.protect("us")
    path = runtime.env_path if change == "env" else runtime.bashrc_path
    text = path.read_text()
    modified = text.replace("America/New_York", "Asia/Tokyo") if change == "env" else text.replace("then .", "then echo") if change == "hook" else text + "\n# >>> fucc environment >>>\n"
    path.write_text(modified)
    assert runtime.status()["health"] == "degraded"
    for action in (runtime.restore, lambda: runtime.protect("eu")):
        with pytest.raises(FuccError) as exc:
            action()
        assert exc.value.code == "CONFIG_CONFLICT"
    assert path.read_text() == modified
    assert runtime.status()["target"]["region"] == "us"


def test_unavailable_locale_and_shell_reject_before_backup(setup):
    runtime, original, options = setup
    with pytest.raises(FuccError) as exc:
        runtime.protect("jp")
    assert exc.value.code == "LOCALE_UNAVAILABLE"
    with pytest.raises(FuccError) as exc:
        runtime.protect({"mode": "deep", "region": "us"})
    assert exc.value.code == "UNSUPPORTED_FEATURE"
    runtime = BashRuntime(**{**options, "environment": {**options["environment"], "SHELL": "/bin/zsh"}})
    with pytest.raises(FuccError) as exc:
        runtime.protect("us")
    assert exc.value.code == "UNSUPPORTED_FEATURE"
    assert runtime.bashrc_path.read_text() == original
    assert not runtime.env_path.exists()
    assert not (runtime.root / "linux-backup.json").exists()


def test_missing_initial_bashrc_is_removed_but_new_user_text_is_kept(setup):
    runtime, _, _ = setup
    runtime.bashrc_path.unlink()
    runtime.protect("us")
    runtime.restore()
    assert not runtime.bashrc_path.exists()
    runtime.protect("us")
    runtime.bashrc_path.write_text(runtime.bashrc_path.read_text() + "export NEW_SETTING=keep\n")
    runtime.restore()
    assert runtime.bashrc_path.read_text() == "export NEW_SETTING=keep\n"


def test_incomplete_compensation_retains_journal_and_recovery_rejects_unrecorded_profile(setup):
    runtime, original, options = setup
    from fucc.bash import replace_snapshot as real_replace

    def fail_configuration(path, desired, expected):
        if Path(path) == runtime.bashrc_path or Path(path) == runtime.env_path and desired["text"] is None:
            raise OSError("simulated interruption")
        return real_replace(path, desired, expected)

    with patch("fucc.bash.replace_snapshot", side_effect=fail_configuration):
        assert runtime.protect("us")["kind"] == "recovery_required"
    resumed = BashRuntime(**options)
    pending = runtime.env_path.read_text()
    runtime.env_path.write_text(environment_script(target_environment("sg")))
    assert resumed.recover()["kind"] == "recovery_required"
    assert runtime.env_path.read_text() == environment_script(target_environment("sg"))
    runtime.env_path.write_text(pending)
    assert resumed.recover()["kind"] == "recovered"
    assert runtime.bashrc_path.read_text() == original
    assert not runtime.env_path.exists()


def test_partial_restore_recovers_forward(setup):
    runtime, original, options = setup
    runtime.protect("us")
    from fucc.bash import replace_snapshot as real_replace

    def fail_unlink(path, desired, expected):
        if Path(path) == runtime.env_path and desired["text"] is None:
            raise OSError("simulated interrupted restore")
        return real_replace(path, desired, expected)

    with patch("fucc.bash.replace_snapshot", side_effect=fail_unlink):
        assert runtime.restore() == {"kind": "recovery_required", "failed": ["environment"]}
    assert runtime.bashrc_path.read_text() == original
    assert BashRuntime(**options).recover()["kind"] == "recovered"
    assert not runtime.env_path.exists()


def test_backup_paths_and_identity_are_bound(setup):
    runtime, _, options = setup
    runtime.protect("us")
    with pytest.raises(FuccError) as exc:
        BashRuntime(**{**options, "environment": {**options["environment"], "XDG_CONFIG_HOME": str(runtime.root / "elsewhere")}})
    assert exc.value.code == "CONFIG_CONFLICT"
    captured = runtime._read_backup()
    write_checked(runtime.root, runtime.root / "linux-backup.json", BACKUP_SCHEMA, {**captured, "snapshotId": "replacement"})
    with pytest.raises(FuccError) as exc:
        runtime._retire_backup(captured)
    assert exc.value.code == "CONFIG_CONFLICT"


def test_consumes_existing_legacy_embedded_protect_journal(setup):
    runtime, original, options = setup
    state, _ = runtime._read_state()
    backup = {"schemaVersion": 1, "snapshotId": "legacy-original", "active": True, "envPath": str(runtime.env_path),
              "bashrcPath": str(runtime.bashrc_path), "bashrcExisted": True, "values": {"environment": _store(EMPTY_ENV)}}
    write_checked(runtime.root, runtime.root / "linux-backup.json", BACKUP_SCHEMA, backup)
    runtime.env_path.parent.mkdir(parents=True)
    runtime.env_path.write_text(environment_script(target_environment("us")))
    journal = {"transactionId": "legacy-interrupted", "kind": "protect", "steps": [{"id": "environment", "phase": "applying",
                "original": _store(EMPTY_ENV), "desired": _store(target_environment("us"))}],
               "context": {"previousState": {key: state[key] for key in ("committedTarget", "preferredRegion", "health", "degradation")},
                           "requestedTarget": {"mode": "standard", "region": "us"}}}
    write_checked(runtime.root, runtime.root / "transaction-journal.json", JOURNAL_SCHEMA, journal)
    write_checked(runtime.root, runtime.root / "state.json", STATE_SCHEMA, {**state, "activeTransactionId": journal["transactionId"]})
    assert BashRuntime(**options).recover() == {"kind": "recovered", "failed": []}
    assert runtime.bashrc_path.read_text() == original
    assert not runtime.env_path.exists()
    values = read_checked(runtime.root, runtime.root / "transaction-journal.json.values", VALUES_SCHEMA)["payload"]
    assert values["transactionId"] == "legacy-interrupted"
    assert runtime._read_backup()["snapshotId"] == "legacy-original"


def test_lost_state_never_recaptures_active_backup(setup):
    runtime, _, options = setup
    runtime.protect("us")
    backup_bytes = (runtime.root / "linux-backup.json").read_bytes()
    (runtime.root / "state.json").unlink()
    (runtime.root / "state.json.prev").unlink()
    with pytest.raises(FuccError) as exc:
        BashRuntime(**options)
    assert exc.value.code == "RECOVERY_CONTEXT_INVALID"
    assert (runtime.root / "linux-backup.json").read_bytes() == backup_bytes


def test_future_state_schema_cannot_fall_back(setup):
    runtime, _, _ = setup
    state, _ = runtime._read_state()
    write_checked(runtime.root, runtime.root / "state.json", STATE_SCHEMA, {**state, "schemaVersion": 2})
    with pytest.raises(FuccError) as exc:
        runtime.status()
    assert exc.value.code == "STATE_INVALID"


def test_valid_checksum_state_rejects_boolean_schema_version(setup):
    runtime, _, _ = setup
    state, _ = runtime._read_state()
    assert _state_valid({**state, "schemaVersion": 1.0})
    assert not _state_valid({**state, "schemaVersion": True})
    write_checked(runtime.root, runtime.root / "state.json", STATE_SCHEMA, {**state, "schemaVersion": True})
    # The envelope checksum is valid; the incompatible payload must still fail
    # instead of accepting true as Python's numeric value 1 or falling back.
    assert read_checked(runtime.root, runtime.root / "state.json", STATE_SCHEMA)["payload"]["schemaVersion"] is True
    with pytest.raises(FuccError) as exc:
        runtime.status()
    assert exc.value.code == "STATE_INVALID"


def test_valid_checksum_backup_rejects_boolean_schema_version(setup):
    runtime, _, _ = setup
    snapshot = runtime._create_backup({"environment": _store(EMPTY_ENV)})
    assert _backup_valid({**snapshot, "schemaVersion": 1.0})
    assert not _backup_valid({**snapshot, "schemaVersion": True})
    write_checked(runtime.root, runtime.root / "linux-backup.json", BACKUP_SCHEMA, {**snapshot, "schemaVersion": True})
    assert read_checked(runtime.root, runtime.root / "linux-backup.json", BACKUP_SCHEMA)["payload"]["schemaVersion"] is True
    with pytest.raises(FuccError) as exc:
        runtime._read_backup()
    assert exc.value.code == "BACKUP_CORRUPT"


@pytest.mark.parametrize("invalid", [None, True, 0, 1.5, "unexpected", [], {}, ["environment"]])
def test_journal_type_mismatches_are_validation_failures(invalid):
    previous = {"committedTarget": None, "preferredRegion": "us", "health": "healthy", "degradation": []}
    context = {"previousState": previous, "requestedTarget": {"mode": "standard", "region": "us"}}
    step = {"id": "environment", "phase": "applying", "original": _store(EMPTY_ENV), "desired": _store(target_environment("us"))}
    journal = {"transactionId": "journal", "kind": "protect", "steps": [step], "context": context}
    validators = [(_journal_valid, journal), (_context_valid, context),
                  (_values_valid, {"transactionId": "journal", "values": {"environment": {"original": step["original"], "desired": step["desired"]}}})]
    for validate, value in validators:
        assert validate(value)
        assert validate(invalid) is False
        for key in value:
            if type(invalid) is not type(value[key]) and not (validate is _context_valid and key == "requestedTarget" and invalid is None):
                assert validate({**value, key: invalid}) is False
    for key in step:
        assert _journal_valid({**journal, "steps": [{**step, key: invalid}]}) is False


def test_degraded_planned_journal_is_compensated_conservatively(setup):
    runtime, _, _ = setup
    state, _ = runtime._read_state()
    runtime._create_backup({"environment": _store(EMPTY_ENV)})
    journal = runtime._plan("protect", state, {"mode": "standard", "region": "us"},
                            [{"id": "environment", "original": _store(EMPTY_ENV), "desired": _store(target_environment("us"))}])
    runtime._commit(state, activeTransactionId=journal["transactionId"])
    runtime._transition(journal, "environment", "applying")
    runtime._write_environment(_store(target_environment("us")))
    # An interrupted write leaves a valid predecessor with planned phases,
    # although configuration was already changed. Recovery must roll it back.
    (runtime.root / "transaction-journal.json").write_text("{corrupt")
    assert runtime.recover() == {"kind": "recovered", "failed": []}
    assert runtime.status()["mode"] == "daily" and not runtime.env_path.exists()


@pytest.mark.parametrize("profile", [{"TZ": "A", "LANG": None, "LC_ALL": None}, {"TZ": None, "LANG": None}, {**EMPTY_ENV, "extra": True}])
def test_recovery_profiles_are_strict(setup, profile):
    runtime, original, _ = setup
    with pytest.raises(FuccError):
        runtime._write_environment(_store(profile))
    assert runtime.bashrc_path.read_text() == original
    assert not runtime.env_path.exists()


def test_unmanaged_and_symlink_files_are_preserved(setup):
    runtime, _, _ = setup
    runtime.env_path.parent.mkdir(parents=True)
    runtime.env_path.write_text("export PERSONAL_SETTING=keep\n")
    with pytest.raises(FuccError):
        runtime.protect("us")
    assert runtime.env_path.read_text() == "export PERSONAL_SETTING=keep\n"
    runtime.env_path.unlink()
    runtime.bashrc_path.unlink()
    outside = runtime.root.parent / "outside"
    outside.write_text("keep")
    runtime.bashrc_path.symlink_to(outside)
    with pytest.raises(FuccError):
        runtime.protect("us")
    assert outside.read_text() == "keep"


def test_shell_parser_is_canonical_and_never_evaluates_commands():
    profile = {key: "some'$(touch /tmp/not-executed)" for key in ("TZ", "LANG", "LC_ALL")}
    assert parse_environment(environment_script(profile)) == profile
    with pytest.raises(FuccError):
        parse_environment(environment_script(profile) + "echo injected\n")


@pytest.fixture(scope="module")
def legacy_bash(tmp_path_factory):
    """Optional source interoperability tests; the Python product needs no Node."""
    legacy = Path(__file__).resolve().parents[2] / "fucc"
    builds = sorted(legacy.glob("node_modules/.pnpm/esbuild@*/node_modules/esbuild/bin/esbuild"))
    node = shutil.which("node")
    if not node or not builds or not (legacy / "src/platform/linux/runtime.ts").exists():
        pytest.skip("Legacy TypeScript source and build tool are not available")
    root = tmp_path_factory.mktemp("legacy-bash-module")
    exports = {
        "createLinuxPersistRuntime": "platform/linux/runtime.ts",
        "createLinuxEnvironmentAuthority": "platform/linux/environment.ts",
        "TransactionJournalRepository": "state/journal.ts",
        "StateRepository": "state/repository.ts",
        "createFileMutationCoordinator": "state/mutation-coordinator.ts",
        "desiredValues": "persist/targets.ts",
    }
    entry = root / "exports.ts"
    entry.write_text("\n".join(f"export {{{name}}} from {json.dumps(str(legacy / 'src' / path))};" for name, path in exports.items()))
    subprocess.run([str(builds[0]), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={root / 'module.mjs'}"], check=True, capture_output=True)
    runner = root / "runner.mjs"
    runner.write_text("""
import {createLinuxPersistRuntime,createLinuxEnvironmentAuthority,TransactionJournalRepository,StateRepository,createFileMutationCoordinator,desiredValues} from './module.mjs';
import {join} from 'node:path';
const [root,action,region]=process.argv.slice(2);
const environment={HOME:join(root,'home'),XDG_CONFIG_HOME:join(root,'config'),SHELL:'/bin/bash'};
const options={root:join(root,'state'),environment,listLocales:async()=>['en_US.utf8','en_GB.utf8']};
const runtime=await createLinuxPersistRuntime(options);
if(action==='hold') {
 const lock=await createFileMutationCoordinator().acquire({stateRoot:options.root,filePath:options.root,lockKey:options.root+'\\0mutation-root',operation:'persist.protect'});
 console.log(JSON.stringify({kind:'held'}));
 process.stdin.resume();
 await new Promise(resolve=>process.stdin.once('data',resolve));
 await lock.release();
 process.stdin.pause();
} else if(action==='pending') {
 await runtime.protect({mode:'standard',region:'us'});
 const repo=new StateRepository({root:options.root,mutationCoordinator:createFileMutationCoordinator()});
 const journals=new TransactionJournalRepository(options.root,join(options.root,'transaction-journal.json'));
 const {value:state}=await repo.read();
 let journal=await journals.plan('protect',[{id:'environment',original:desiredValues(state.committedTarget).environment,desired:desiredValues({mode:'standard',region:'eu'}).environment}],{previousState:{committedTarget:state.committedTarget,preferredRegion:state.preferredRegion,health:state.health,degradation:state.degradation},requestedTarget:{mode:'standard',region:'eu'}});
 await repo.commit(state.revision,{committedTarget:state.committedTarget,preferredRegion:state.preferredRegion,health:state.health,degradation:state.degradation,activeTransactionId:journal.transactionId});
 await journals.transition(journal,'environment','applying');
 await createLinuxEnvironmentAuthority(environment).authority.write(desiredValues({mode:'standard',region:'eu'}).environment);
 console.log(JSON.stringify({kind:'pending'}));
} else console.log(JSON.stringify(action==='on'?await runtime.protect({mode:'standard',region}):action==='off'?await runtime.restore():action==='recover'?await runtime.recover():await runtime.status()));
""")

    def invoke(directory, action, region=None):
        result = subprocess.run([node, str(runner), str(directory), action] + ([region] if region else []),
                                check=True, text=True, capture_output=True)
        return json.loads(result.stdout)
    invoke.node, invoke.runner = node, runner
    return invoke


def _interop_runtime(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text("export USER_SETTING=keep\n")
    return {"environment": {"HOME": str(home), "XDG_CONFIG_HOME": str(tmp_path / "config"), "SHELL": "/bin/bash"},
            "list_locales": lambda: ["en_US.utf8", "en_GB.utf8"], "root": tmp_path / "state"}


def test_typescript_on_python_switch_and_off_preserve_original(legacy_bash, tmp_path):
    options = _interop_runtime(tmp_path)
    assert legacy_bash(tmp_path, "on", "us")["kind"] == "committable"
    runtime = BashRuntime(**options)
    backup = (runtime.root / "linux-backup.json").read_bytes()
    assert runtime.protect("eu")["kind"] == "committable"
    assert (runtime.root / "linux-backup.json").read_bytes() == backup
    assert legacy_bash(tmp_path, "status")["target"]["region"] == "eu"
    assert runtime.restore()["kind"] == "restored"
    assert legacy_bash(tmp_path, "status")["mode"] == "daily"
    assert runtime.bashrc_path.read_text() == "export USER_SETTING=keep\n"
    assert not runtime.env_path.exists()


def test_old_typescript_rejects_dynamic_bash_state_even_with_damaged_current(legacy_bash, tmp_path):
    runtime = BashRuntime(**_interop_runtime(tmp_path))
    runtime.protect({"mode": "standard", "region": "us", "timezone": "America/Los_Angeles"})
    original = runtime.env_path.read_text()
    with pytest.raises(subprocess.CalledProcessError):
        legacy_bash(tmp_path, "on", "us")
    assert runtime.env_path.read_text() == original
    (runtime.root / "state.json").write_text("{corrupt")
    with pytest.raises(subprocess.CalledProcessError):
        legacy_bash(tmp_path, "on", "us")
    assert runtime.env_path.read_text() == original


def test_typescript_pending_journal_python_recovery_preserves_external_edits(legacy_bash, tmp_path):
    options = _interop_runtime(tmp_path)
    assert legacy_bash(tmp_path, "pending")["kind"] == "pending"
    runtime = BashRuntime(**options)
    backup = (runtime.root / "linux-backup.json").read_bytes()
    interrupted = runtime.env_path.read_text()
    external = environment_script(target_environment("sg"))
    runtime.env_path.write_text(external)
    assert runtime.recover()["kind"] == "recovery_required"
    assert runtime.env_path.read_text() == external
    runtime.env_path.write_text(interrupted)
    assert runtime.recover()["kind"] == "recovered"
    assert runtime.status()["target"]["region"] == "us"
    assert (runtime.root / "linux-backup.json").read_bytes() == backup
    assert legacy_bash(tmp_path, "status")["target"]["region"] == "us"


def test_python_interrupted_restore_is_recoverable_by_typescript(legacy_bash, tmp_path):
    runtime = BashRuntime(**_interop_runtime(tmp_path))
    runtime.protect("us")
    from fucc.bash import replace_snapshot as real_replace

    def fail_unlink(path, desired, expected):
        if Path(path) == runtime.env_path and desired["text"] is None:
            raise OSError("interrupted restore")
        return real_replace(path, desired, expected)

    with patch("fucc.bash.replace_snapshot", side_effect=fail_unlink):
        assert runtime.restore()["kind"] == "recovery_required"
    assert legacy_bash(tmp_path, "recover")["kind"] == "recovered"
    assert runtime.status()["mode"] == "daily"
    assert runtime.bashrc_path.read_text() == "export USER_SETTING=keep\n"
    assert not runtime.env_path.exists()


def test_typescript_and_python_share_root_mutation_lock(legacy_bash, tmp_path):
    runtime = BashRuntime(**_interop_runtime(tmp_path))
    process = subprocess.Popen([legacy_bash.node, str(legacy_bash.runner), str(tmp_path), "hold"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([process.stdout], [], [], 5)[0], "legacy root lock did not become ready"
        assert json.loads(process.stdout.readline()) == {"kind": "held"}
        with pytest.raises(FuccError) as exc:
            runtime.protect("us")
        assert exc.value.code == "LOCK_BUSY"
        process.communicate("release\n", timeout=5)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    # The old process detects a live Python lock at the same hashed path too.
    with MutationLock(runtime.root, str(runtime.root) + "\0mutation-root"):
        with pytest.raises(subprocess.CalledProcessError) as exc:
            legacy_bash(tmp_path, "on", "us")
        assert "MutationBusyError" in exc.value.stderr
