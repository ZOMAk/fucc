"""Cancellation stays a useful CLI result without exposing interrupted values."""
import json

import pytest

from fucc import cli


PRIVATE = "password=private-cancellation-value"


class InterruptedRuntime:
    def target_region(self, _region):
        return "us"

    def saved_timezone(self, _region):
        return None

    def on(self, *_arguments):
        raise KeyboardInterrupt(PRIVATE)

    def off(self):
        raise KeyboardInterrupt(PRIVATE)

    def recover(self):
        raise KeyboardInterrupt(PRIVATE)

    def status(self):
        pytest.fail("An interrupted mutation must not emit a successful status")


@pytest.fixture
def interrupted(monkeypatch):
    monkeypatch.setattr(cli, "LocalRuntime", InterruptedRuntime)
    monkeypatch.setattr(cli, "fetch_ip_intelligence", lambda: None)


def assert_cancellation_message(message):
    assert "取消" in message
    assert "off" in message and "recover" in message
    assert "保留" in message and ("日志" in message or "状态" in message)
    assert PRIVATE not in message
    assert "traceback" not in message.lower()
    assert "所有配置已回滚" not in message
    assert "已验证回退" not in message


@pytest.mark.parametrize("command", ["on", "off", "recover"])
def test_cancelled_mutation_has_stable_json_and_exit_130(interrupted, capsys, command):
    assert cli.main([command, "--json"]) == 130
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["schemaVersion"] == 1 and result["ok"] is False
    assert set(result) == {"schemaVersion", "ok", "error"}
    assert result["error"]["id"] == "CANCELLED" and result["error"]["code"] == 130
    assert_cancellation_message(result["error"]["message"])
    assert PRIVATE not in captured.err and "traceback" not in captured.err.lower()


@pytest.mark.parametrize("command", ["on", "off", "recover"])
def test_cancelled_mutation_explains_next_step_in_plain_output(interrupted, capsys, command):
    assert cli.main([command]) == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert_cancellation_message(captured.err)


def test_privileged_helper_cancellation_preserves_json_protocol(monkeypatch, capsys):
    def interrupt(_arguments):
        raise KeyboardInterrupt(PRIVATE)

    monkeypatch.setattr(cli, "_machine", interrupt)
    assert cli.main(["__machine", "off", "--owner", "1000"]) == 130
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["error"]["id"] == "CANCELLED" and result["error"]["code"] == 130
    assert_cancellation_message(result["error"]["message"])
