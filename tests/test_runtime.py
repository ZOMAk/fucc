from copy import deepcopy
import json
from pathlib import Path

import pytest

from fucc.errors import FuccError
from fucc.regions import target_environment
from fucc.runtime import LocalRuntime, assert_local_inactive
from fucc.transaction import Resource


class FakeMachine:
    def __init__(self):
        self.current = {"active": False, "region": None, "ownerUid": None}
        self.writes = []
        self.health = "healthy"
        self.pending = False
        self.fail = False
        self.resource = Resource("machine", "fucc-machine:v1:/var/lib/fucc", lambda: deepcopy(self.current), self.write)

    def write(self, value):
        if self.fail:
            raise OSError("fixture privileged failure")
        self.writes.append(deepcopy(value))
        self.current = deepcopy(value)

    def desired(self, region, timezone=None):
        return {"active": True, "region": region, "ownerUid": 1000, **({"timezone": timezone} if timezone else {})}

    def inspect(self):
        return {**self.current, "scope": "machine", "health": self.health, "recoveryRequired": self.pending,
                "transaction": "apply" if self.pending else "none", "revision": len(self.writes),
                "actualTimezone": target_environment(self.current["region"], self.current.get("timezone"))["TZ"] if self.current["active"] else "Asia/Tokyo",
                "timezoneAligned": True if self.current["active"] else None, "requiresAdministrator": True, "browsers": []}

    def recover(self):
        self.pending = False


@pytest.fixture
def local(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text("# daily settings\nexport OTHER=value\n")
    apps = tmp_path / "system-apps"
    apps.mkdir()
    (apps / "google-chrome.desktop").write_text("[Desktop Entry]\nName=Google Chrome\nExec=/usr/bin/google-chrome %U\nDBusActivatable=true\n[Desktop Action NewWindow]\nExec=/usr/bin/google-chrome --new-window\n")
    env = {"HOME": str(home), "SHELL": "/bin/bash", "PATH": "/usr/bin:/bin", "XDG_DATA_DIRS": str(tmp_path / "data")}
    machine = FakeMachine()
    locales = lambda: ["en_US.utf8", "en_GB.utf8", "ja_JP.utf8", "en_SG.utf8"]
    runtime = LocalRuntime(env, [str(apps)], machine, locales)
    return runtime, env, machine, apps, locales


def test_unified_enable_switch_noop_and_restore_original(local):
    runtime, env, machine, apps, locales = local
    original_bash = (Path(env["HOME"]) / ".bashrc").read_text()
    settings = Path(runtime.user.paths["claudeSettings"])
    settings.parent.mkdir()
    settings.write_text('{"env":{"TZ":"Europe/Paris","TOKEN":"keep-secret"},"theme":"dark"}\n')
    vscode = Path(runtime.user.paths["vscodeSettings"])
    vscode.parent.mkdir(parents=True)
    vscode.write_text('{\n// user comment\n"editor.fontSize":14,\n"claudeCode.environmentVariables":[{"name":"TZ","value":null},{"name":"TZ","value":"old"}]\n}\n')
    assert runtime.on("us")["kind"] == "committed"
    status = runtime.status()
    assert status["health"] == "healthy" and status["configured"] == target_environment("us")
    assert status["machine"]["active"] and status["coverage"]["applicationRuntimeVerified"] is False
    assert status["applications"][0]["adaptation"] == "system-policy"
    assert status["applications"][0]["managed"] is False  # The fixture has no machine browser policies.
    assert status["configurationModel"] == "native-first" and not status["nativeMigrationRequired"]
    assert "keep-secret" not in json.dumps(status)
    assert runtime.on("us")["kind"] == "noop"
    assert runtime.on("eu")["kind"] == "committed"
    # Fresh runtime resumes the resource set and immutable original snapshots.
    resumed = LocalRuntime(env, [str(apps)], machine, locales)
    assert resumed.off()["kind"] == "restored"
    assert (Path(env["HOME"]) / ".bashrc").read_text() == original_bash
    assert json.loads(settings.read_text())["env"] == {"TZ": "Europe/Paris", "TOKEN": "keep-secret"}
    assert "// user comment" in vscode.read_text()
    assert json.loads(vscode.read_text().replace("// user comment", ""))["claudeCode.environmentVariables"] == [{"name": "TZ", "value": None}, {"name": "TZ", "value": "old"}]
    assert not Path(runtime.user.paths["sessionFile"]).exists()
    assert not Path(runtime.user.paths["launcher"]).exists()
    assert not (Path(env["HOME"]) / ".local/share/applications/google-chrome.desktop").exists()
    assert machine.current == {"active": False, "region": None, "ownerUid": None}
    assert resumed.off()["kind"] == "noop"
    assert_local_inactive(env)


def test_user_only_and_standalone_profile_guard(local):
    runtime, env, machine, _, _ = local
    assert runtime.on("us", user_only=True)["kind"] == "committed"
    assert machine.writes == []
    with pytest.raises(FuccError) as exc:
        assert_local_inactive(env)
    assert exc.value.exit_code == 10
    runtime.off()
    assert machine.writes == []
    assert_local_inactive(env)


def test_application_status_uses_exact_browser_and_native_vscode_resources(local):
    _, env, machine, apps, locales = local
    (apps / "chromium.desktop").write_text('[Desktop Entry]\nName=Chromium\nExec=/usr/bin/chromium %U\n')
    (apps / "chromium-browser.desktop").write_text('[Desktop Entry]\nName=Chromium Browser\nExec=/usr/bin/chromium-browser %U\n')
    (apps / "flatpak-chromium.desktop").write_text('[Desktop Entry]\nName=Chromium\nExec=/usr/bin/flatpak run org.chromium.Chromium %U\n')
    (apps / "hidden-chromium.desktop").write_text('[Desktop Entry]\nName=Chromium\nHidden=true\nExec=/usr/bin/chromium %U\n')
    (apps / "code.desktop").write_text('[Desktop Entry]\nName=Visual Studio Code\nExec=/usr/bin/code %F\n')
    inspect = machine.inspect
    machine.inspect = lambda: {**inspect(), "browsers": [
        {"id": "chrome", "managed": False, "configurationAligned": None},
        {"id": "chromium", "managed": True, "configurationAligned": True},
        {"id": "chromium-browser", "managed": True, "configurationAligned": False},
    ]}
    runtime = LocalRuntime(env, [str(apps)], machine, locales)
    assert runtime.on("us")["kind"] == "committed"
    status = runtime.status()
    observed = {app["id"]: app for app in status["applications"]}
    assert not observed["google-chrome.desktop"]["managed"]
    assert observed["chromium.desktop"]["managed"] and observed["chromium.desktop"]["configurationAligned"]
    assert observed["chromium-browser.desktop"]["managed"] and not observed["chromium-browser.desktop"]["configurationAligned"]
    assert not observed["flatpak-chromium.desktop"]["managed"]
    assert observed["flatpak-chromium.desktop"]["configurationAligned"] is None
    assert not observed["hidden-chromium.desktop"]["managed"]
    assert observed["code.desktop"]["managed"] and observed["code.desktop"]["configurationAligned"]
    assert all(not resource["id"].startswith("desktop.") for resource in status["resources"])
    assert not Path(runtime.user.paths["sessionFile"]).exists()
    assert runtime.off()["kind"] == "restored"


def test_external_edits_block_restore_without_overwrite(local):
    runtime, _, machine, _, _ = local
    runtime.on("us", user_only=True)
    settings = Path(runtime.user.paths["claudeSettings"])
    value = json.loads(settings.read_text())
    value["env"]["TZ"] = "Europe/Paris"
    settings.write_text(json.dumps(value))
    assert runtime.status()["health"] == "degraded"
    assert runtime.off()["kind"] == "recovery_required"
    assert runtime.recover()["kind"] == "recovery_required"
    assert json.loads(settings.read_text())["env"]["TZ"] == "Europe/Paris"
    assert machine.writes == []


def test_full_failure_compensates_user_resources(local):
    runtime, env, machine, _, _ = local
    machine.fail = True
    before = (Path(env["HOME"]) / ".bashrc").read_text()
    assert runtime.on("us")["kind"] == "compensated"
    assert runtime.status()["active"] is False
    assert (Path(env["HOME"]) / ".bashrc").read_text() == before
    assert not Path(runtime.user.paths["sessionFile"]).exists()
    assert runtime.bash.status()["mode"] == "daily"


def test_saved_desktop_binding_survives_uninstall(local):
    runtime, env, machine, apps, locales = local
    (apps / "claude.desktop").write_text('[Desktop Entry]\nName=Claude Desktop\nExec=/opt/claude/claude %U\n')
    runtime = LocalRuntime(env, [str(apps)], machine, locales)
    runtime.on("us", user_only=True)
    (apps / "claude.desktop").unlink()
    resumed = LocalRuntime(env, [str(apps)], machine, locales)
    assert resumed.status()["health"] == "healthy"
    assert resumed.off()["kind"] == "restored"


def test_nested_machine_recovery_and_locale_prerequisite(local):
    runtime, env, machine, apps, _ = local
    machine.pending = True
    assert runtime.status()["health"] == "recovery_required"
    assert runtime.recover()["kind"] == "noop"
    assert not machine.pending
    unavailable = LocalRuntime(env, [str(apps)], machine, lambda: ["C.utf8"])
    with pytest.raises(FuccError) as exc:
        unavailable.on("us")
    assert exc.value.exit_code == 10 and not machine.writes


def test_root_session_rejected_before_state_creation(tmp_path, monkeypatch):
    monkeypatch.setattr("fucc.runtime.os.getuid", lambda: 0)
    with pytest.raises(FuccError) as exc:
        LocalRuntime({"HOME": str(tmp_path)})
    assert exc.value.exit_code == 10
    assert list(tmp_path.iterdir()) == []


def test_native_firefox_discovery_does_not_add_user_desktop_overrides(local):
    runtime, env, machine, apps, locales = local
    runtime.on("us", user_only=True, timezone="America/Los_Angeles")
    old = json.loads((runtime.root / "local-transaction.json").read_text())["payload"]
    assert old["version"] == 2
    original = {entry["id"]: entry["original"] for entry in old["committed"]["resources"]}
    (apps / "firefox.desktop").write_text('[Desktop Entry]\nName=Firefox\nExec=/usr/lib/firefox/firefox %u\n')
    resumed = LocalRuntime(env, [str(apps)], machine, locales)
    assert resumed.status()["health"] == "healthy"
    assert resumed.on("us", user_only=True, timezone="America/Los_Angeles")["kind"] == "noop"
    ledger = json.loads((resumed.root / "local-transaction.json").read_text())["payload"]
    assert ledger["version"] == 2
    assert {entry["id"]: entry["original"] for entry in ledger["committed"]["resources"] if entry["id"] in original} == original
    firefox = next(app for app in resumed.status()["applications"] if app["kind"] == "firefox")
    assert firefox["supported"] and firefox["adaptation"] == "system-policy"
    assert not firefox["managed"] and firefox["configurationAligned"] is None
    assert resumed.on("us", user_only=True, timezone="America/Los_Angeles")["kind"] == "noop"
    override = Path(env["HOME"]) / ".local/share/applications/firefox.desktop"
    assert not override.exists() and not machine.writes
    assert resumed.off()["kind"] == "restored" and not override.exists()
    assert (Path(env["HOME"]) / ".bashrc").read_text() == "# daily settings\nexport OTHER=value\n"


def test_machine_failure_with_discovered_firefox_preserves_native_binding_set(local):
    runtime, env, machine, apps, locales = local
    runtime.on("us", user_only=True)
    previous = runtime.status()["resources"]
    (apps / "firefox.desktop").write_text('[Desktop Entry]\nName=Firefox\nExec=/usr/lib/firefox/firefox %u\n')
    resumed = LocalRuntime(env, [str(apps)], machine, locales)
    machine.fail = True
    assert resumed.on("us")["kind"] == "compensated"
    assert resumed.status()["health"] == "healthy"
    assert resumed.status()["resources"] == previous
    assert not (Path(env["HOME"]) / ".local/share/applications/firefox.desktop").exists()
    assert resumed.off()["kind"] == "restored"


def test_recover_interrupted_native_settings_switch_restores_previous_bindings(local, monkeypatch):
    from fucc import apps as app_module

    runtime, env, machine, apps, locales = local
    runtime.on("us", user_only=True)
    previous = runtime.status()["resources"]
    (apps / "firefox.desktop").write_text('[Desktop Entry]\nName=Firefox\nExec=/usr/lib/firefox/firefox %u\n')
    create_resource = app_module.vscode_resource
    interrupted = [False]

    def vscode_resource(path):
        value = create_resource(path)
        original_write = value.write

        def write(desired):
            original_write(desired)
            if not interrupted[0]:
                interrupted[0] = True
                raise KeyboardInterrupt("fixture power loss after native VS Code settings")

        value.write = write
        return value

    monkeypatch.setattr(app_module, "vscode_resource", vscode_resource)
    resumed = LocalRuntime(env, [str(apps)], machine, locales)
    with pytest.raises(KeyboardInterrupt):
        resumed.on("eu", user_only=True)
    recovering = LocalRuntime(env, [str(apps)], machine, locales)
    assert recovering.recover()["kind"] == "recovered"
    assert recovering.status()["health"] == "healthy" and recovering.status()["resources"] == previous
    assert recovering.off()["kind"] == "restored"
