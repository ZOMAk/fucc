import json
import os
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import pytest

from fucc.errors import FuccError
from fucc.machine import (BROWSERS, MachineAdapter, _blank, _policy_object, _valid_public,
                          _valid_switch, run_machine_action)


@pytest.fixture
def machine(tmp_path):
    zone = ["Asia/Tokyo"]
    commands = []
    failure = [None]
    browsers = [{**browser, "directory": str(tmp_path / "policies" / browser["id"]),
                 "kind": "native" if browser["id"] in ("chrome", "edge") else "not_installed"} for browser in BROWSERS]

    def run(command, args):
        commands.append((command, args))
        if args[0] == "show":
            return zone[0] + "\n"
        assert command == "/usr/bin/timedatectl" and args[0] == "set-timezone" and len(args) == 2
        if failure[0]:
            failure[0](args[1])
        zone[0] = args[1]
        return ""

    options = {"root": str(tmp_path), "trusted_uid": os.getuid(), "effective_uid": lambda: 0,
               "sudo_uid": lambda: "1000", "discover_browsers": lambda: browsers, "run_command": run}
    return {"root": tmp_path, "options": options, "browsers": browsers, "zone": zone, "failure": failure, "commands": commands,
            "chrome": Path(browsers[0]["directory"]) / "fucc.json"}


def action(machine, verb, region=None, owner=1000, **overrides):
    return run_machine_action(verb, region, owner, {**machine["options"], **overrides})


def test_machine_policies_and_timezone_restore_exact_original(machine):
    chrome = machine["chrome"]
    chrome.parent.mkdir(parents=True, mode=0o755)
    chrome.parent.parent.chmod(0o755)
    original = '{"HomepageLocation":"https://example.test", "ForcedLanguages":["fr"]}\n'
    chrome.write_text(original)
    chrome.chmod(0o640)
    result = action(machine, "on", "us")
    assert result["active"] and result["health"] == "healthy" and result["kind"] == "committed"
    assert result["ownerUid"] == 1000 and result["actualTimezone"] == "America/New_York"
    assert json.loads(chrome.read_text()) == {"HomepageLocation": "https://example.test", "ForcedLanguages": ["en-US", "en"], "WebRtcIPHandling": "disable_non_proxied_udp"}
    assert chrome.stat().st_mode & 0o777 == 0o644
    edge = Path(machine["browsers"][3]["directory"]) / "fucc.json"
    assert json.loads(edge.read_text()) == {"DefinePreferredLanguages": "en-US,en"}
    assert result["browsers"][3]["webRtcPolicy"] == "unsupported"
    action(machine, "on", "eu")
    assert machine["zone"][0] == "Europe/London"
    result = action(machine, "off")
    assert result["kind"] == "restored" and result["ownerUid"] is None
    assert machine["zone"][0] == "Asia/Tokyo"
    assert chrome.read_text() == original and chrome.stat().st_mode & 0o777 == 0o640
    assert all(command == "/usr/bin/timedatectl" for command, _ in machine["commands"])


def _dynamic_action(machine, verb, zone=None, region="us"):
    return run_machine_action(verb, region if verb == "on" else None, 1000, machine["options"], timezone=zone)


def test_dynamic_machine_timezone_uses_saved_target_and_original_baseline(machine):
    assert _dynamic_action(machine, "on", "America/Los_Angeles")["kind"] == "committed"
    public = json.loads((machine["root"] / "status.json").read_text())
    assert public["schema"] == "fucc-machine-public-v2" and public["timezone"] == "America/Los_Angeles"
    assert _valid_public(public)
    state = _dynamic_action(machine, "status")
    assert state["configuredTimezone"] == "America/Los_Angeles" and state["timezoneAligned"] is True
    machine["zone"][0] = "America/Chicago"
    state = _dynamic_action(machine, "status")
    assert state["configuredTimezone"] == "America/Los_Angeles" and state["timezoneAligned"] is False
    assert state["health"] == "degraded"
    machine["zone"][0] = "America/Los_Angeles"
    assert _dynamic_action(machine, "on", "America/Denver")["kind"] == "committed"
    state = _dynamic_action(machine, "off")
    assert state["kind"] == "restored" and state["configuredTimezone"] is None
    assert machine["zone"][0] == "Asia/Tokyo"


@pytest.mark.parametrize("zone", ["Asia/Tokyo", "../../etc/passwd", "America/Los_Angeles\n", "US", True])
def test_invalid_dynamic_machine_zone_is_rejected_before_commands(machine, zone):
    with pytest.raises(FuccError):
        _dynamic_action(machine, "on", zone)
    assert machine["commands"] == [] and not (machine["root"] / "status.json").exists()


def test_dynamic_machine_compensation_keeps_previous_public_target(machine):
    _dynamic_action(machine, "on", "America/Los_Angeles")

    def fail(zone):
        if zone == "America/Chicago":
            raise OSError("new timezone failed")
    machine["failure"][0] = fail
    state = _dynamic_action(machine, "on", "America/Chicago")
    assert state["kind"] == "compensated" and state["configuredTimezone"] == "America/Los_Angeles"
    assert state["actualTimezone"] == "America/Los_Angeles" and state["health"] == "healthy"
    assert json.loads((machine["root"] / "status.json").read_text())["timezone"] == "America/Los_Angeles"


def test_dynamic_machine_pending_recovery_keeps_last_committed_target(machine):
    _dynamic_action(machine, "on", "America/Los_Angeles")

    def fail(zone):
        if zone == "America/Chicago":
            machine["zone"][0] = zone
        raise OSError("interrupted apply and compensation")
    machine["failure"][0] = fail
    state = _dynamic_action(machine, "on", "America/Chicago")
    assert state["kind"] == "recovery_required" and state["configuredTimezone"] == "America/Los_Angeles"
    assert state["actualTimezone"] == "America/Chicago" and state["health"] == "recovery_required"
    machine["failure"][0] = None
    (machine["root"] / "status.json").unlink()
    state = _dynamic_action(machine, "recover")
    assert state["kind"] == "recovered" and state["configuredTimezone"] == "America/Los_Angeles"
    assert state["actualTimezone"] == "America/Los_Angeles" and state["health"] == "healthy"
    assert _dynamic_action(machine, "off")["kind"] == "restored"
    assert machine["zone"][0] == "Asia/Tokyo"


@pytest.mark.parametrize("corrupt", [False, True])
def test_dynamic_public_recovery_rebuilds_committed_target_without_recapture(machine, corrupt):
    _dynamic_action(machine, "on", "America/Los_Angeles")
    public = machine["root"] / "status.json"
    public.write_text("{corrupt") if corrupt else public.unlink()
    state = _dynamic_action(machine, "recover")
    assert state["configuredTimezone"] == "America/Los_Angeles" and state["health"] == "healthy"
    assert json.loads(public.read_text())["timezone"] == "America/Los_Angeles"
    assert _dynamic_action(machine, "off")["kind"] == "restored"
    assert machine["zone"][0] == "Asia/Tokyo"


def test_dynamic_machine_default_normalizes_alias_and_keeps_v1_shape(machine):
    state = _dynamic_action(machine, "on", "US/Eastern")
    assert state["configuredTimezone"] == "America/New_York" and "timezone" not in state
    assert json.loads((machine["root"] / "status.json").read_text())["schema"] == "fucc-machine-public-v1"
    with patch("fucc.machine.os.getuid", return_value=1000):
        adapter = MachineAdapter("/opt/fucc/scripts/fucc", "/usr/bin/python3", test_options=machine["options"])
    assert adapter.desired("us", "US/Pacific")["timezone"] == "America/Los_Angeles"
    assert adapter.desired("us", "US/Eastern") == {"active": True, "region": "us", "ownerUid": 1000}


def test_dynamic_switch_and_public_strictness():
    switch = {"active": True, "region": "us", "ownerUid": 1000, "timezone": "America/Los_Angeles"}
    public = {**_blank(), **switch, "schema": "fucc-machine-public-v2"}
    assert _valid_switch(switch) and _valid_public(public)
    for zone in (None, True, "Asia/Tokyo", "America/New_York", "US/Pacific"):
        assert not _valid_switch({**switch, "timezone": zone})
        assert not _valid_public({**public, "timezone": zone})
    assert not _valid_public({**public, "schema": "fucc-machine-public-v1"})
    assert not _valid_public({**public, "active": False, "region": None})


def test_read_only_status_and_drift_have_no_backup_disclosure(machine):
    assert action(machine, "status")["health"] == "healthy"
    assert not (machine["root"] / "status.json").exists()
    assert not (machine["root"] / "machine").exists()
    action(machine, "on", "us")
    machine["zone"][0] = "Europe/Paris"
    machine["chrome"].write_text('{"ForcedLanguages":["fr"]}\n')
    result = action(machine, "status")
    assert result["health"] == "degraded" and result["timezoneAligned"] is False
    assert result["browsers"][0]["configurationAligned"] is False
    for private in ("original", "snapshot", "Asia/Tokyo"):
        assert private not in json.dumps(result)


def test_competing_policy_prevents_all_resource_changes(machine):
    chrome = machine["chrome"]
    chrome.parent.mkdir(parents=True, mode=0o755)
    chrome.parent.parent.chmod(0o755)
    (chrome.parent / "administrator.json").write_text('{"WebRtcIPHandling":"default"}\n')
    (chrome.parent / "administrator.json").chmod(0o644)
    with pytest.raises(FuccError) as exc:
        action(machine, "on", "us")
    assert exc.value.code == "CONFIG_CONFLICT"
    assert machine["zone"][0] == "Asia/Tokyo" and not chrome.exists()


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_browser_policy_rejects_before_machine_changes(machine, value):
    chrome = machine["chrome"]
    chrome.parent.mkdir(parents=True, mode=0o755)
    chrome.parent.parent.chmod(0o755)
    original = '{"HomepageLocation":' + value + '}\n'
    chrome.write_text(original)
    chrome.chmod(0o644)
    with pytest.raises(FuccError) as exc:
        action(machine, "on", "us")
    assert exc.value.code == "CONFIG_CONFLICT"
    assert machine["zone"][0] == "Asia/Tokyo" and chrome.read_text() == original
    assert not (machine["root"] / "status.json").exists()


def test_duplicate_managed_policy_keys_are_rejected():
    with pytest.raises(FuccError) as exc:
        _policy_object('{"ForcedLanguages":["fr"],"ForcedLanguages":["en"]}', "Chrome")
    assert exc.value.code == "CONFIG_CONFLICT"


@pytest.mark.parametrize("invalid", [None, True, 0, 1.5, "unexpected", [], {}, ["chrome"]])
def test_public_and_switch_type_mismatches_return_false(invalid):
    active = {"active": True, "region": "us", "ownerUid": 1000}
    public = {**_blank(), **active, "managedBrowsers": ["chrome"]}
    for validate, value in [(_valid_switch, active), (_valid_public, public)]:
        assert validate(value)
        if not isinstance(invalid, dict) or invalid != value:
            assert validate(invalid) is False
        for key in value:
            if type(invalid) is not type(value[key]):
                assert validate({**value, key: invalid}) is False


def test_uid_policy_prevents_non_root_spoofing_and_other_owner(machine):
    for overrides, owner, code in [({"effective_uid": lambda: 1000}, 1000, "ADMINISTRATOR_REQUIRED"), ({}, 999, "OWNER_MISMATCH")]:
        with pytest.raises(FuccError) as exc:
            action(machine, "on", "us", owner, **overrides)
        assert exc.value.code == code
    action(machine, "on", "us")
    with pytest.raises(FuccError) as exc:
        action(machine, "off", owner=1001, sudo_uid=lambda: "1001")
    assert exc.value.code == "OWNER_MISMATCH"
    assert machine["zone"][0] == "America/New_York"


def test_failed_timezone_compensates_policies_and_owner(machine):
    def fail(zone):
        if zone == "America/New_York":
            raise OSError("simulated failure")
    machine["failure"][0] = fail
    result = action(machine, "on", "us")
    assert result["kind"] == "compensated" and result["recoveryRequired"] is False
    assert not machine["chrome"].exists() and machine["zone"][0] == "Asia/Tokyo"


def test_failed_compensation_claim_survives_lost_public_owner(machine):
    def fail(zone):
        if zone == "America/New_York":
            machine["zone"][0] = zone
            raise OSError("failure after write")
        if zone == "Asia/Tokyo":
            raise OSError("failure restoring")
    machine["failure"][0] = fail
    result = action(machine, "on", "us")
    assert result["kind"] == "recovery_required" and result["ownerUid"] == 1000
    assert not (machine["root"] / "machine" / "owner.json").exists()
    (machine["root"] / "status.json").unlink()
    with pytest.raises(FuccError) as exc:
        action(machine, "recover", owner=1001, sudo_uid=lambda: "1001")
    assert exc.value.code == "OWNER_MISMATCH"
    machine["failure"][0] = None
    result = action(machine, "recover")
    assert result["kind"] == "recovered" and result["ownerUid"] is None and result["recoveryRequired"] is False
    assert machine["zone"][0] == "Asia/Tokyo"


@pytest.mark.parametrize("corrupt", [False, True])
def test_recovery_reconstructs_public_mirror_without_recapturing_original(machine, corrupt):
    action(machine, "on", "us")
    public = machine["root"] / "status.json"
    public.write_text("{corrupt") if corrupt else public.unlink()
    result = action(machine, "recover")
    assert result["active"] and result["region"] == "us" and result["recoveryRequired"] is False
    result = action(machine, "off")
    assert result["active"] is False and machine["zone"][0] == "Asia/Tokyo"


def test_isolated_browser_installs_never_create_machine_policy(machine):
    machine["browsers"][0]["kind"] = "flatpak"
    machine["browsers"][1]["kind"] = "snap"
    result = action(machine, "on", "jp")
    assert result["browsers"][0]["effectivePolicy"] == "unsupported"
    assert result["browsers"][1]["webRtcPolicy"] == "unsupported"
    assert not machine["chrome"].exists()


def test_untrusted_public_mirror_or_policy_ancestor_is_rejected(machine):
    forged = machine["root"] / "forged.json"
    forged.write_text("{}")
    (machine["root"] / "status.json").symlink_to(forged)
    with pytest.raises(FuccError) as exc:
        action(machine, "status")
    assert exc.value.code == "CONFIG_CONFLICT"
    (machine["root"] / "status.json").unlink()
    machine["root"].chmod(0o777)
    with pytest.raises(FuccError) as exc:
        action(machine, "status")
    assert exc.value.code == "MACHINE_PATH_UNTRUSTED"
    machine["root"].chmod(0o700)
    (machine["root"] / "outside").mkdir()
    (machine["root"] / "policies").symlink_to(machine["root"] / "outside")
    with pytest.raises(FuccError) as exc:
        action(machine, "on", "us")
    assert exc.value.code == "CONFIG_CONFLICT"


def test_adapter_passes_only_isolated_absolute_helper_and_validates_result(machine):
    calls = []

    def invoke(command, **kwargs):
        calls.append((command, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": json.dumps({"schemaVersion": 1, "ok": True, "machine": {"scope": "machine", "active": True, "region": "us", "ownerUid": 1000, "recoveryRequired": False}})})()

    with patch("fucc.machine.os.getuid", return_value=1000), patch("fucc.machine.subprocess.run", side_effect=invoke):
        adapter = MachineAdapter("/opt/fucc/scripts/fucc", "/opt/conda/envs/fucc/bin/python", {"TERM": "xterm", "PYTHONPATH": "/dangerous", "PYTHONINSPECT": "1"}, machine["options"])
        adapter.resource.write(adapter.desired("us"))
    assert calls[0][0] == ["/usr/bin/sudo", "--", "/opt/conda/envs/fucc/bin/python", "-I", "/opt/fucc/scripts/fucc", "__machine", "on", "--owner", "1000", "--region", "us"]
    assert calls[0][1]["env"] == {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "TERM": "xterm"}
    assert "stdin" not in calls[0][1] and "stderr" not in calls[0][1]


def test_dynamic_adapter_uses_validated_timezone_in_helper_argument(machine):
    calls = []
    fake = {"scope": "machine", "active": True, "region": "us", "ownerUid": 1000, "recoveryRequired": False,
            "configuredTimezone": "America/Los_Angeles", "timezone": "America/Los_Angeles"}

    def invoke(command, **kwargs):
        calls.append((command, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": json.dumps(fake)})()

    with patch("fucc.machine.os.getuid", return_value=1000), patch("fucc.machine.subprocess.run", side_effect=invoke):
        adapter = MachineAdapter("/opt/fucc/scripts/fucc", "/opt/conda/envs/fucc/bin/python", {}, machine["options"])
        adapter.resource.write(adapter.desired("us", "US/Pacific"))
    assert calls[0][0][-4:] == ["--region", "us", "--timezone", "America/Los_Angeles"]


@pytest.mark.parametrize("reported,override", [("America/Chicago", "America/Chicago"),
                                              ("America/Los_Angeles", "America/Chicago"),
                                              ("Asia/Tokyo", "Asia/Tokyo"), (None, None)])
def test_dynamic_helper_rejects_wrong_or_ambiguous_target_timezone(machine, reported, override):
    with patch("fucc.machine.os.getuid", return_value=1000):
        adapter = MachineAdapter("/opt/fucc/scripts/fucc", "/usr/bin/python3", test_options=machine["options"])
    payload = {"scope": "machine", "active": True, "region": "us", "ownerUid": 1000, "recoveryRequired": False,
               "configuredTimezone": reported}
    if override is not None:
        payload["timezone"] = override
    fake = type("Result", (), {"returncode": 0, "stdout": json.dumps(payload)})()
    with patch("fucc.machine.subprocess.run", return_value=fake), pytest.raises(FuccError) as exc:
        adapter.resource.write(adapter.desired("us", "America/Los_Angeles"))
    assert exc.value.code == "MACHINE_HELPER_FAILED"


def test_adapter_rejects_pending_and_drift_without_sudo(machine):
    action(machine, "on", "us")
    with patch("fucc.machine.os.getuid", return_value=1000):
        adapter = MachineAdapter("/opt/fucc/scripts/fucc", "/usr/bin/python3", test_options=machine["options"])
    assert adapter.resource.read() == {"active": True, "region": "us", "ownerUid": 1000}
    machine["zone"][0] = "Europe/Paris"
    with pytest.raises(FuccError) as exc:
        adapter.resource.read()
    assert exc.value.code == "MACHINE_RECOVERY_REQUIRED"


def test_helper_rejects_wrong_region_result(machine):
    with patch("fucc.machine.os.getuid", return_value=1000):
        adapter = MachineAdapter("/opt/fucc/scripts/fucc", "/usr/bin/python3", test_options=machine["options"])
    fake = type("Result", (), {"returncode": 0, "stdout": json.dumps({"scope": "machine", "active": True, "region": "eu", "ownerUid": 1000, "recoveryRequired": False})})()
    with patch("fucc.machine.subprocess.run", return_value=fake), pytest.raises(FuccError) as exc:
        adapter.resource.write(adapter.desired("us"))
    assert exc.value.code == "MACHINE_HELPER_FAILED"


@pytest.fixture(scope="module")
def legacy_machine(tmp_path_factory):
    legacy = Path(__file__).resolve().parents[2] / "fucc"
    builds = sorted(legacy.glob("node_modules/.pnpm/esbuild@*/node_modules/esbuild/bin/esbuild"))
    node = shutil.which("node")
    source = legacy / "src/local/machine.ts"
    if not node or not builds or not source.exists():
        pytest.skip("Legacy TypeScript source and build tool are not available")
    root = tmp_path_factory.mktemp("legacy-machine-module")
    subprocess.run([str(builds[0]), str(source), "--bundle", "--platform=node", "--format=esm", f"--outfile={root / 'module.mjs'}"], check=True, capture_output=True)
    runner = root / "runner.mjs"
    runner.write_text("""
import {runMachineAction} from './module.mjs';
import {readFile,writeFile} from 'node:fs/promises';
import {join} from 'node:path';
const [root,action,region]=process.argv.slice(2);
const browsers=[['chrome','Chrome'],['chromium','Chromium'],['chromium-browser','Chromium (distro)'],['edge','Edge']].map(([id,label])=>({id,label,kind:id==='chrome'||id==='edge'?'native':'not_installed',directory:join(root,'policies',id)}));
const options={root,trustedUid:process.getuid(),effectiveUid:()=>0,sudoUid:()=> '1000',discoverBrowsers:async()=>browsers,runCommand:async(_command,args)=>{
 if(args[0]==='show') return await readFile(join(root,'test-zone'),'utf8');
 await writeFile(join(root,'test-zone'),args[1]);
 if(action==='fail'&&(args[1]==='America/New_York'||args[1]==='Asia/Tokyo')) throw new Error('simulated interrupted timezone');
 return '';
}};
console.log(JSON.stringify(await runMachineAction({action:action==='fail'?'on':action,region,ownerUid:1000,options})));
""")

    def invoke(directory, action, region=None):
        result = subprocess.run([node, str(runner), str(directory), action] + ([region] if region else []),
                                check=True, text=True, capture_output=True)
        return json.loads(result.stdout)
    return invoke


def _use_file_timezone(machine):
    path = machine["root"] / "test-zone"
    path.write_text("Asia/Tokyo")

    def run(_command, args):
        if args[0] == "show":
            return path.read_text()
        path.write_text(args[1])
        return ""
    machine["options"]["run_command"] = run
    return path


def test_typescript_machine_on_python_switch_and_off(legacy_machine, machine):
    zone = _use_file_timezone(machine)
    root = machine["root"]
    chrome = machine["chrome"]
    chrome.parent.mkdir(parents=True, mode=0o755)
    chrome.parent.parent.chmod(0o755)
    original = '{"HomepageLocation":"https://example.test"}\n'
    chrome.write_text(original)
    chrome.chmod(0o640)
    assert legacy_machine(root, "on", "us")["kind"] == "committed"
    assert action(machine, "on", "eu")["kind"] == "committed"
    assert legacy_machine(root, "status")["region"] == "eu"
    assert action(machine, "off")["kind"] == "restored"
    assert chrome.read_text() == original and chrome.stat().st_mode & 0o777 == 0o640
    assert zone.read_text() == "Asia/Tokyo"
    assert legacy_machine(root, "status")["active"] is False


def test_typescript_failed_machine_claim_recovers_in_python(legacy_machine, machine):
    zone = _use_file_timezone(machine)
    root = machine["root"]
    assert legacy_machine(root, "fail", "us")["kind"] == "recovery_required"
    (root / "status.json").unlink()
    with pytest.raises(FuccError) as exc:
        action(machine, "recover", owner=1001, sudo_uid=lambda: "1001")
    assert exc.value.code == "OWNER_MISMATCH"
    result = action(machine, "recover")
    assert result["kind"] == "recovered" and result["recoveryRequired"] is False
    assert zone.read_text() == "Asia/Tokyo"
    assert legacy_machine(root, "status")["active"] is False
