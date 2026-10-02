"""Dynamic US target propagation and recovery across the actual nested adapters."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys

import pytest

from fucc import cli
from fucc.jsonc import parse
from fucc.machine import MachineAdapter, run_machine_action
from fucc.paths import persist_root
from fucc.regions import target_environment
from fucc.runtime import LocalRuntime
from test_machine import machine as machine_fixture

machine = machine_fixture


def intelligence(timezone="America/Los_Angeles", country="US"):
    return {"ip": "203.0.113.40", "country": country, "region": "California", "city": "Los Angeles",
            "timezone": timezone, "sources": ["ipwho.is", "ipinfo.io"], "sourceCount": 2,
            "multiSourceConsistent": True, "geolocationConsistent": True,
            "timezoneSources": [{"source": source, "country": country, "timezone": timezone}
                                for source in ("ipwho.is", "ipinfo.io")]}


@pytest.fixture
def managed(machine, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text("# original startup\n")
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "google-chrome.desktop").write_text("[Desktop Entry]\nName=Chrome\nExec=/usr/bin/google-chrome %U\n")
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/bash", "XDG_DATA_DIRS": str(tmp_path / "empty")}
    for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "CLAUDE_CONFIG_DIR", "TZ", "LC_ALL"):
        monkeypatch.delenv(name, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("fucc.bash.list_linux_locales", lambda: ["en_US.utf8", "ja_JP.utf8"])
    machine["options"]["sudo_uid"] = lambda: str(os.getuid())
    adapter = MachineAdapter(str(Path(cli.__file__).with_name("helper.py")), sys.executable, env, machine["options"])

    def helper(python_path, entrypoint, action, owner_uid, environment, region=None, timezone=None, firefox=False):
        state = run_machine_action(action, region, owner_uid, machine["options"], timezone=timezone, firefox=firefox is True)
        if state["kind"] in ("compensated", "recovery_required"):
            raise OSError("fixture helper did not commit")

    monkeypatch.setattr("fucc.machine._invoke_helper", helper)
    factory = lambda: LocalRuntime(env, [str(apps)], adapter, lambda: ["en_US.utf8", "ja_JP.utf8"])
    monkeypatch.setattr(cli, "LocalRuntime", factory)
    return factory, env, machine


def invoke(capsys, *args, expected=0):
    assert cli.main([*args, "--json"]) == expected
    return json.loads(capsys.readouterr().out)


def assert_all_targets(runtime, zone):
    status = runtime.status()
    expected = target_environment("us", zone)
    assert status["health"] == "healthy" and status["configured"] == expected
    assert status["bash"]["configured"] == expected
    assert status["machine"]["configuredTimezone"] == zone
    assert status["machine"]["actualTimezone"] == zone
    assert all(resource["aligned"] for resource in status["resources"])
    assert json.loads(Path(runtime.user.paths["claudeSettings"]).read_text())["env"] == expected
    rows = parse(Path(runtime.user.paths["vscodeSettings"]).read_text())["claudeCode.environmentVariables"]
    assert {entry["name"]: entry["value"] for entry in rows} == expected
    assert f"TZ={zone}" in Path(runtime.user.paths["sessionFile"]).read_text()


def originals(runtime):
    ledger = json.loads((runtime.root / "local-transaction.json").read_text())["payload"]
    return {resource["id"]: deepcopy(resource["original"]) for resource in ledger["committed"]["resources"]}


def install_firefox(machine, kind="snap"):
    browser = next(browser for browser in machine["browsers"] if browser["id"] == "firefox")
    browser.update(kind=kind, policySupported=True, snapInterfaceConnected=True if kind == "snap" else None)
    path = Path(browser["directory"]) / "policies.json"
    path.parent.mkdir(parents=True, mode=0o755)
    path.parent.parent.chmod(0o755)
    path.write_text('{"policies":{"DisableTelemetry":true,"Preferences":{"privacy.resistFingerprinting":true,"intl.accept_languages":"fr"}}}\n')
    path.chmod(0o640)
    return path


def test_snap_firefox_upgrade_enrollment_and_full_restore(managed):
    factory, _, machine = managed
    runtime = factory()
    runtime.on("us", timezone="America/Los_Angeles")
    outer_baseline = originals(runtime)
    private_path = machine["root"] / "machine/local-transaction.json"
    old = json.loads(private_path.read_text())["payload"]
    assert old["version"] == 2
    old_originals = {entry["id"]: entry["original"] for entry in old["committed"]["resources"]}
    path = install_firefox(machine)
    baseline = path.read_bytes()
    assert factory().on("us", timezone="America/Los_Angeles")["kind"] == "committed"
    status = factory().status()
    assert status["health"] == "healthy" and status["machine"]["firefoxManaged"] is True
    firefox = next(browser for browser in status["machine"]["browsers"] if browser["id"] == "firefox")
    assert firefox["installation"] == "snap" and firefox["configurationAligned"]
    assert firefox["effectivePolicy"] == "verify_in_browser" and firefox["verificationPage"] == "about:policies"
    assert originals(factory()) == outer_baseline
    ledger = json.loads(private_path.read_text())["payload"]
    assert ledger["version"] == 3
    assert {entry["id"]: entry["original"] for entry in ledger["committed"]["resources"] if entry["id"] in old_originals} == old_originals
    policies = json.loads(path.read_text())["policies"]
    assert policies["DisableTelemetry"] is True and policies["Preferences"]["privacy.resistFingerprinting"] is True
    assert policies["Preferences"]["intl.accept_languages"] == {"Value": "en-US,en", "Status": "locked"}
    assert factory().on("us", timezone="America/Los_Angeles")["kind"] == "noop"
    assert factory().off()["kind"] == "restored"
    assert path.read_bytes() == baseline and path.stat().st_mode & 0o777 == 0o640
    assert machine["zone"][0] == "Asia/Tokyo"
    assert not (machine["root"] / "machine/firefox-profile.json").exists()


def test_outer_failure_disables_new_firefox_profile_without_rebaselining(managed):
    factory, _, machine = managed
    runtime = factory()
    runtime.on("us", timezone="America/Los_Angeles")
    path = install_firefox(machine)
    baseline = path.read_bytes()
    original_write = runtime.machine.resource.write
    failed = [False]

    def fail_after_nested_commit(value):
        original_write(value)
        if value.get("firefox") is True and not failed[0]:
            failed[0] = True
            raise OSError("fixture outer failure after Firefox commit")

    runtime.machine.resource.write = fail_after_nested_commit
    assert runtime.on("us", timezone="America/Los_Angeles")["kind"] == "compensated"
    assert_all_targets(factory(), "America/Los_Angeles")
    assert path.read_bytes() == baseline and path.stat().st_mode & 0o777 == 0o640
    public = json.loads((machine["root"] / "status.json").read_text())
    assert public["schema"] == "fucc-machine-public-v3" and public["firefoxManaged"] is False
    assert "firefox" not in runtime.machine.resource.read()
    saved = json.loads((machine["root"] / "machine/local-transaction.json").read_text())["payload"]
    firefox_original = next(entry["original"] for entry in saved["committed"]["resources"] if entry["id"] == "policy.firefox")
    assert firefox_original == {"text": baseline.decode(), "mode": 0o640}
    assert factory().on("us", timezone="America/Los_Angeles")["kind"] == "committed"
    assert factory().off()["kind"] == "restored" and path.read_bytes() == baseline


def test_firefox_user_only_and_external_policy_drift_are_preserved(managed):
    factory, _, machine = managed
    path = install_firefox(machine)
    baseline = path.read_bytes()
    assert factory().on("us", user_only=True)["kind"] == "committed"
    assert path.read_bytes() == baseline and machine["zone"][0] == "Asia/Tokyo"
    assert factory().on("us")["kind"] == "committed"
    data = json.loads(path.read_text())
    data["policies"]["Preferences"]["intl.accept_languages"]["Value"] = "fr"
    path.write_text(json.dumps(data))
    changed = path.read_bytes()
    assert factory().status()["health"] == "degraded"
    assert factory().off()["kind"] == "recovery_required"
    assert factory().recover()["kind"] == "recovery_required"
    assert path.read_bytes() == changed


def test_ip_selected_target_reaches_all_adapters_and_restores_initial_baseline(managed, capsys, monkeypatch):
    factory, env, machine = managed
    calls = []
    current = [intelligence()]
    monkeypatch.setattr(cli, "fetch_ip_intelligence", lambda: calls.append(True) or current[0])
    first = invoke(capsys, "on", "--region", "us")
    assert first["timezoneSelection"]["source"] == "ip" and len(calls) == 1
    assert_all_targets(factory(), "America/Los_Angeles")
    baseline = originals(factory())
    current[0] = intelligence("America/Chicago")
    second = invoke(capsys, "on", "--region", "us")
    assert second["timezoneSelection"]["timezone"] == "America/Chicago" and len(calls) == 2
    assert_all_targets(factory(), "America/Chicago")
    assert originals(factory()) == baseline
    public = json.loads((machine["root"] / "status.json").read_text())
    assert public["schema"] == "fucc-machine-public-v2" and public["timezone"] == "America/Chicago"
    # New IP observations during status/off must not replace the committed target.
    current[0] = intelligence("America/Phoenix")
    status = invoke(capsys, "status")
    assert status["network"]["timezone"] == "America/Phoenix"
    assert status["status"]["configured"]["TZ"] == "America/Chicago"
    assert invoke(capsys, "off")["result"] == "restored"
    assert machine["zone"][0] == "Asia/Tokyo"
    assert (Path(env["HOME"]) / ".bashrc").read_text() == "# original startup\n"
    assert json.loads(Path(factory().user.paths["claudeSettings"]).read_text()).get("env", {}) == {}
    assert not Path(factory().user.paths["sessionFile"]).exists()
    # Only the selected target, not the observed IP/city, is durable.
    for path in persist_root(env).rglob("*.json"):
        assert "203.0.113.40" not in path.read_text() and '"city"' not in path.read_text()


@pytest.mark.parametrize("failure", ["offline", "foreign", "inconsistent", "invalid_zone"])
def test_missing_or_untrusted_ip_preserves_saved_us_timezone(managed, capsys, monkeypatch, failure):
    factory, _, _ = managed
    factory().on("us", timezone="America/Los_Angeles")
    observation = None if failure == "offline" else intelligence()
    if failure == "foreign":
        observation = intelligence("Asia/Tokyo", "JP")
    elif failure == "inconsistent":
        observation["timezoneSources"][1]["timezone"] = "America/Chicago"
    elif failure == "invalid_zone":
        observation = intelligence("Asia/Tokyo")
    monkeypatch.setattr(cli, "fetch_ip_intelligence", lambda: observation)
    result = invoke(capsys, "on", "--region", "us")
    assert result["result"] == "noop" and result["timezoneSelection"]["source"] == "saved"
    if failure == "offline":
        assert "lookup unavailable" in result["timezoneSelection"]["reason"]
    assert_all_targets(factory(), "America/Los_Angeles")


def test_initial_offline_default_and_explicit_override(managed, capsys, monkeypatch):
    factory, _, _ = managed
    monkeypatch.setattr(cli, "fetch_ip_intelligence", lambda: None)
    first = invoke(capsys, "on", "--region", "us")
    assert first["timezoneSelection"]["source"] == "default"
    assert_all_targets(factory(), "America/New_York")
    monkeypatch.setattr(cli, "fetch_ip_intelligence", intelligence)
    explicit = invoke(capsys, "on", "--region", "us", "--timezone", "US/Arizona")
    assert explicit["timezoneSelection"] == {"timezone": "America/Phoenix", "source": "explicit",
                                              "reason": "Explicit timezone override"}
    assert_all_targets(factory(), "America/Phoenix")
    for value in ("Asia/Tokyo", "../../etc/passwd", "America/Los_Angeles\n"):
        assert invoke(capsys, "on", "--region", "us", "--timezone", value, expected=10)["error"]["id"] == "INVALID_TIMEZONE"
    assert_all_targets(factory(), "America/Phoenix")


def test_saved_timezone_is_used_by_run_and_check_without_reselecting(managed, capfd, monkeypatch):
    factory, _, _ = managed
    factory().on("us", timezone="America/Los_Angeles")
    monkeypatch.setattr(cli, "assert_locale", lambda region: None)
    def unexpected_network():
        pytest.fail("Saved run target must not query IP")
    monkeypatch.setattr(cli, "fetch_ip_intelligence", unexpected_network)
    assert cli.main(["run", "--", sys.executable, "-c", "import os,sys;print(os.environ['TZ']);sys.exit(7)"]) == 7
    assert capfd.readouterr().out == "America/Los_Angeles\n"
    seen = []
    def check(region, timezone=None):
        seen.append((region, timezone))
        return {"targetTimezone": timezone}
    monkeypatch.setattr(cli, "check", check)
    result = invoke(capfd, "check")
    assert result["timezoneSelection"]["source"] == "saved" and seen[-1] == ("us", "America/Los_Angeles")
    invoke(capfd, "check", "--timezone", "America/Denver")
    assert seen[-1] == ("us", "America/Denver")
    assert_all_targets(factory(), "America/Los_Angeles")


def test_nested_dynamic_failure_compensates_then_rebuilds_public_summary(managed, monkeypatch):
    factory, _, machine = managed
    runtime = factory()
    runtime.on("us", timezone="America/Los_Angeles")
    baseline = originals(runtime)
    def fail(zone):
        if zone == "America/Chicago":
            raise OSError("fixture failed switch")
    machine["failure"][0] = fail
    assert runtime.on("us", timezone="America/Chicago")["kind"] == "compensated"
    assert_all_targets(factory(), "America/Los_Angeles")
    assert originals(factory()) == baseline
    machine["failure"][0] = None
    (machine["root"] / "status.json").write_text("{corrupt")
    assert factory().status()["health"] == "recovery_required"
    assert factory().recover()["kind"] == "noop"
    assert_all_targets(factory(), "America/Los_Angeles")
    assert factory().off()["kind"] == "restored" and machine["zone"][0] == "Asia/Tokyo"


def test_dynamic_external_timezone_drift_is_preserved(managed):
    factory, _, machine = managed
    factory().on("us", timezone="America/Los_Angeles")
    machine["zone"][0] = "America/Denver"
    status = factory().status()
    assert status["configured"]["TZ"] == "America/Los_Angeles" and status["health"] == "degraded"
    assert factory().off()["kind"] == "recovery_required"
    assert factory().recover()["kind"] == "recovery_required"
    assert machine["zone"][0] == "America/Denver"


def test_diagnostics_use_outer_committed_target_after_nested_bash_switch_interrupt(managed, capsys, monkeypatch):
    factory, _, _ = managed
    runtime = factory()
    runtime.on("us", timezone="America/Los_Angeles")
    def interrupt(value):
        raise KeyboardInterrupt("fixture power loss after Bash commit")
    index = next(index for index, resource in enumerate(runtime.transaction.resources) if resource.id == "bash-profile")
    runtime.transaction.resources[index + 1].write = interrupt
    with pytest.raises(KeyboardInterrupt):
        runtime.on("us", timezone="America/Chicago")
    assert factory().bash.status()["platform"]["configured"]["TZ"] == "America/Chicago"
    assert factory().status()["configured"]["TZ"] == "America/Los_Angeles"
    assert cli.saved_timezone("us") == "America/Los_Angeles"
    seen = []
    monkeypatch.setattr(cli, "check", lambda region, timezone=None: seen.append((region, timezone)) or {})
    invoke(capsys, "check")
    assert seen == [("us", "America/Los_Angeles")]
    assert factory().recover()["kind"] == "recovered"
    assert_all_targets(factory(), "America/Los_Angeles")


def test_stateless_run_uses_ip_timezone_without_saving_profile(managed, capfd, monkeypatch):
    _, env, _ = managed
    monkeypatch.setattr(cli, "fetch_ip_intelligence", intelligence)
    monkeypatch.setattr(cli, "assert_locale", lambda region: None)
    before = {str(path): path.read_bytes() for path in Path(env["HOME"]).rglob("*") if path.is_file()}
    assert cli.main(["run", "--region", "us", "--", sys.executable, "-c", "import os;print(os.environ['TZ'])"]) == 0
    assert capfd.readouterr().out == "America/Los_Angeles\n"
    assert {str(path): path.read_bytes() for path in Path(env["HOME"]).rglob("*") if path.is_file()} == before
