import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from fucc import __version__, cli
from fucc.errors import FuccError
from test_runtime import FakeMachine
from fucc.runtime import LocalRuntime


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(cli, "fetch_ip_intelligence", lambda: None)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    env = {"HOME": str(tmp_path), "SHELL": "/bin/bash", "PATH": "/usr/bin:/bin", "XDG_DATA_DIRS": str(tmp_path / "empty")}
    for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "CLAUDE_CONFIG_DIR", "TZ", "LC_ALL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    machine = FakeMachine()
    monkeypatch.setattr(cli, "LocalRuntime", lambda: LocalRuntime(dict(os.environ), [], machine, lambda: ["en_US.utf8", "en_GB.utf8"]))
    monkeypatch.setattr("fucc.bash.list_linux_locales", lambda: ["en_US.utf8", "en_GB.utf8"])
    return env


def parsed(capsys):
    return json.loads(capsys.readouterr().out)


def test_local_json_contract_and_legacy_guard(environment, capsys):
    assert cli.main(["on", "--user-only", "--region", "us", "--json"]) == 0
    value = parsed(capsys)
    assert value["schemaVersion"] == 1 and value["ok"] and value["result"] == "committed"
    assert cli.main(["persist", "off", "--json"]) == 10
    assert parsed(capsys)["error"]["id"] == "INVALID_COMMAND"
    assert cli.main(["status", "--json"]) == 0
    assert parsed(capsys)["status"]["region"] == "us"
    assert cli.main(["off", "--json"]) == 0
    assert parsed(capsys)["status"]["active"] is False
    assert cli.main(["persist", "on", "--region", "eu", "--json"]) == 0
    assert parsed(capsys)["committed"] == {"mode": "standard", "region": "eu"}
    assert cli.main(["persist", "off", "--json"]) == 0
    assert parsed(capsys)["committed"] is None


def network_fixture():
    return {"ip": "203.0.113.4", "country": "JP", "region": "Tokyo", "city": "Tokyo",
            "sourceCount": 2, "sources": ["ipwho.is", "ipinfo.io"], "multiSourceConsistent": True,
            "timezone": "Asia/Tokyo", "geolocationConsistent": True,
            "timezoneSources": [{"source": source, "country": "JP", "timezone": "Asia/Tokyo"} for source in ("ipwho.is", "ipinfo.io")]}


def test_local_on_queries_ip_before_selection_once_and_does_not_persist_location(environment, capsys, monkeypatch):
    observations = []

    def observe():
        status = cli.LocalRuntime().status()
        assert not status["active"] and status["health"] == "healthy"
        observations.append(status["revision"])
        return network_fixture()

    monkeypatch.setattr(cli, "fetch_ip_intelligence", observe)
    assert cli.main(["on", "--user-only", "--region", "us", "--json"]) == 0
    value = parsed(capsys)
    assert value["result"] == "committed"
    assert value["network"] == {"availability": "available", "scope": "cli-process", **network_fixture()}
    assert value["status"]["region"] == "us"  # Observation does not replace the configured target.
    assert value["timezoneSelection"]["source"] == "default"
    assert "network" not in cli.LocalRuntime().status()
    assert len(observations) == 1
    for path in (Path(environment["HOME"]) / ".config/fucc").rglob("*.json"):
        assert "203.0.113.4" not in path.read_text()


def test_status_displays_ip_region_and_sources_in_ascii_table(environment, capsys, monkeypatch):
    monkeypatch.setattr(cli, "fetch_ip_intelligence", network_fixture)
    assert cli.main(["status"]) == 0
    text = capsys.readouterr().out
    assert "Egress IP" in text and "203.0.113.4" in text
    assert "IP region" in text and "JP / Tokyo / Tokyo" in text
    assert "ipwho.is, ipinfo.io (consistent)" in text
    assert all(character not in text for character in "✅⚠️╔║")


def test_single_source_and_missing_location_are_explicit(environment, capsys, monkeypatch):
    monkeypatch.setattr(cli, "fetch_ip_intelligence", lambda: {**network_fixture(), "region": None, "city": None,
                                                            "sourceCount": 1, "sources": ["ipinfo.io"]})
    assert cli.main(["status"]) == 0
    text = capsys.readouterr().out
    assert "JP / unknown / unknown" in text and "ipinfo.io (single source)" in text


def test_unavailable_ip_is_visible_in_plain_table(environment, capsys):
    assert cli.main(["on", "--user-only", "--region", "us"]) == 0
    text = capsys.readouterr().out
    assert "| committed" in text and "Egress IP" in text and "unavailable" in text


@pytest.mark.parametrize("command", ["on", "off", "status", "recover"])
@pytest.mark.parametrize("failure", ["none", "exception"])
def test_unavailable_ip_preserves_configuration_success(environment, capsys, monkeypatch, command, failure):
    def unavailable():
        if failure == "exception":
            raise RuntimeError("user:SECRET_PASSWORD@proxy")
        return None

    monkeypatch.setattr(cli, "fetch_ip_intelligence", unavailable)
    args = [command, "--json"] + (["--user-only", "--region", "us"] if command == "on" else [])
    assert cli.main(args) == 0
    value = parsed(capsys)
    assert value["ok"] is True and value["status"]["health"] == "healthy"
    assert value["network"]["availability"] == "unavailable"
    assert value["network"]["ip"] is None and value["network"]["sources"] == []
    assert "SECRET_PASSWORD" not in json.dumps(value)


@pytest.mark.parametrize("health,exit_code", [("degraded", 2), ("recovery_required", 21)])
def test_local_ip_failure_preserves_health_exit_code(environment, capsys, monkeypatch, health, exit_code):
    original = cli.LocalRuntime

    class DegradedRuntime:
        def status(self):
            return {**original().status(), "health": health}

    monkeypatch.setattr(cli, "LocalRuntime", DegradedRuntime)
    assert cli.main(["status", "--json"]) == exit_code
    value = parsed(capsys)
    assert value["status"]["health"] == health and value["network"]["availability"] == "unavailable"


def test_failed_local_transaction_queries_once_for_selection_then_preserves_failure(environment, capsys, monkeypatch):
    observations = []
    class FailedRuntime:
        def target_region(self, region):
            return region or "us"

        def saved_timezone(self, region):
            return None

        def on(self, *args):
            return {"kind": "compensated"}

    def observe():
        observations.append(True)
        return None

    monkeypatch.setattr(cli, "LocalRuntime", FailedRuntime)
    monkeypatch.setattr(cli, "fetch_ip_intelligence", observe)
    assert cli.main(["on", "--user-only", "--json"]) == 22
    assert parsed(capsys)["error"]["id"] == "COMPENSATED"
    assert observations == [True]


@pytest.mark.parametrize("args", [["region", "list"], ["region", "status"], ["persist", "status"]])
def test_preference_and_legacy_status_do_not_query_ip(environment, capsys, monkeypatch, args):
    def unexpected():
        pytest.fail("This command must not query network intelligence")

    monkeypatch.setattr(cli, "fetch_ip_intelligence", unexpected)
    assert cli.main([*args, "--json"]) == 0
    assert "network" not in parsed(capsys)


def test_provider_names_describe_only_successful_sources(environment):
    from fucc.network import fetch_ip_intelligence

    def second_only(url):
        return {"success": False} if "ipwho" in url else {"ip": "203.0.113.4", "country": "JP"}

    result = fetch_ip_intelligence(environment, second_only)
    assert result["sources"] == ["ipinfo.io"] and result["sourceCount"] == 1

    def both(url):
        return {"ip": "203.0.113.4", "country_code": "JP", "country": "JP"}

    assert fetch_ip_intelligence(environment, both)["sources"] == ["ipwho.is", "ipinfo.io"]


@pytest.mark.parametrize("args,identifier", [(["on", "--region", "cn"], "INVALID_REGION"), (["persist", "on", "--deep"], "CONFLICTING_PROTECTION_LEVEL"), (["persist", "on", "--level", "deep"], "INVALID_PROTECTION_LEVEL"), (["on", "--unknown"], "INVALID_COMMAND"), (["__machine", "on", "--owner", "nan"], "INVALID_COMMAND"), (["run", "--"], "INVALID_COMMAND")])
def test_invalid_input_exit_code(args, identifier, environment, capsys):
    assert cli.main([*args, "--json"] if args[0] != "run" else args) == 10
    output = capsys.readouterr()
    if args[0] == "run":
        assert "INVALID_COMMAND" in output.err
    else:
        assert json.loads(output.out)["error"]["id"] == identifier


def test_preference_status_and_plain_table(environment, capsys):
    assert cli.main(["region", "set", "eu", "--json"]) == 0
    assert parsed(capsys)["preferredRegion"] == "eu"
    assert cli.main(["region", "list"]) == 0
    output = capsys.readouterr().out
    assert "FUCC 0.0.2" in output and "Europe/London" in output
    assert all(character not in output for character in "✅⚠️╔║")
    assert cli.main(["region", "status", "--json"]) == 0
    assert parsed(capsys)["activeRegion"] is None


def test_helper_json_and_public_errors(environment, capsys, monkeypatch):
    monkeypatch.setattr(cli, "run_machine_action", lambda *args: {"scope": "machine", "active": False})
    assert cli.main(["__machine", "status", "--owner", "1000"]) == 0
    assert parsed(capsys)["scope"] == "machine"
    def fail(options):
        raise RuntimeError("SECRET_TOKEN")
    monkeypatch.setattr(cli, "dispatch", fail)
    assert cli.main(["status", "--json"]) == 30
    value = parsed(capsys)
    assert value["error"]["id"] == "INTERNAL" and "SECRET_TOKEN" not in str(value)


@pytest.mark.parametrize("code,expected", [("BUSY", 20), ("RECOVERY_REQUIRED", 21), ("COMPENSATED", 22), ("INCOMPLETE_RECOVERY", 23), ("CONFIG_CONFLICT", 24)])
def test_stable_error_codes(environment, capsys, monkeypatch, code, expected):
    def fail(options):
        raise FuccError("fixture", code, expected)
    monkeypatch.setattr(cli, "dispatch", fail)
    assert cli.main(["status", "--json"]) == expected
    assert parsed(capsys)["error"]["code"] == expected


def test_run_preserves_arguments_exit_and_parent_env(monkeypatch, capfd):
    monkeypatch.setattr(cli, "assert_locale", lambda region: None)
    before = os.environ.get("TZ")
    args = [sys.executable, "-c", "import os,sys;print(os.environ['TZ']);print(sys.argv[1]);sys.exit(7)", "literal;$SECRET"]
    assert cli.run_command(args, "us") == 7
    output = capfd.readouterr().out
    assert output == "America/New_York\nliteral;$SECRET\n"
    assert os.environ.get("TZ") == before
    assert cli.run_command([sys.executable, "-c", "import os,signal;os.kill(os.getpid(),signal.SIGTERM)"], "us") == 143


def test_source_console_module_and_isolated_helper_entrypoints():
    root = Path(__file__).resolve().parents[1]
    for args in ([str(root / "scripts/fucc"), "--version"], ["-m", "fucc", "--version"], ["-I", str(root / "src/fucc/helper.py"), "--version"]):
        result = subprocess.run([sys.executable, *args], check=True, capture_output=True, text=True)
        assert result.stdout.strip() == __version__
    result = subprocess.run([sys.executable, str(root / "scripts/fucc"), "--help"], check=True, capture_output=True, text=True)
    assert "__machine" not in result.stdout


def test_run_forwards_parent_termination_to_child():
    child_code = "import signal,sys,time;signal.signal(signal.SIGTERM,lambda *_:sys.exit(42));print('READY',flush=True);time.sleep(20)"
    parent_code = "import sys;from fucc import cli;cli.assert_locale=lambda _:None;sys.exit(cli.run_command([sys.executable,'-c',sys.argv[1]],'us'))"
    process = subprocess.Popen([sys.executable, "-c", parent_code, child_code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "READY"
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=5)
        assert process.returncode == 42
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
