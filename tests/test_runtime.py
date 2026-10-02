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
    assert status["applications"][0]["managed"] is True
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
    runtime.on("us", user_only=True)
    (apps / "google-chrome.desktop").unlink()
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
