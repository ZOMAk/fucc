"""Upgrade the actual predecessor package across isolated process boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import pytest


RUNNER = r'''
import json, os, sys, traceback
from pathlib import Path
import fucc
from fucc.bash import BashRuntime
import fucc.machine as machine
from fucc.runtime import LocalRuntime
from fucc.transaction import SCHEMAS

base, action = Path(sys.argv[1]), sys.argv[2]
if action == "old-on" and (fucc.__version__ != "0.0.2" or 3 in SCHEMAS):
    raise SystemExit(77)
if action != "old-on" and 3 not in SCHEMAS:
    raise AssertionError("Upgrade process must import the current implementation")
home, zone = base / "home", base / "machine" / "test-timezone"
environment = {"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/bash",
               "XDG_CONFIG_HOME": str(home / ".config"),
               "XDG_STATE_HOME": str(home / ".local/state"),
               "XDG_DATA_HOME": str(home / ".local/share"),
               "XDG_DATA_DIRS": str(base / "empty-data"), "LANG": "ja_JP.UTF-8"}
browsers = [{**browser, "directory": str(base / "machine" / "policies" / browser["id"]),
             "kind": "native" if browser["id"] == "chrome" else "not_installed"}
            for browser in machine.BROWSERS]
if action != "old-on":
    firefox = next(browser for browser in browsers if browser["id"] == "firefox")
    firefox.update(kind="snap", policySupported=True, snapInterfaceConnected=True,
                   distributionPolicyPaths=[], distributionPolicyConflict=False,
                   message="Isolated connected Snap fixture")
commands = base / "commands.json"
def run_command(command, arguments):
    assert command == "/usr/bin/timedatectl"
    history = json.loads(commands.read_text()) if commands.exists() else []
    history.append([command, arguments])
    commands.write_text(json.dumps(history))
    if arguments == ["show", "--property=Timezone", "--value"]:
        return zone.read_text()
    assert len(arguments) == 2 and arguments[0] == "set-timezone"
    zone.write_text(arguments[1])
    return ""
options = {"root": str(base / "machine"), "trusted_uid": os.getuid(),
           "effective_uid": lambda: 0, "sudo_uid": lambda: str(os.getuid()),
           "discover_browsers": lambda: browsers, "run_command": run_command}
def helper(python_path, entrypoint, verb, owner_uid, env, region=None, timezone=None, firefox=None):
    kwargs = {"timezone": timezone}
    if 3 in SCHEMAS:
        kwargs["firefox"] = firefox is True
    try:
        result = machine.run_machine_action(verb, region, owner_uid, options, **kwargs)
    except Exception:
        traceback.print_exc()
        raise
    if result["kind"] in ("compensated", "recovery_required"):
        raise AssertionError("Isolated privileged fixture failed: " + result["kind"])
machine._invoke_helper = helper
BashRuntime._system_timezone = staticmethod(lambda: zone.read_text())
adapter = machine.MachineAdapter(str(Path(machine.__file__).with_name("helper.py")),
                                 sys.executable, environment, options)
runtime = LocalRuntime(environment, [str(base / "applications")], adapter, lambda: ["en_US.utf8"])
result = runtime.off() if action == "new-off" else runtime.on("us", timezone="America/Los_Angeles")
assert result["kind"] == ("restored" if action == "new-off" else "committed"), result
print(json.dumps({"version": fucc.__version__, "implementation": fucc.__file__,
                  "result": result["kind"], "status": runtime.status()}))
'''


def payload(path):
    return json.loads(path.read_text())["payload"]


def original_values(document):
    return {entry["id"]: entry["original"] for entry in document["committed"]["resources"]}


def test_actual_002_local_runtime_upgrades_firefox_and_restores_initial_baselines(tmp_path):
    if os.getuid() == 0:
        pytest.skip("Local runtime migration requires an ordinary user, matching supported usage")
    project = Path(__file__).resolve().parents[1]
    wheel = project / "dist/fucc-0.0.2-py3-none-any.whl"
    if not wheel.is_file():
        pytest.skip("Predecessor Python 0.0.2 wheel unavailable")
    predecessor = tmp_path / "predecessor"
    with ZipFile(wheel) as archive:
        archive.extractall(predecessor)
    home = tmp_path / "home"
    home.mkdir()
    startup = home / ".bashrc"
    startup.write_text("# Original user startup\nexport UNRELATED=keep\n")
    startup_original = startup.read_bytes()
    applications = tmp_path / "applications"
    applications.mkdir()
    (applications / "google-chrome.desktop").write_text("[Desktop Entry]\nName=Google Chrome\nExec=/usr/bin/google-chrome %U\n")
    machine = tmp_path / "machine"
    machine.mkdir(mode=0o755)
    timezone = machine / "test-timezone"
    timezone.write_text("Asia/Tokyo")
    chrome = machine / "policies/chrome/fucc.json"
    chrome.parent.mkdir(parents=True, mode=0o755)
    chrome.parent.parent.chmod(0o755)
    chrome.write_text('{ "HomepageLocation": "https://original.test", "ForcedLanguages": ["fr"] }\n')
    chrome.chmod(0o640)
    chrome_original = chrome.read_bytes()
    firefox = machine / "policies/firefox/policies.json"
    firefox.parent.mkdir(mode=0o755)
    firefox.write_text('{ "policies": { "DisableTelemetry": true, "Preferences": { "intl.accept_languages": "fr", "privacy.resistFingerprinting": true } } }\n')
    firefox.chmod(0o640)
    firefox_original = firefox.read_bytes()

    def invoke(source, action):
        # An empty cwd and explicit PYTHONPATH select the unpacked predecessor
        # rather than the implementation already imported by this pytest process.
        environment = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONPATH": str(source)}
        result = subprocess.run([sys.executable, "-c", RUNNER, str(tmp_path), action], cwd=tmp_path,
                                env=environment, text=True, capture_output=True, timeout=30)
        if result.returncode == 77:
            pytest.skip("Available 0.0.2 wheel already contains the newer transaction format")
        assert result.returncode == 0, result.stderr
        value = json.loads(result.stdout)
        assert Path(value["implementation"]).is_relative_to(source)
        return value

    old = invoke(predecessor, "old-on")
    assert old["version"] == "0.0.2" and old["result"] == "committed"
    assert old["status"]["health"] == "healthy" and timezone.read_text() == "America/Los_Angeles"
    assert old["status"]["configured"]["TZ"] == "America/Los_Angeles"
    assert firefox.read_bytes() == firefox_original and firefox.stat().st_mode & 0o777 == 0o640
    outer_path = home / ".local/state/fucc/local/local-transaction.json"
    machine_path = machine / "machine/local-transaction.json"
    old_outer, old_machine = payload(outer_path), payload(machine_path)
    assert old_outer["version"] == old_machine["version"] == 2
    outer_originals, machine_originals = original_values(old_outer), original_values(old_machine)
    (applications / "firefox_firefox.desktop").write_text("[Desktop Entry]\nName=Firefox\nExec=/snap/bin/firefox %u\nX-SnapInstanceName=firefox\n")

    upgraded = invoke(project / "src", "new-on")
    assert upgraded["result"] == "committed" and upgraded["status"]["health"] == "healthy"
    assert upgraded["status"]["machine"]["firefoxManaged"] is True
    assert payload(outer_path)["version"] == payload(machine_path)["version"] == 3
    assert original_values(payload(outer_path)) == outer_originals
    new_machine_originals = original_values(payload(machine_path))
    assert {key: new_machine_originals[key] for key in machine_originals} == machine_originals
    assert new_machine_originals["policy.firefox"] == {"text": firefox_original.decode(), "mode": 0o640}
    preferences = json.loads(firefox.read_text())["policies"]["Preferences"]
    assert preferences["intl.accept_languages"] == {"Value": "en-US,en", "Status": "locked"}
    assert preferences["privacy.resistFingerprinting"] is True
    assert all(preferences[key] == {"Value": True, "Status": "locked"}
               for key in ("media.peerconnection.ice.default_address_only", "media.peerconnection.ice.no_host",
                           "media.peerconnection.ice.proxy_only_if_behind_proxy"))
    assert firefox.stat().st_mode & 0o777 == 0o644
    snap = next(app for app in upgraded["status"]["applications"] if app["kind"] == "firefox")
    assert snap["managed"] and snap["supported"] and snap["configurationAligned"]
    assert snap["adaptation"] == "system-policy"
    outer = payload(outer_path)
    retired = set(upgraded["status"]["retiredResources"])
    assert {"session-environment", "app-launcher", "desktop.google-chrome.desktop"} <= retired
    assert all(entry["desired"] == entry["original"] for entry in outer["committed"]["resources"] if entry["id"] in retired)
    assert not any(entry["id"].startswith("desktop.") and "firefox" in entry["id"] for entry in outer["committed"]["resources"])
    assert not (home / ".config/environment.d/60-fucc.conf").exists()
    assert not (home / ".config/fucc/launch").exists()
    assert not (home / ".local/share/applications/google-chrome.desktop").exists()
    assert not (home / ".local/share/applications/firefox_firefox.desktop").exists()

    restored = invoke(project / "src", "new-off")
    assert restored["result"] == "restored" and restored["status"]["health"] == "healthy"
    assert restored["status"]["active"] is False and timezone.read_text() == "Asia/Tokyo"
    assert firefox.read_bytes() == firefox_original and firefox.stat().st_mode & 0o777 == 0o640
    assert chrome.read_bytes() == chrome_original and chrome.stat().st_mode & 0o777 == 0o640
    assert startup.read_bytes() == startup_original
    assert not (home / ".config/environment.d/60-fucc.conf").exists()
    assert not (home / ".config/fucc/env.sh").exists()
    assert not (home / ".local/share/applications/google-chrome.desktop").exists()
    assert not (machine / "machine/firefox-profile.json").exists()
    assert all(command == "/usr/bin/timedatectl" for command, _ in json.loads((tmp_path / "commands.json").read_text()))
