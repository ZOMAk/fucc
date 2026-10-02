from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess

import pytest

from fucc.errors import FuccError
from fucc.firefox import (MANAGED_PREFERENCES, POLICY_PATH, desired_snapshot, discover_firefox,
                          managed_preferences, merge_policies, parse_policies, policy_aligned,
                          read_policy_snapshot, snap_policy_interface_connected)


@pytest.mark.parametrize("region,languages", [("us", "en-US,en"), ("eu", "en-GB,en"), ("jp", "ja-JP,ja"), ("sg", "en-SG,en")])
def test_firefox_prefs_match_mozilla_mapping_and_keep_webrtc_enabled(region, languages):
    preferences = managed_preferences(region)
    assert set(preferences) == set(MANAGED_PREFERENCES)
    assert preferences["intl.accept_languages"] == {"Value": languages, "Status": "locked"}
    for key in MANAGED_PREFERENCES[1:]:
        assert preferences[key] == {"Value": True, "Status": "locked"}
    assert "media.peerconnection.enabled" not in preferences
    assert "media.peerconnection.ice.proxy_only" not in preferences
    assert not any(key.startswith("network.proxy") or key == "privacy.resistFingerprinting" for key in preferences)


def test_merge_preserves_unrelated_policies_and_preferences_without_mutating_baseline():
    original = {"metadata": {"owner": "admin"}, "policies": {"Homepage": {"URL": "https://intranet.example"},
                "Preferences": {"intl.accept_languages": {"Value": "fr-FR,fr", "Status": "default"},
                "media.peerconnection.enabled": False, "media.peerconnection.ice.proxy_only": True,
                "privacy.resistFingerprinting": True, "network.proxy.type": 1,
                "browser.tabs.warnOnClose": {"Value": True, "Status": "user"},
                "browser.startup.page": {"Status": "clear"}}}}
    before = deepcopy(original)
    merged = merge_policies(json.dumps(original), "us")
    assert original == before
    assert merged["metadata"] == original["metadata"] and merged["policies"]["Homepage"] == original["policies"]["Homepage"]
    for key, value in original["policies"]["Preferences"].items():
        if key not in MANAGED_PREFERENCES:
            assert merged["policies"]["Preferences"][key] == value
    assert merged["policies"]["Preferences"]["intl.accept_languages"] == {"Value": "en-US,en", "Status": "locked"}
    assert policy_aligned(json.dumps(merged), "us")


def test_desired_snapshot_is_read_only_and_does_not_change_original_bytes_or_mode(tmp_path):
    path = tmp_path / "policies.json"
    original = '{ "policies": { "Homepage": {"URL":"https://example.test"} } }\n'
    path.write_text(original)
    path.chmod(0o640)
    snapshot = read_policy_snapshot(path)
    desired = desired_snapshot(snapshot, "us")
    assert snapshot == {"text": original, "mode": 0o640}
    assert desired["mode"] == 0o644 and desired["text"].endswith("\n")
    assert policy_aligned(desired["text"], "us")
    assert path.read_text() == original and path.stat().st_mode & 0o777 == 0o640
    assert desired_snapshot({"text": None, "mode": None}, "jp")["text"]
    assert parse_policies(None) == {"policies": {}}


@pytest.mark.parametrize("text", ["", "   ", "[]", "null", "{}", '{"policies":[]}', '{"policies":null}',
                                  '{"policies":{"Preferences":[]}}', '{"policies":{"Preferences":null}}',
                                  '{"policies":{"Preferences":{"some.pref":[]}}}',
                                  '{"policies":{"Preferences":{"some.pref":{"Value":{},"Status":"locked"}}}}',
                                  '{"policies":{"Preferences":{"some.pref":{"Status":"bad"}}}}',
                                  '{"policies":{"Preferences":{"some.pref":{"Value":true,"Type":"string"}}}}',
                                  '{"policies":{"Preferences":{"intl.accept_languages":true}}}',
                                  '{"policies":{"Preferences":{"media.peerconnection.ice.no_host":"true"}}}',
                                  '{"policies":{"Preferences":{"media.peerconnection.ice.no_host":1}}}',
                                  '{"policies":{"Preferences":{"media.peerconnection.ice.no_host":{"Status":"locked"}}}}',
                                  '{"policies":{"Preferences":{"some.pref":NaN}}}',
                                  '{"policies":{"Preferences":{"some.pref":1e999}}}',
                                  '{"policies":{"Preferences":{"some.pref":"\\ud800"}}}',
                                  '{"policies":{"SECRET":"https://user:secret@server/"},"policies":{}}',
                                  '{"policies":{"Preferences":{},"Preferences":{}}}',
                                  '{"policies":{"Preferences":{"intl.accept_languages":"en","intl.accept_languages":"fr"}}}',
                                  '{"policies":{"Preferences":{"some.pref":{"Value":true,"Value":false}}}}',
                                  '{"policies":{"Homepage":{"URL":"a","URL":"b"}}}'])
def test_invalid_or_duplicate_policy_json_fails_without_disclosing_contents(text):
    with pytest.raises(FuccError) as error:
        parse_policies(text)
    assert error.value.code == "CONFIG_CONFLICT" and error.value.exit_code == 24
    assert "secret" not in str(error.value)
    assert not policy_aligned(text, "us")


@pytest.mark.parametrize("snapshot", [{"text": None, "mode": 0o644}, {"text": "{}", "mode": None},
                                      {"text": "{}", "mode": True}, {"text": "{}", "mode": 0o1000},
                                      {"text": "{}"}, {"text": "{}", "mode": 0o644, "path": "/tmp/evil"}])
def test_invalid_snapshot_shape_is_rejected(snapshot):
    with pytest.raises(FuccError):
        desired_snapshot(snapshot, "us")


def test_region_is_not_interpolated_into_policy_or_shell():
    with pytest.raises(FuccError) as error:
        managed_preferences("us; touch /tmp/injected")
    assert error.value.code == "INVALID_REGION"
    with pytest.raises(FuccError):
        merge_policies(None, "US")


def test_readback_checks_lock_and_values_and_rejects_boolean_impostors():
    baseline = merge_policies(None, "us")
    for key in MANAGED_PREFERENCES:
        modified = deepcopy(baseline)
        modified["policies"]["Preferences"][key]["Status"] = "default"
        assert not policy_aligned(json.dumps(modified), "us")
    modified = deepcopy(baseline)
    modified["policies"]["Preferences"][MANAGED_PREFERENCES[1]]["Value"] = 1
    assert not policy_aligned(json.dumps(modified), "us")
    assert not policy_aligned(None, "us")
    assert not policy_aligned(json.dumps(baseline), "eu")


@pytest.mark.parametrize("link_kind", ["symlink", "parent_symlink", "hardlink", "fifo"])
def test_policy_read_rejects_links_and_special_files(tmp_path, link_kind):
    target = tmp_path / "source.json"
    target.write_text('{"policies":{}}')
    policy = tmp_path / "policies.json"
    if link_kind == "symlink":
        policy.symlink_to(target)
    elif link_kind == "parent_symlink":
        real = tmp_path / "real"
        real.mkdir()
        (real / "policies.json").write_text('{"policies":{}}')
        (tmp_path / "linked").symlink_to(real, target_is_directory=True)
        policy = tmp_path / "linked/policies.json"
    elif link_kind == "hardlink":
        os.link(target, policy)
    else:
        os.mkfifo(policy)
    with pytest.raises(FuccError):
        read_policy_snapshot(policy)


def test_snap_interface_parsing_requires_exact_connected_host_slot():
    assert snap_policy_interface_connected("Interface Plug Slot Notes\nsystem-files firefox:etc-firefox :system-files -\n")
    for text in (None, "", "system-files firefox:etc-firefox - -", "system-files firefox:etc-firefoxx :system-files -",
                 "system-files another:etc-firefox :system-files -", "network firefox:etc-firefox :system-files -",
                 "system-files firefox:etc-firefox untrusted:system-files -"):
        assert not snap_policy_interface_connected(text)


def installation(tmp_path, text):
    binary = tmp_path / "firefox"
    binary.write_text(text)
    binary.chmod(0o755)
    return binary


def discovery(tmp_path, **kwargs):
    return discover_firefox(candidates=kwargs.pop("candidates", []), snap_path=kwargs.pop("snap_path", str(tmp_path / "missing-snap")),
                            flatpak_dirs=kwargs.pop("flatpak_dirs", []), trusted_uid=os.getuid(),
                            system_policy_path=kwargs.pop("system_policy_path", tmp_path / "etc/policies.json"),
                            distribution_paths=kwargs.pop("distribution_paths", []), **kwargs)


def test_native_discovery_inspects_scripts_without_executing_them(tmp_path, monkeypatch):
    marker = tmp_path / "would-run"
    binary = installation(tmp_path, f"#!/bin/sh\ntouch {marker}\n")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("Native discovery must not execute anything"))
    result = discovery(tmp_path, candidates=[binary])
    assert result["kind"] == "native" and result["policySupported"] is True
    assert result["snapInterfaceConnected"] is None and result["policyPath"] == POLICY_PATH
    assert "about:policies" in result["message"] and not marker.exists()


@pytest.mark.parametrize("connected", [True, False])
def test_snap_wrapper_discovery_checks_interface_without_executing_browser(tmp_path, connected):
    binary = installation(tmp_path, "#!/bin/sh\nexec /snap/bin/firefox \"$@\"\n")
    snap = tmp_path / "snap-firefox"
    snap.touch()
    calls = []
    connections = "system-files firefox:etc-firefox " + (":system-files -" if connected else "- -")
    result = discovery(tmp_path, candidates=[binary], snap_path=str(snap), snap_connections=lambda: calls.append(True) or connections)
    assert result["kind"] == "snap" and result["policySupported"] is connected
    assert result["snapInterfaceConnected"] is connected and calls == [True]
    assert result["policyPath"] == POLICY_PATH
    assert ("已连接" if connected else "未连接") in result["message"]


def test_snap_cli_missing_or_connection_query_failure_is_not_reported_supported(tmp_path):
    snap = tmp_path / "snap-firefox"
    snap.touch()
    result = discovery(tmp_path, snap_path=str(snap), snap_connections=lambda: None)
    assert result["kind"] == "snap" and result["policySupported"] is False
    assert "无法核实" in result["message"]


def test_flatpak_is_distinct_from_not_installed_and_not_managed(tmp_path):
    flatpak = tmp_path / "flatpak-org.mozilla.firefox"
    flatpak.mkdir()
    result = discovery(tmp_path, flatpak_dirs=[flatpak])
    assert result["kind"] == "flatpak" and result["policySupported"] is False and "不支持" in result["message"]
    absent = discovery(tmp_path)
    assert absent["kind"] == "not_installed" and "未发现" in absent["message"]
    assert absent["snapInterfaceConnected"] is None


def test_world_writable_candidate_is_not_trusted_as_native_firefox(tmp_path):
    binary = installation(tmp_path, "untrusted browser")
    binary.chmod(0o777)
    assert discovery(tmp_path, candidates=[binary])["kind"] == "not_installed"


def test_snap_query_uses_fixed_system_client_args_and_clean_env(monkeypatch):
    from fucc import firefox
    original = Path.stat
    def safe_stat(path, *args, **kwargs):
        if str(path) == "/usr/bin/snap":
            return type("Info", (), {"st_mode": 0o100755, "st_uid": 0})()
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", lambda path, **kwargs: path)
    monkeypatch.setattr(Path, "stat", safe_stat)
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return type("Result", (), {"stdout": "system-files firefox:etc-firefox :system-files -\n"})()
    monkeypatch.setattr(subprocess, "run", run)
    assert firefox._snap_connections()
    args, options = calls[0]
    assert args == ["/usr/bin/snap", "connections", "firefox"]
    assert options["timeout"] == 5 and options["check"] is True
    assert options["env"] == {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}


@pytest.mark.parametrize("kind", ["native", "snap"])
def test_distribution_policies_are_not_shadowed_by_a_new_host_file(tmp_path, kind):
    binary = installation(tmp_path, "#!/bin/sh\n" + ("exec /snap/bin/firefox\n" if kind == "snap" else "exit 0\n"))
    snap = tmp_path / "snap-firefox"
    snap.touch()
    policy = tmp_path / "distribution/policies.json"
    policy.parent.mkdir()
    text = '{"policies":{"DisableTelemetry":true,"SECRET":"private URL"}}\n'
    policy.write_text(text)
    result = discovery(tmp_path, candidates=[binary], snap_path=str(snap), distribution_paths=[policy],
                       snap_connections=lambda: "system-files firefox:etc-firefox :system-files -")
    assert result["kind"] == kind and result["policySupported"] is False
    assert result["distributionPolicyConflict"] is True and result["distributionPolicyPaths"] == [str(policy)]
    assert "遮蔽" in result["message"] and "SECRET" not in json.dumps(result) and "private URL" not in json.dumps(result)
    assert policy.read_text() == text and not (tmp_path / "etc/policies.json").exists()


@pytest.mark.parametrize("policy_kind", ["regular", "symlink", "broken_link", "fifo", "invalid"])
def test_distribution_unknown_or_linked_policy_also_blocks_shadowing_without_reading(tmp_path, policy_kind):
    binary = installation(tmp_path, "native")
    policy = tmp_path / "policies.json"
    if policy_kind in ("regular", "invalid"):
        policy.write_text('{"policies":{}}' if policy_kind == "regular" else "invalid existing policy")
    elif policy_kind in ("symlink", "broken_link"):
        target = tmp_path / "target"
        if policy_kind == "symlink":
            target.write_text('{"policies":{}}')
        policy.symlink_to(target)
    else:
        os.mkfifo(policy)
    result = discovery(tmp_path, candidates=[binary], distribution_paths=[policy])
    assert result["policySupported"] is False and result["distributionPolicyConflict"]


def test_existing_host_policy_already_has_precedence_and_can_be_managed(tmp_path):
    binary = installation(tmp_path, "native")
    system = tmp_path / "etc/policies.json"
    system.parent.mkdir()
    system.write_text('{"policies":{"DisableTelemetry":true}}')
    distribution = tmp_path / "distribution.json"
    distribution.write_text('{"policies":{"DisableAppUpdate":true}}')
    result = discovery(tmp_path, candidates=[binary], distribution_paths=[distribution], system_policy_path=system)
    assert result["policySupported"] is True and result["distributionPolicyConflict"] is False
    assert "优先" in result["message"] and result["distributionPolicyPaths"] == [str(distribution)]


def test_unreadable_distribution_metadata_is_not_treated_as_absent(tmp_path, monkeypatch):
    binary = installation(tmp_path, "native")
    policy = tmp_path / "distribution.json"
    original = Path.lstat
    def unreadable(path, *args, **kwargs):
        if path == policy:
            raise PermissionError("fixture metadata unreadable")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", unreadable)
    result = discovery(tmp_path, candidates=[binary], distribution_paths=[policy])
    assert result["policySupported"] is False and result["distributionPolicyConflict"]


def test_default_distribution_paths_are_fixed_to_detected_installation_kind(tmp_path, monkeypatch):
    from fucc import firefox
    calls = []
    monkeypatch.setattr(firefox, "_policy_presence", lambda path: calls.append(str(path)) or False)
    binary = installation(tmp_path, "native")
    discover_firefox(candidates=[binary], snap_path=str(tmp_path / "missing"), flatpak_dirs=[], trusted_uid=os.getuid())
    assert calls == [*firefox.NATIVE_DISTRIBUTION_POLICIES, POLICY_PATH]
    calls.clear()
    snap = tmp_path / "snap-firefox"
    snap.touch()
    discover_firefox(candidates=[], snap_path=str(snap), flatpak_dirs=[],
                     snap_connections=lambda: "system-files firefox:etc-firefox :system-files -")
    assert calls == [*firefox.SNAP_DISTRIBUTION_POLICIES, POLICY_PATH]
