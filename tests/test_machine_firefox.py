"""Firefox machine resources enroll reversibly into an existing FUCC profile."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from fucc.errors import FuccError
from fucc.firefox import MANAGED_PREFERENCES, policy_aligned
from fucc.machine import MachineAdapter, _valid_public, _valid_switch, run_machine_action
from fucc.transaction import read_resource_bindings
from test_machine import machine as machine


def enable_firefox(machine, kind="native", supported=True):
    browser = next(item for item in machine["browsers"] if item["id"] == "firefox")
    browser.update(kind=kind, policySupported=supported,
                   snapInterfaceConnected=supported if kind == "snap" else None,
                   message="Firefox: verify in about:policies")
    return Path(browser["directory"]) / "policies.json"


def action(machine, verb, region=None, *, firefox=False, timezone=None):
    return run_machine_action(verb, region, 1000, machine["options"], timezone=timezone, firefox=firefox)


def state(machine):
    return json.loads((machine["root"] / "status.json").read_text())


def private(machine):
    return json.loads((machine["root"] / "machine" / "local-transaction.json").read_text())["payload"]


def original_policy(path):
    path.parent.mkdir(mode=0o755, parents=True)
    path.parent.parent.chmod(0o755)
    text = '{ "policies": {"DisableAppUpdate": true,"Preferences": {"browser.startup.page": 3,"intl.accept_languages": {"Value":"fr","Status":"user"}}}, "annotation":"keep" }\n'
    path.write_text(text)
    path.chmod(0o640)
    return text


def test_firefox_native_policy_preserves_other_preferences_and_exact_original(machine):
    path = enable_firefox(machine)
    original = original_policy(path)
    result = action(machine, "on", "us", firefox=True, timezone="America/Los_Angeles")
    assert result["kind"] == "committed" and result["firefoxManaged"] is True
    assert result["configuredTimezone"] == "America/Los_Angeles"
    assert state(machine)["schema"] == "fucc-machine-public-v3"
    assert _valid_public(state(machine))
    assert private(machine)["version"] == 3
    actual = json.loads(path.read_text())
    assert actual["annotation"] == "keep" and actual["policies"]["DisableAppUpdate"] is True
    assert actual["policies"]["Preferences"]["browser.startup.page"] == 3
    assert all(actual["policies"]["Preferences"][key]["Status"] == "locked" for key in MANAGED_PREFERENCES)
    assert policy_aligned(path.read_text(), "us")
    firefox = next(browser for browser in result["browsers"] if browser["id"] == "firefox")
    assert firefox["policyPath"] == str(path) and firefox["verificationPage"] == "about:policies"
    assert firefox["configurationAligned"] is True and firefox["effectivePolicy"] == "verify_in_browser"
    assert not (path.parent / "fucc.json").exists()
    action(machine, "on", "eu", firefox=True)
    assert policy_aligned(path.read_text(), "eu")
    result = action(machine, "off")
    assert result["kind"] == "restored" and result["active"] is False
    assert path.read_text() == original and path.stat().st_mode & 0o777 == 0o640
    assert not (machine["root"] / "machine" / "firefox-profile.json").exists()
    assert machine["zone"][0] == "Asia/Tokyo"


@pytest.mark.parametrize("kind,supported", [("snap", True), ("snap", False), ("flatpak", False), ("native", False)])
def test_firefox_policy_management_requires_supported_installation(machine, kind, supported):
    path = enable_firefox(machine, kind, supported)
    if supported:
        result = action(machine, "on", "us", firefox=True)
        firefox = next(item for item in result["browsers"] if item["id"] == "firefox")
        assert firefox["installation"] == "snap" and firefox["snapInterfaceConnected"] is True
        assert firefox["effectivePolicy"] == "verify_in_browser" and path.exists()
        assert action(machine, "off")["kind"] == "restored" and not path.exists()
    else:
        with pytest.raises(FuccError) as exc:
            action(machine, "on", "us", firefox=True)
        assert exc.value.code == "UNSUPPORTED_FEATURE"
        assert not path.exists() and machine["zone"][0] == "Asia/Tokyo"
        result = action(machine, "status")
        firefox = next(item for item in result["browsers"] if item["id"] == "firefox")
        assert firefox["effectivePolicy"] == "unsupported" and firefox["managed"] is False


def test_same_region_on_enrolls_firefox_and_old_descriptor_disables_only_firefox(machine):
    action(machine, "on", "us")
    saved_timezone = machine["zone"][0]
    core_originals = {item["id"]: item["original"] for item in private(machine)["committed"]["resources"]}
    path = enable_firefox(machine)
    original = original_policy(path)
    result = action(machine, "on", "us", firefox=True)
    assert result["kind"] == "committed" and result["firefoxManaged"] is True
    assert {item["id"]: item["original"] for item in private(machine)["committed"]["resources"] if item["id"] in core_originals} == core_originals
    result = action(machine, "on", "us", firefox=False)
    assert result["kind"] == "committed" and result["active"] is True and result["firefoxManaged"] is False
    assert machine["zone"][0] == saved_timezone
    assert path.read_text() == original and path.stat().st_mode & 0o777 == 0o640
    assert state(machine)["schema"] == "fucc-machine-public-v3" and "firefox" not in state(machine)["managedBrowsers"]
    assert {item["id"] for item in read_resource_bindings(machine["root"] / "machine")} >= {"policy.firefox", "firefox-profile"}
    action(machine, "on", "us", firefox=True)
    assert policy_aligned(path.read_text(), "us")
    action(machine, "off")
    assert path.read_text() == original and machine["zone"][0] == "Asia/Tokyo"


def test_legacy_off_does_not_enroll_newly_installed_firefox(machine):
    action(machine, "on", "us")
    path = enable_firefox(machine)
    original = original_policy(path)
    assert action(machine, "off")["kind"] == "restored"
    assert path.read_text() == original and path.stat().st_mode & 0o777 == 0o640
    assert state(machine)["schema"] == "fucc-machine-public-v1"


def test_enrolled_firefox_restores_original_after_uninstall_and_lost_public_mirror(machine):
    path = enable_firefox(machine)
    original = original_policy(path)
    action(machine, "on", "us", firefox=True)
    enable_firefox(machine, "not_installed", False)
    (machine["root"] / "status.json").unlink()
    result = action(machine, "recover")
    assert result["firefoxManaged"] is True and result["health"] == "healthy"
    assert action(machine, "off")["kind"] == "restored"
    assert path.read_text() == original and machine["zone"][0] == "Asia/Tokyo"


def test_failed_firefox_enrollment_compensates_to_original_resource_collection(machine):
    action(machine, "on", "us")
    path = enable_firefox(machine)
    core = read_resource_bindings(machine["root"] / "machine")
    from fucc.machine import replace_snapshot as real_replace

    def fail(path_arg, desired, expected):
        if Path(path_arg) == path and desired["text"] is not None:
            raise OSError("simulated policy failure")
        return real_replace(path_arg, desired, expected)

    with patch("fucc.machine.replace_snapshot", side_effect=fail):
        result = action(machine, "on", "us", firefox=True)
    assert result["kind"] == "compensated" and result["health"] == "healthy"
    assert "firefoxManaged" not in result and read_resource_bindings(machine["root"] / "machine") == core
    assert not path.exists() and machine["zone"][0] == "America/New_York"
    assert action(machine, "off")["kind"] == "restored" and machine["zone"][0] == "Asia/Tokyo"


def test_interrupted_firefox_enrollment_recovers_previous_core_profile(machine):
    action(machine, "on", "us")
    path = enable_firefox(machine)
    profile = machine["root"] / "machine" / "firefox-profile.json"
    from fucc.machine import replace_snapshot as real_replace

    def fail(path_arg, desired, expected):
        if Path(path_arg) == path and desired["text"] is not None:
            real_replace(path_arg, desired, expected)
            raise OSError("interrupted policy write")
        if Path(path_arg) == profile and desired["text"] is None:
            raise OSError("interrupted profile rollback")
        return real_replace(path_arg, desired, expected)

    with patch("fucc.machine.replace_snapshot", side_effect=fail):
        result = action(machine, "on", "us", firefox=True)
    assert result["kind"] == "recovery_required" and result["recoveryRequired"] is True
    assert "firefoxManaged" not in result  # Pending enrollment is not a committed capability.
    assert action(machine, "recover")["kind"] == "recovered"
    assert not path.exists() and not profile.exists()
    assert machine["zone"][0] == "America/New_York"
    assert action(machine, "off")["kind"] == "restored"


def test_firefox_user_policy_edits_are_not_overwritten(machine):
    path = enable_firefox(machine)
    action(machine, "on", "us", firefox=True)
    modified = path.read_text().replace('"en-US,en"', '"fr"')
    path.write_text(modified)
    result = action(machine, "status")
    assert result["health"] == "degraded"
    assert action(machine, "off")["kind"] == "recovery_required"
    assert path.read_text() == modified


def test_firefox_machine_descriptor_and_helper_arguments_are_explicit(machine):
    enable_firefox(machine)
    with patch("fucc.machine.os.getuid", return_value=1000):
        adapter = MachineAdapter("/opt/fucc/scripts/fucc", "/usr/bin/python3", test_options=machine["options"])
    assert adapter.desired("us")["firefox"] is True
    calls = []

    def helper(command, **kwargs):
        calls.append(command)
        managed = command[-1] == "on"
        return type("Result", (), {"returncode": 0, "stdout": json.dumps({"scope": "machine", "active": True,
                    "region": "us", "ownerUid": 1000, "recoveryRequired": False, "firefoxManaged": managed})})()

    with patch("fucc.machine.subprocess.run", side_effect=helper):
        adapter.resource.write(adapter.desired("us"))
    assert calls[0][-2:] == ["--firefox", "on"]
    action(machine, "on", "us", firefox=True)
    with patch("fucc.machine.subprocess.run", side_effect=helper):
        adapter.resource.write({"active": True, "region": "us", "ownerUid": 1000})
    assert calls[-1][-2:] == ["--firefox", "off"]
    assert _valid_switch({"active": True, "region": "us", "ownerUid": 1000, "firefox": True})
    assert not _valid_switch({"active": True, "region": "us", "ownerUid": 1000, "firefox": False})


def test_public_firefox_marker_requires_v3_and_matching_management_list(machine):
    enable_firefox(machine)
    action(machine, "on", "us", firefox=True)
    public = state(machine)
    assert _valid_public(public)
    assert not _valid_public({**public, "schema": "fucc-machine-public-v1"})
    assert not _valid_public({**public, "firefoxManaged": False})
    assert not _valid_public({**public, "firefoxManaged": "yes"})


@pytest.mark.parametrize("resource_id", ["policy.firefox", "policy.chrome", "owner", "firefox-profile"])
def test_resource_write_never_upgrades_expected_snapshot_to_unknown_external_edit(machine, resource_id):
    firefox = enable_firefox(machine)
    profile = machine["root"] / "machine" / "firefox-profile.json"
    owner = machine["root"] / "machine" / "owner.json"
    targets = {"policy.firefox": firefox, "policy.chrome": machine["chrome"], "owner": owner, "firefox-profile": profile}
    external = ('{"ownerUid":1001}\n' if resource_id == "owner" else '{"enabled":false}\n'
                if resource_id == "firefox-profile" else '{"policies":{"Homepage":{"URL":"https://external.test"}}}\n'
                if resource_id == "policy.firefox" else '{"HomepageLocation":"https://external.test"}\n')
    from fucc.machine import _resources as factory
    inserted = [False]

    def resources(*args, **kwargs):
        result = factory(*args, **kwargs)
        resource = next((item for item in result if item.id == resource_id), None)
        if resource is not None:
            writer = resource.write

            def racing_write(value):
                if not inserted[0]:
                    inserted[0] = True
                    path = targets[resource_id]
                    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                    path.write_text(external)
                    path.chmod(0o600 if resource_id in ("owner", "firefox-profile") else 0o644)
                return writer(value)
            resource.write = racing_write
        return result

    with patch("fucc.machine._resources", side_effect=resources):
        result = action(machine, "on", "us", firefox=True)
    assert inserted[0] and result["kind"] == "recovery_required"
    assert targets[resource_id].read_text() == external
    assert machine["zone"][0] == "Asia/Tokyo"


@pytest.mark.parametrize("verb", ["on", "recover", "off"])
def test_lost_private_authorities_never_recapture_protected_configuration_as_original(machine, verb):
    path = enable_firefox(machine)
    action(machine, "on", "us", firefox=True)
    root = machine["root"] / "machine"
    for name in ("local-transaction.json", "local-transaction.json.prev", "local-transaction.initialized",
                 "local-transaction.initialized.prev"):
        (root / name).unlink(missing_ok=True)
    protected = [path, root / "owner.json", root / "firefox-profile.json", machine["root"] / "status.json"]
    before = {path: path.read_bytes() for path in protected}
    with pytest.raises(FuccError) as exc:
        action(machine, verb, "us" if verb == "on" else None, firefox=verb == "on")
    assert exc.value.code == "MACHINE_STATE_INVALID"
    assert {path: path.read_bytes() for path in protected} == before
    assert machine["zone"][0] == "America/New_York"
    assert not (root / "local-transaction.json").exists()


def test_complete_private_restore_can_recover_stale_active_public_mirror(machine):
    enable_firefox(machine)
    action(machine, "on", "us", firefox=True)
    public = (machine["root"] / "status.json").read_bytes()
    action(machine, "off")
    (machine["root"] / "status.json").write_bytes(public)
    result = action(machine, "recover")
    assert result["active"] is False and result["health"] == "healthy"
    assert machine["zone"][0] == "Asia/Tokyo"


@pytest.mark.parametrize("evidence", ["pending-public", "private-profile"])
def test_initial_lifecycle_evidence_without_authorities_also_blocks_reinitialization(machine, evidence):
    from fucc.machine import _blank
    root = machine["root"]
    if evidence == "pending-public":
        public = {**_blank(), "ownerUid": 1000, "recoveryRequired": True, "transaction": "apply"}
        (root / "status.json").write_text(json.dumps(public))
        (root / "status.json").chmod(0o644)
    else:
        private_root = root / "machine"
        private_root.mkdir(mode=0o700)
        (private_root / "firefox-profile.json").write_text('{"enabled":true}\n')
        (private_root / "firefox-profile.json").chmod(0o600)
    with pytest.raises(FuccError) as exc:
        action(machine, "recover")
    assert exc.value.code == "MACHINE_STATE_INVALID"
    assert not (root / "machine" / "local-transaction.json").exists()
    assert machine["zone"][0] == "Asia/Tokyo"
