"""Claude's generated desktop guard and restoration across the actual adapters."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from fucc import apps
from fucc.jsonc import parse
from fucc.regions import target_environment
from test_dynamic_runtime import managed as managed_fixture
from test_machine import machine as machine_fixture


machine = machine_fixture
managed = managed_fixture
DESKTOP_ID = "com.anthropic.Claude.desktop"
DESKTOP_RESOURCE = "desktop." + DESKTOP_ID
ORIGINAL = ("[Desktop Entry]\nName=Claude Desktop\nType=Application\n"
            "Exec=/opt/claude/claude %U\nIcon=claude\nDBusActivatable=true\n"
            "X-Claude-Generated=true\n")
BASE_ENV = {"TZ": "Asia/Tokyo", "LANG": "ja_JP.UTF-8", "LC_ALL": "ja_JP.UTF-8"}


@pytest.fixture
def claude_local(managed):
    factory, environment, machine = managed
    home = Path(environment["HOME"])
    desktop = home / ".local/share/applications" / DESKTOP_ID
    desktop.parent.mkdir(parents=True)
    desktop.write_text(ORIGINAL)
    desktop.chmod(0o600)
    claude_settings = home / ".claude/settings.json"
    claude_settings.parent.mkdir()
    claude_settings.write_text(json.dumps({"env": {**BASE_ENV, "KEEP": "value"}, "theme": "dark"}) + "\n")
    vscode_settings = home / ".config/Code/User/settings.json"
    vscode_settings.parent.mkdir(parents=True)
    vscode_settings.write_text(json.dumps({"editor.fontSize": 14, "claudeCode.environmentVariables": [
        {"name": key, "value": value} for key, value in {**BASE_ENV, "KEEP": "value"}.items()]}) + "\n")
    return {"factory": factory, "environment": environment, "machine": machine, "home": home,
            "desktop": desktop, "startup": (home / ".bashrc").read_bytes(),
            "claudeSettings": claude_settings, "vscodeSettings": vscode_settings}


def ledger(runtime):
    return json.loads((runtime.root / "local-transaction.json").read_text())["payload"]


def originals(runtime):
    return {entry["id"]: deepcopy(entry["original"]) for entry in ledger(runtime)["committed"]["resources"]}


def legacy_on(context, monkeypatch):
    """Produce the predecessor's actual wrapped desired value and saved baseline."""
    current = apps.wrap_desktop_entry
    def legacy_wrap(text, launcher, **kwargs):
        return current(text, launcher, **kwargs).replace("X-Claude-Generated=false", "X-Claude-Generated=true")
    with monkeypatch.context() as patch:
        patch.setattr(apps, "wrap_desktop_entry", legacy_wrap)
        runtime = context["factory"]()
        assert runtime.on("us", timezone="America/Los_Angeles")["kind"] == "committed"
    assert "X-Claude-Generated=true" in context["desktop"].read_text()
    assert "X-FUCC-Managed=true" in context["desktop"].read_text()
    return runtime


def regenerate_if_client_owns_entry(context):
    """Model Claude's literal generated-marker guard without executing a client."""
    if "X-Claude-Generated=true" not in context["desktop"].read_text():
        return False
    context["desktop"].write_text(ORIGINAL)
    context["desktop"].chmod(0o600)
    return True


def assert_all_targets(runtime, zone):
    status = runtime.status()
    expected = target_environment("us", zone)
    assert status["health"] == "healthy" and status["configured"] == expected
    assert status["bash"]["configured"] == expected
    assert status["machine"]["configuredTimezone"] == status["machine"]["actualTimezone"] == zone
    assert all(resource["aligned"] for resource in status["resources"])
    assert json.loads(Path(runtime.user.paths["claudeSettings"]).read_text())["env"] == {**expected, "KEEP": "value"}
    rows = parse(Path(runtime.user.paths["vscodeSettings"]).read_text())["claudeCode.environmentVariables"]
    assert {entry["name"]: entry["value"] for entry in rows} == {**expected, "KEEP": "value"}
    assert f"TZ={zone}" in Path(runtime.user.paths["sessionFile"]).read_text()


def assert_fully_restored(context, runtime):
    assert runtime.status()["health"] == "healthy" and runtime.status()["active"] is False
    assert context["desktop"].read_bytes() == ORIGINAL.encode()
    assert context["desktop"].stat().st_mode & 0o777 == 0o600
    assert (context["home"] / ".bashrc").read_bytes() == context["startup"]
    assert runtime.bash.status()["mode"] == "daily"
    assert context["machine"]["zone"][0] == "Asia/Tokyo"
    assert runtime.machine.inspect()["active"] is False
    assert not context["machine"]["chrome"].exists()
    assert not (context["home"] / ".config/fucc/env.sh").exists()
    assert not Path(runtime.user.paths["sessionFile"]).exists()
    assert not Path(runtime.user.paths["launcher"]).exists()
    assert not (context["home"] / ".local/share/applications/google-chrome.desktop").exists()
    assert json.loads(context["claudeSettings"].read_text()) == {"env": {**BASE_ENV, "KEEP": "value"}, "theme": "dark"}
    vscode = parse(context["vscodeSettings"].read_text())
    assert vscode["editor.fontSize"] == 14
    assert {item["name"]: item["value"] for item in vscode["claudeCode.environmentVariables"]} == {**BASE_ENV, "KEEP": "value"}


def test_old_client_regenerated_exact_original_is_kept_while_every_other_adapter_restores(claude_local, monkeypatch):
    context = claude_local
    legacy_on(context, monkeypatch)
    assert regenerate_if_client_owns_entry(context)
    runtime = context["factory"]()
    assert runtime.status()["health"] == "degraded"
    resource = next(item for item in runtime.transaction.resources if item.id == DESKTOP_RESOURCE)
    write = resource.write
    writes = []
    def observe(value):
        writes.append(value)
        write(value)
    resource.write = observe
    assert runtime.off()["kind"] == "restored"
    assert writes == [], "The already-restored exact original must not be rewritten"
    assert_fully_restored(context, runtime)


def test_new_wrapper_prevents_client_regeneration_and_restores_exact_original(claude_local):
    context = claude_local
    runtime = context["factory"]()
    assert runtime.on("us", timezone="America/Los_Angeles")["kind"] == "committed"
    wrapped = context["desktop"].read_bytes()
    assert b"X-Claude-Generated=false" in wrapped and b"X-Claude-Generated=true" not in wrapped
    assert context["desktop"].stat().st_mode & 0o777 == 0o644
    assert regenerate_if_client_owns_entry(context) is False
    assert context["desktop"].read_bytes() == wrapped
    assert_all_targets(runtime, "America/Los_Angeles")
    assert runtime.off()["kind"] == "restored"
    assert_fully_restored(context, runtime)


@pytest.mark.parametrize("external", ["original", "unknown"])
def test_on_refuses_external_original_or_unknown_without_changing_other_resources(claude_local, external):
    context = claude_local
    runtime = context["factory"]()
    assert runtime.on("us", timezone="America/Los_Angeles")["kind"] == "committed"
    context["desktop"].write_text(ORIGINAL if external == "original" else ORIGINAL.replace("%U", "--external-edit %U"))
    context["desktop"].chmod(0o600)
    paths = [path for path in context["machine"]["root"].rglob("*") if path.is_file()]
    before = {path: (path.read_bytes(), path.stat().st_mode & 0o777) for path in paths}
    original_timezone = context["machine"]["zone"][0]
    assert runtime.on("us", timezone="America/Chicago")["kind"] == "recovery_required"
    assert {path: (path.read_bytes(), path.stat().st_mode & 0o777) for path in paths} == before
    assert {path for path in context["machine"]["root"].rglob("*") if path.is_file()} == set(paths)
    assert context["machine"]["zone"][0] == original_timezone
    assert runtime.bash.status()["platform"]["configured"]["TZ"] == "America/Los_Angeles"


def test_upgrading_saved_true_wrapper_keeps_original_and_commits_false_wrapper(claude_local, monkeypatch):
    context = claude_local
    old = legacy_on(context, monkeypatch)
    before = originals(old)
    assert before[DESKTOP_RESOURCE] == {"text": ORIGINAL, "mode": 0o600}
    runtime = context["factory"]()
    assert runtime.status()["health"] == "healthy"
    assert runtime.on("us", timezone="America/Los_Angeles")["kind"] == "committed"
    assert originals(runtime) == before
    desired = next(entry["desired"] for entry in ledger(runtime)["committed"]["resources"] if entry["id"] == DESKTOP_RESOURCE)
    assert "X-Claude-Generated=false" in desired["text"] and "X-Claude-Generated=true" not in desired["text"]
    assert context["desktop"].read_text() == desired["text"]
    assert_all_targets(runtime, "America/Los_Angeles")
    assert runtime.on("us", timezone="America/Los_Angeles")["kind"] == "noop"
    assert runtime.off()["kind"] == "restored"
    assert_fully_restored(context, runtime)
