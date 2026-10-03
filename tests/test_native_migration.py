"""Native configuration migrations use only isolated homes and a fake machine.

The legacy fixture freezes the 0.0.4 resource set and desired file format;
it does not rely on a previously built wheel or the mutable Git HEAD.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from fucc import apps
from fucc.errors import FuccError
from fucc.files import read_snapshot
from fucc.regions import target_environment
from fucc.runtime import LocalRuntime
from fucc.transaction import LocalTransaction, Resource, read_resource_bindings, valid_document
from test_runtime import FakeMachine


CLAUDE_ID = "com.anthropic.Claude.desktop"
DESKTOPS = {
    "google-chrome.desktop": ("Google Chrome", "/usr/bin/google-chrome %U"),
    "microsoft-edge.desktop": ("Microsoft Edge", "/usr/bin/microsoft-edge %U"),
    "code.desktop": ("Visual Studio Code", "/usr/bin/code %F"),
    "firefox.desktop": ("Firefox", "/usr/lib/firefox/firefox %u"),
    CLAUDE_ID: ("Claude Desktop", "/opt/claude/claude %U"),
}
INITIAL_ENV = {"TZ": "Asia/Tokyo", "LANG": "ja_JP.UTF-8", "LC_ALL": "ja_JP.UTF-8"}


def desktop_text(name, command, *, generated=False):
    return ("[Desktop Entry]\nType=Application\n" + f"Name={name}\nExec={command}\n"
            + "DBusActivatable=true\n" + ("X-Claude-Generated=true\n" if generated else ""))


def document(runtime):
    return json.loads((runtime.root / "local-transaction.json").read_text())["payload"]


def resource_values(engine):
    return {resource.id: deepcopy(resource.read()) for resource in engine.resources}


def original_values(runtime):
    return {entry["id"]: deepcopy(entry["original"]) for entry in document(runtime)["committed"]["resources"]}


def checked_files(runtime):
    return {path.name: path.read_bytes() for path in runtime.root.iterdir() if path.is_file()}


def make_context(tmp_path, *, version=3, session_present=True):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text("# User shell configuration\nexport KEEP=unchanged\n")
    system_apps = tmp_path / "system-applications"
    system_apps.mkdir()
    for id, (name, command) in DESKTOPS.items():
        if id == "firefox.desktop" and version < 3:
            continue
        (system_apps / id).write_text(desktop_text(name, command, generated=id == CLAUDE_ID))
    user_apps = home / ".local/share/applications"
    user_apps.mkdir(parents=True)
    chrome = user_apps / "google-chrome.desktop"
    chrome.write_text(desktop_text("Google Chrome user override", "/usr/bin/google-chrome --user-option %U"))
    chrome.chmod(0o640)
    claude = user_apps / CLAUDE_ID
    claude.write_text(desktop_text("Claude Desktop", "/opt/claude/claude %U", generated=True))
    claude.chmod(0o600)
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"env": {**INITIAL_ENV, "KEEP_SECRET": "preserved"}, "theme": "dark"}) + "\n")
    vscode = home / ".config/Code/User/settings.json"
    vscode.parent.mkdir(parents=True)
    vscode.write_text("{\n// Keep this comment\n\"editor.fontSize\":14,\n"
                      + '"claudeCode.environmentVariables":' + json.dumps([
                          {"name": key, "value": value} for key, value in {**INITIAL_ENV, "KEEP": "preserved"}.items()]) + "\n}\n")
    session = home / ".config/environment.d/60-fucc.conf"
    if session_present:
        session.parent.mkdir(parents=True)
        session.write_text("# Original user environment file\nOTHER=preserved\n")
        session.chmod(0o640)
    env = {"HOME": str(home), "SHELL": "/bin/bash", "PATH": "/usr/bin:/bin",
           "XDG_DATA_DIRS": str(tmp_path / "empty-data")}
    machine = FakeMachine()
    locales = lambda: ["en_US.utf8", "en_GB.utf8", "ja_JP.utf8", "en_SG.utf8"]
    factory = lambda: LocalRuntime(env, [str(system_apps)], machine, locales)
    runtime = factory()
    launcher_path = home / ".config/fucc/launch"
    env_path = home / ".config/fucc/env.sh"
    launcher = ("#!/bin/sh\n# FUCC application environment launcher\n"
                + f"if [ -r '{env_path}' ]; then\n  . '{env_path}'\nfi\nexec \"$@\"\n")
    resources = [
        Resource("bash-profile", str(runtime.root.parent), runtime._read_bash, runtime._write_bash,
                 lambda _: {"region": None}),
        apps.snapshot_resource("session-environment", str(session)),
        apps.snapshot_resource("app-launcher", str(launcher_path)),
        apps.claude_resource(str(settings)), apps.vscode_resource(str(vscode)), machine.resource,
    ]
    zone = "America/New_York" if version == 1 else "America/Los_Angeles"
    target = target_environment("us", zone)
    desired = {
        "bash-profile": {"region": "us", **({"timezone": zone} if version >= 2 else {})},
        "session-environment": {"text": "# FUCC managed environment\n" + "".join(f"{key}={value}\n" for key, value in target.items()), "mode": 0o600},
        "app-launcher": {"text": launcher, "mode": 0o755},
        "claude-settings": {key: {"present": True, "value": value} for key, value in target.items()},
        "vscode-settings": {key: [{"present": True, "value": value}] for key, value in target.items()},
        "machine": machine.desired("us", zone) if version >= 2 else machine.desired("us"),
    }
    retired = {"session-environment"}
    for source in system_apps.iterdir():
        destination = user_apps / source.name
        resource_id = apps.desktop_resource_id(source.name)
        resources.append(apps.snapshot_resource(resource_id, str(destination)))
        before = read_snapshot(str(destination))
        text = before["text"] if before["text"] is not None else source.read_text()
        desired[resource_id] = {"text": apps.wrap_desktop_entry(text, str(launcher_path), identity=source.name), "mode": 0o644}
        if source.name != CLAUDE_ID:
            retired.add(resource_id)
    legacy = LocalTransaction(runtime.root, resources, before_recover=runtime._recover_nested, wire_version=version)
    assert legacy.apply("us", desired) == {"kind": "committed"}
    original = original_values(runtime)
    return {"factory": factory, "home": home, "systemApps": system_apps, "machine": machine,
            "legacy": legacy, "legacyDesired": desired, "original": original, "retired": retired,
            "zone": zone, "version": version, "startup": "# User shell configuration\nexport KEEP=unchanged\n"}


@pytest.fixture
def legacy(tmp_path):
    return make_context(tmp_path)


def assert_native_targets(context, runtime, region, zone=None):
    status = runtime.status()
    assert status["health"] == "healthy" and status["transaction"] == "none"
    assert status["configurationModel"] == "native-first"
    assert status["nativeMigrationRequired"] is False
    assert status["retirementPendingResources"] == []
    assert set(status["retiredResources"]) == context["retired"]
    assert status["coverage"]["desktopSessionEnvironmentManaged"] is False
    assert status["configured"] == target_environment(region, zone)
    payload = document(runtime)
    assert valid_document(payload) and payload["version"] == context["version"]
    assert original_values(runtime) == context["original"]
    by_id = {entry["id"]: entry for entry in payload["committed"]["resources"]}
    live = resource_values(runtime.transaction)
    for id in context["retired"]:
        assert by_id[id]["desired"] == by_id[id]["original"] == live[id]
    claude = by_id["desktop." + CLAUDE_ID]
    assert claude["desired"] == live[claude["id"]]
    assert "X-FUCC-Managed=true" in claude["desired"]["text"]
    assert "X-Claude-Generated=true" not in claude["desired"]["text"]
    assert set(live) == set(context["original"])
    assert "KEEP_SECRET" not in json.dumps(status)


def assert_off(context, runtime):
    status = runtime.status()
    assert status["health"] == "healthy" and status["active"] is False
    assert status["nativeMigrationRequired"] is False
    assert status["retirementPendingResources"] == status["retiredResources"] == []
    assert (context["home"] / ".bashrc").read_text() == context["startup"]
    assert resource_values(context["legacy"]) == context["original"]
    assert read_resource_bindings(runtime.root) == []
    assert document(runtime)["version"] == 1


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("session_present", [False, True])
def test_historical_active_bindings_migrate_without_recapturing_baselines(tmp_path, version, session_present):
    context = make_context(tmp_path, version=version, session_present=session_present)
    runtime = context["factory"]()
    before = checked_files(runtime)
    status = runtime.status()
    assert status["health"] == "healthy" and status["configurationModel"] == "legacy"
    assert status["nativeMigrationRequired"] is True
    assert set(status["retirementPendingResources"]) == context["retired"]
    assert status["retiredResources"] == []
    assert status["coverage"]["desktopSessionEnvironmentManaged"] is True
    assert checked_files(runtime) == before
    assert runtime.on("us", timezone=context["zone"])["kind"] == "committed"
    assert_native_targets(context, runtime, "us", context["zone"])
    assert runtime.on("us", timezone=context["zone"])["kind"] == "noop"
    if version == 1:
        # Selecting a nondefault timezone upgrades the existing v1 wire, while
        # the original snapshots and all historical identities remain intact.
        context["version"] = 2
    assert runtime.on("us", timezone="America/Chicago")["kind"] == "committed"
    assert_native_targets(context, runtime, "us", "America/Chicago")
    assert runtime.on("eu")["kind"] == "committed"
    assert_native_targets(context, runtime, "eu")
    resumed = context["factory"]()
    assert resumed.off() == {"kind": "restored"}
    assert_off(context, resumed)


def test_successful_migration_off_does_not_rewrite_retired_original_files(legacy):
    runtime = legacy["factory"]()
    assert runtime.on("us", timezone=legacy["zone"])["kind"] == "committed"
    for resource in runtime.transaction.resources:
        if resource.id in legacy["retired"]:
            resource.write = lambda _: pytest.fail("A retired exact original must not be rewritten by off")
    assert runtime.off() == {"kind": "restored"}
    assert_off(legacy, runtime)


def test_failed_migration_compensates_to_complete_legacy_configuration(legacy):
    runtime = legacy["factory"]()
    before = resource_values(legacy["legacy"])
    legacy["machine"].fail = True
    assert runtime.on("eu") == {"kind": "compensated"}
    assert resource_values(legacy["legacy"]) == before == legacy["legacyDesired"]
    assert original_values(runtime) == legacy["original"]
    assert runtime.status()["health"] == "healthy" and runtime.status()["nativeMigrationRequired"] is True
    legacy["machine"].fail = False
    assert runtime.on("us", timezone=legacy["zone"])["kind"] == "committed"
    assert_native_targets(legacy, runtime, "us", legacy["zone"])
    assert runtime.off() == {"kind": "restored"}
    assert_off(legacy, runtime)


def intercept_desktop_write(monkeypatch, predicate, exception):
    original = apps.snapshot_resource

    def create(resource_id, path):
        resource = original(resource_id, path)
        write = resource.write
        if resource_id == "desktop.google-chrome.desktop":
            def intercepted(value):
                if predicate(value):
                    if isinstance(exception, KeyboardInterrupt):
                        write(value)
                    raise exception
                write(value)
            resource.write = intercepted
        return resource

    monkeypatch.setattr(apps, "snapshot_resource", create)


def test_interrupted_migration_recovers_back_to_old_targets_before_retry(legacy, monkeypatch):
    before = resource_values(legacy["legacy"])
    original_chrome = legacy["original"]["desktop.google-chrome.desktop"]
    with monkeypatch.context() as patch:
        intercept_desktop_write(patch, lambda value: value == original_chrome, KeyboardInterrupt("fixture power loss"))
        runtime = legacy["factory"]()
        with pytest.raises(KeyboardInterrupt):
            runtime.on("us", timezone="America/Chicago")
    pending = document(runtime)
    assert pending["journal"]["kind"] == "apply" and valid_document(pending)
    assert runtime.status()["health"] == "recovery_required"
    recovering = legacy["factory"]()
    assert recovering.recover() == {"kind": "recovered"}
    assert resource_values(legacy["legacy"]) == before
    assert original_values(recovering) == legacy["original"]
    assert recovering.status()["health"] == "healthy"
    assert recovering.on("us", timezone="America/Chicago")["kind"] == "committed"
    assert_native_targets(legacy, recovering, "us", "America/Chicago")
    assert recovering.off() == {"kind": "restored"}
    assert_off(legacy, recovering)


def test_incomplete_compensation_retains_journal_and_saved_legacy_baselines(legacy, monkeypatch):
    before = resource_values(legacy["legacy"])
    old_chrome = legacy["legacyDesired"]["desktop.google-chrome.desktop"]
    legacy["machine"].fail = True
    with monkeypatch.context() as patch:
        intercept_desktop_write(patch, lambda value: value == old_chrome, OSError("fixture rollback failure"))
        runtime = legacy["factory"]()
        assert runtime.on("eu") == {"kind": "recovery_required"}
    assert document(runtime)["journal"]["kind"] == "apply"
    assert original_values(runtime) == legacy["original"]
    legacy["machine"].fail = False
    recovering = legacy["factory"]()
    assert recovering.recover() == {"kind": "recovered"}
    assert resource_values(legacy["legacy"]) == before
    assert original_values(recovering) == legacy["original"]
    assert recovering.off() == {"kind": "restored"}
    assert_off(legacy, recovering)


@pytest.mark.parametrize("resource_id", ["session-environment", "desktop.google-chrome.desktop", "desktop.firefox.desktop", "desktop.code.desktop"])
def test_external_changes_to_retired_resource_block_entire_migration_without_writes(legacy, resource_id):
    resource = next(resource for resource in legacy["legacy"].resources if resource.id == resource_id)
    path = Path(resource.identity)
    path.write_text(path.read_text() + "# External unknown value\n")
    runtime = legacy["factory"]()
    before = resource_values(legacy["legacy"])
    state_before = checked_files(runtime)
    machine_writes = deepcopy(legacy["machine"].writes)
    assert runtime.on("eu") == {"kind": "recovery_required"}
    assert resource_values(legacy["legacy"]) == before
    assert checked_files(runtime) == state_before
    assert legacy["machine"].writes == machine_writes
    assert document(runtime)["journal"] is None
    assert original_values(runtime) == legacy["original"]


def test_saved_bindings_restore_after_native_application_sources_are_uninstalled(legacy):
    for source in legacy["systemApps"].iterdir():
        if source.name != CLAUDE_ID:
            source.unlink()
    runtime = legacy["factory"]()
    assert runtime.status()["health"] == "healthy"
    assert runtime.on("us", timezone=legacy["zone"])["kind"] == "committed"
    assert_native_targets(legacy, runtime, "us", legacy["zone"])
    resumed = legacy["factory"]()
    assert resumed.status()["health"] == "healthy"
    assert {entry["id"] for entry in read_resource_bindings(resumed.root)} == set(legacy["original"])
    assert resumed.off() == {"kind": "restored"}
    assert_off(legacy, resumed)


def test_pending_old_apply_is_recovered_without_first_migrating_resources(legacy, monkeypatch):
    resource = next(item for item in legacy["legacy"].resources if item.id == "session-environment")
    writer = resource.write

    def interrupt(value):
        writer(value)
        raise KeyboardInterrupt("fixture old version interruption")

    monkeypatch.setattr(resource, "write", interrupt)
    updated = deepcopy(legacy["legacyDesired"])
    target = target_environment("eu")
    updated["bash-profile"] = {"region": "eu"}
    updated["machine"] = legacy["machine"].desired("eu")
    updated["session-environment"] = {"text": "# FUCC managed environment\n" + "".join(f"{key}={value}\n" for key, value in target.items()), "mode": 0o600}
    updated["claude-settings"] = {key: {"present": True, "value": value} for key, value in target.items()}
    updated["vscode-settings"] = {key: [{"present": True, "value": value}] for key, value in target.items()}
    with pytest.raises(KeyboardInterrupt):
        legacy["legacy"].apply("eu", updated)
    runtime = legacy["factory"]()
    before = resource_values(runtime.transaction)
    assert runtime.on("us", timezone=legacy["zone"]) == {"kind": "recovery_required"}
    assert resource_values(runtime.transaction) == before
    assert runtime.recover() == {"kind": "recovered"}
    assert resource_values(runtime.transaction) == legacy["legacyDesired"]
    assert original_values(runtime) == legacy["original"]
    assert runtime.on("us", timezone=legacy["zone"])["kind"] == "committed"
    assert runtime.off() == {"kind": "restored"}
    assert_off(legacy, runtime)


def test_interrupted_off_after_migration_recovers_forward_and_keeps_retired_files(legacy, monkeypatch):
    runtime = legacy["factory"]()
    assert runtime.on("us", timezone=legacy["zone"])["kind"] == "committed"
    resource = next(item for item in runtime.transaction.resources if item.id == "desktop." + CLAUDE_ID)
    writer = resource.write

    def interrupt(value):
        writer(value)
        raise KeyboardInterrupt("fixture power loss during off")

    monkeypatch.setattr(resource, "write", interrupt)
    with pytest.raises(KeyboardInterrupt):
        runtime.off()
    assert document(runtime)["journal"]["kind"] == "restore"
    recovering = legacy["factory"]()
    assert recovering.recover() == {"kind": "recovered"}
    assert_off(legacy, recovering)


def test_damaged_migration_generation_recovers_legacy_plan_without_recapturing_baseline(legacy):
    runtime = legacy["factory"]()
    assert runtime.on("us", timezone=legacy["zone"])["kind"] == "committed"
    (runtime.root / "local-transaction.json").write_text("damaged current generation")
    resumed = legacy["factory"]()
    before = resource_values(resumed.transaction)
    state_before = checked_files(resumed)
    with pytest.raises(FuccError) as error:
        resumed.on("eu")
    assert error.value.code == "RECOVERY_REQUIRED"
    assert resource_values(resumed.transaction) == before and checked_files(resumed) == state_before
    assert resumed.recover() == {"kind": "recovered"}
    assert resource_values(resumed.transaction) == legacy["legacyDesired"]
    assert original_values(resumed) == legacy["original"]
    assert resumed.status()["nativeMigrationRequired"] is True
    assert resumed.on("us", timezone=legacy["zone"])["kind"] == "committed"
    assert_native_targets(legacy, resumed, "us", legacy["zone"])
    assert resumed.off() == {"kind": "restored"}
    assert_off(legacy, resumed)


def test_retired_resource_external_edit_after_migration_is_not_overwritten_by_any_operation(legacy):
    runtime = legacy["factory"]()
    assert runtime.on("us", timezone=legacy["zone"])["kind"] == "committed"
    retired = next(resource for resource in runtime.transaction.resources if resource.id == "session-environment")
    path = Path(retired.identity)
    path.write_text(path.read_text() + "# New independent user configuration\n")
    before = resource_values(runtime.transaction)
    state_before = checked_files(runtime)
    machine_writes = deepcopy(legacy["machine"].writes)
    for operation in (lambda: runtime.on("eu"), runtime.off, runtime.recover):
        assert operation() == {"kind": "recovery_required"}
        assert resource_values(runtime.transaction) == before and checked_files(runtime) == state_before
        assert legacy["machine"].writes == machine_writes
        assert document(runtime)["journal"] is None
