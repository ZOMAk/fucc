"""All sudo invocations are mocked; no authorization or privileged writes occur."""
import errno
import json
import signal
import subprocess

import pytest

from fucc import machine
from fucc.errors import FuccError


ENVIRONMENT = {
    "PATH": "/private/bin",
    "TERM": "xterm-256color",
    "PYTHONPATH": "/untrusted/python",
    "PYTHONINSPECT": "1",
    "SUDO_ASKPASS": "/untrusted/askpass",
    "TOKEN": "private-helper-value",
}
SAFE_ENVIRONMENT = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "TERM": "xterm-256color"}
PYTHON = "/opt/conda/envs/fucc/bin/python"
ENTRYPOINT = "/opt/fucc/src/fucc/helper.py"


def completed(command, code=0, output=""):
    return subprocess.CompletedProcess(command, code, stdout=output)


def assert_preserved_terminal(kwargs):
    assert kwargs.get("stdin") is None
    assert kwargs.get("stderr") is None
    assert kwargs.get("capture_output", False) is False
    assert kwargs.get("start_new_session", False) is False
    assert kwargs.get("process_group") is None
    assert "input" not in kwargs


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(machine.os, "getuid", lambda: 1000)
    return machine.MachineAdapter(ENTRYPOINT, PYTHON, ENVIRONMENT)


def test_foreign_machine_owner_is_rejected_before_sudo(monkeypatch, adapter):
    monkeypatch.setattr(adapter, "inspect", lambda: {"ownerUid": 2000})
    monkeypatch.setattr(machine, "_authorize", lambda *_arguments: pytest.fail("Foreign state must not prompt for sudo"))
    with pytest.raises(FuccError) as error:
        adapter.authorize()
    assert error.value.code == "OWNER_MISMATCH"


@pytest.mark.parametrize("owner", [None, 1000])
def test_valid_machine_owner_is_inspected_before_authorization(monkeypatch, adapter, owner):
    calls = []

    def inspect():
        calls.append("inspect")
        return {"ownerUid": owner}

    def authorize(environment):
        assert environment == ENVIRONMENT
        calls.append("authorize")

    monkeypatch.setattr(adapter, "inspect", inspect)
    monkeypatch.setattr(machine, "_authorize", authorize)
    adapter.authorize()
    assert calls == ["inspect", "authorize"]


def test_invalid_public_state_can_authorize_private_helper_recovery(monkeypatch, adapter):
    calls = []

    def inspect():
        calls.append("inspect")
        raise FuccError("Public state requires private recovery", "MACHINE_STATE_INVALID", 10)

    def authorize(environment):
        assert environment == ENVIRONMENT
        calls.append("authorize")

    monkeypatch.setattr(adapter, "inspect", inspect)
    monkeypatch.setattr(machine, "_authorize", authorize)
    adapter.authorize()
    assert calls == ["inspect", "authorize"]


@pytest.mark.parametrize("code", ["MACHINE_PATH_INVALID", "MACHINE_CATALOG_INVALID", "MACHINE_COMMAND_FAILED",
                                  "OWNER_MISMATCH", "CONFIG_CONFLICT", "MACHINE_RECOVERY_REQUIRED"])
def test_other_inspection_errors_refuse_authorization(monkeypatch, adapter, code):
    failure = FuccError("Inspection did not establish an authorized owner", code, 10)

    def inspect():
        raise failure

    monkeypatch.setattr(adapter, "inspect", inspect)
    monkeypatch.setattr(machine, "_authorize", lambda *_arguments: pytest.fail("Inspection failure must not prompt for sudo"))
    with pytest.raises(FuccError) as error:
        adapter.authorize()
    assert error.value is failure


def test_authorization_test_seam_still_requires_owner_check(monkeypatch):
    calls = []
    monkeypatch.setattr(machine.os, "getuid", lambda: 1000)
    adapter = machine.MachineAdapter(ENTRYPOINT, PYTHON, ENVIRONMENT,
                                    test_options={"authorize": lambda: calls.append("authorize")})
    monkeypatch.setattr(machine, "_authorize", lambda *_arguments: pytest.fail("The test seam must not execute sudo"))
    monkeypatch.setattr(adapter, "inspect", lambda: {"ownerUid": 2000})
    with pytest.raises(FuccError) as error:
        adapter.authorize()
    assert error.value.code == "OWNER_MISMATCH" and calls == []
    monkeypatch.setattr(adapter, "inspect", lambda: {"ownerUid": 1000})
    adapter.authorize()
    assert calls == ["authorize"]


def test_authorization_refreshes_credentials_before_any_command_with_original_terminal(monkeypatch):
    calls = []

    def invoke(command, **kwargs):
        calls.append((command, kwargs))
        return completed(command)

    monkeypatch.setattr(machine.subprocess, "run", invoke)
    machine._authorize(ENVIRONMENT)
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == ["/usr/bin/sudo", "-v"]
    assert kwargs["env"] == SAFE_ENVIRONMENT
    assert_preserved_terminal(kwargs)


def test_authorization_does_not_add_term_when_terminal_type_was_absent(monkeypatch):
    calls = []

    def invoke(command, **kwargs):
        calls.append(kwargs)
        return completed(command)

    monkeypatch.setattr(machine.subprocess, "run", invoke)
    machine._authorize({"PATH": "/private/bin", "TOKEN": ENVIRONMENT["TOKEN"]})
    assert calls[0]["env"] == {"PATH": SAFE_ENVIRONMENT["PATH"]}
    assert_preserved_terminal(calls[0])


@pytest.mark.parametrize("code", [-signal.SIGINT, 130])
def test_authorization_sigint_is_cancellation_not_denial(monkeypatch, code):
    monkeypatch.setattr(machine.subprocess, "run", lambda command, **_: completed(command, code))
    with pytest.raises(KeyboardInterrupt):
        machine._authorize(ENVIRONMENT)


@pytest.mark.parametrize("code", [1, 2, 127, -signal.SIGTERM, 143])
def test_failed_authorization_is_not_mislabeled_as_ctrl_c(monkeypatch, code):
    monkeypatch.setattr(machine.subprocess, "run", lambda command, **_: completed(command, code, ENVIRONMENT["TOKEN"]))
    with pytest.raises(FuccError) as error:
        machine._authorize(ENVIRONMENT)
    assert error.value.code == "ADMINISTRATOR_REQUIRED" and error.value.exit_code == 10
    assert "认证" in str(error.value)
    assert ENVIRONMENT["TOKEN"] not in str(error.value)


def test_missing_sudo_has_actionable_authorization_error_without_os_details(monkeypatch):
    def unavailable(*_arguments, **_kwargs):
        raise OSError(ENVIRONMENT["TOKEN"])

    monkeypatch.setattr(machine.subprocess, "run", unavailable)
    with pytest.raises(FuccError) as error:
        machine._authorize(ENVIRONMENT)
    assert error.value.code == "ADMINISTRATOR_REQUIRED" and error.value.exit_code == 10
    assert ENVIRONMENT["TOKEN"] not in str(error.value)


def test_authorization_keyboard_interrupt_propagates(monkeypatch):
    def interrupt(*_arguments, **_kwargs):
        raise KeyboardInterrupt(ENVIRONMENT["TOKEN"])

    monkeypatch.setattr(machine.subprocess, "run", interrupt)
    with pytest.raises(KeyboardInterrupt):
        machine._authorize(ENVIRONMENT)


def test_actual_helper_cannot_prompt_after_mutation_begins(monkeypatch):
    calls = []
    payload = {"scope": "machine", "active": False, "region": None,
               "ownerUid": None, "recoveryRequired": False}

    def invoke(command, **kwargs):
        calls.append((command, kwargs))
        return completed(command, output=json.dumps(payload))

    monkeypatch.setattr(machine, "_run_helper", invoke)
    machine._invoke_helper(PYTHON, ENTRYPOINT, "off", 1000, ENVIRONMENT)
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == ["/usr/bin/sudo", "-n", "--", PYTHON, "-I", ENTRYPOINT,
                       "__machine", "off", "--owner", "1000"]
    assert kwargs["env"] == SAFE_ENVIRONMENT
    assert kwargs["stdout"] == subprocess.PIPE and kwargs["text"] is True
    assert_preserved_terminal(kwargs)


@pytest.mark.parametrize("code", [-signal.SIGINT, 130])
def test_helper_sigint_is_not_swallowed_as_internal_failure(monkeypatch, code):
    monkeypatch.setattr(machine, "_run_helper", lambda command, **_: completed(command, code, ENVIRONMENT["TOKEN"]))
    with pytest.raises(KeyboardInterrupt):
        machine._invoke_helper(PYTHON, ENTRYPOINT, "off", 1000, ENVIRONMENT)


def test_noninteractive_helper_failure_stays_distinct_from_user_cancellation(monkeypatch):
    monkeypatch.setattr(machine, "_run_helper", lambda command, **_: completed(command, 1, ENVIRONMENT["TOKEN"]))
    with pytest.raises(FuccError) as error:
        machine._invoke_helper(PYTHON, ENTRYPOINT, "off", 1000, ENVIRONMENT)
    assert error.value.code == "MACHINE_HELPER_FAILED"
    assert ENVIRONMENT["TOKEN"] not in str(error.value)


def test_helper_waiting_preserves_a_successful_result(monkeypatch):
    payload = '{"scope":"machine"}'

    class Child:
        returncode = 0

        def communicate(self):
            return payload, None

        def __enter__(self):
            return self

        def __exit__(self, *_arguments):
            return None

    monkeypatch.setattr(machine.subprocess, "Popen", lambda *_arguments, **_kwargs: Child())
    command = ["/usr/bin/sudo", "-n", "--", PYTHON, "-I", ENTRYPOINT, "__machine", "off", "--owner", "1000"]
    result = machine._run_helper(command, stdout=subprocess.PIPE, text=True, env=SAFE_ENVIRONMENT)
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.args == command and result.returncode == 0
    assert result.stdout == payload and result.stderr is None


@pytest.mark.parametrize("interruptions", [1, 2])
def test_helper_interrupt_waits_for_sudo_before_propagating_without_force_kill(monkeypatch, capsys, interruptions):
    events = []
    calls = []

    class Child:
        returncode = None

        def __init__(self):
            self.remaining = interruptions

        def communicate(self, *_arguments, **_kwargs):
            if self.remaining:
                self.remaining -= 1
                events.append("interrupted")
                raise KeyboardInterrupt(ENVIRONMENT["TOKEN"])
            events.append("finished")
            self.returncode = 130
            return ENVIRONMENT["TOKEN"], None

        def poll(self):
            return self.returncode

        def send_signal(self, received):
            assert received == signal.SIGINT
            events.append("signal")

        def kill(self):
            pytest.fail("SIGKILL of sudo could leave its privileged child running")

        def terminate(self):
            pytest.fail("Cancellation must relay SIGINT and wait for the helper")

        def __enter__(self):
            return self

        def __exit__(self, *_arguments):
            assert self.returncode == 130

    child = Child()

    def start(command, **kwargs):
        calls.append((command, kwargs))
        return child

    monkeypatch.setattr(machine.subprocess, "Popen", start)
    command = ["/usr/bin/sudo", "-n", "--", PYTHON, "-I", ENTRYPOINT,
               "__machine", "off", "--owner", "1000"]
    with pytest.raises(KeyboardInterrupt):
        machine._run_helper(command, stdout=subprocess.PIPE, text=True, env=SAFE_ENVIRONMENT)
    assert calls == [(command, {"stdout": subprocess.PIPE, "text": True, "env": SAFE_ENVIRONMENT})]
    assert events[0] == "interrupted" and events[-1] == "finished"
    assert "signal" in events and events.index("signal") < events.index("finished")
    assert child.returncode == 130
    captured = capsys.readouterr()
    assert ENVIRONMENT["TOKEN"] not in captured.out + captured.err
    assert "traceback" not in (captured.out + captured.err).lower()


@pytest.mark.parametrize("signal_fault", ["interrupted", "process_gone"])
def test_signal_interruption_or_process_exit_still_waits_for_communicate(monkeypatch, capsys, signal_fault):
    events = []

    class Child:
        returncode = None
        signal_attempts = 0
        communication_attempts = 0

        def communicate(self):
            self.communication_attempts += 1
            if self.communication_attempts == 1:
                events.append("communicate_interrupted")
                raise KeyboardInterrupt(ENVIRONMENT["TOKEN"])
            events.append("communicate_finished")
            self.returncode = 130
            return ENVIRONMENT["TOKEN"], None

        def send_signal(self, received):
            assert received == signal.SIGINT
            self.signal_attempts += 1
            if self.signal_attempts == 1:
                events.append("signal_" + signal_fault)
                if signal_fault == "interrupted":
                    raise KeyboardInterrupt(ENVIRONMENT["TOKEN"])
                raise ProcessLookupError(ENVIRONMENT["TOKEN"])
            events.append("signal_sent")

        def kill(self):
            pytest.fail("Repeated Ctrl+C must not force-kill sudo")

        def terminate(self):
            pytest.fail("Repeated Ctrl+C must wait for helper completion")

        def __enter__(self):
            return self

        def __exit__(self, *_arguments):
            assert self.returncode == 130 and self.communication_attempts == 2
            events.append("context_exited")

    child = Child()
    monkeypatch.setattr(machine.subprocess, "Popen", lambda *_arguments, **_kwargs: child)
    command = ["/usr/bin/sudo", "-n", "--", PYTHON, "-I", ENTRYPOINT,
               "__machine", "off", "--owner", "1000"]
    with pytest.raises(KeyboardInterrupt):
        machine._run_helper(command, stdout=subprocess.PIPE, text=True, env=SAFE_ENVIRONMENT)
    expected = ["communicate_interrupted", "signal_" + signal_fault]
    if signal_fault == "interrupted":
        expected.append("signal_sent")
    assert events == expected + ["communicate_finished", "context_exited"]
    assert child.signal_attempts == (2 if signal_fault == "interrupted" else 1)
    captured = capsys.readouterr()
    assert ENVIRONMENT["TOKEN"] not in captured.out + captured.err
    assert "traceback" not in (captured.out + captured.err).lower()


@pytest.mark.parametrize("error_number", [errno.EINTR, errno.EPERM])
def test_signal_oserror_retries_eintr_and_waits_before_reporting_permanent_error(monkeypatch, capsys, error_number):
    events = []
    failure = OSError(error_number, ENVIRONMENT["TOKEN"])
    interrupted_communications = 1 if error_number == errno.EINTR else 2

    class Child:
        returncode = None
        communication_attempts = 0
        signal_attempts = 0

        def communicate(self):
            self.communication_attempts += 1
            if self.communication_attempts <= interrupted_communications:
                events.append("communicate_interrupted")
                raise KeyboardInterrupt(ENVIRONMENT["TOKEN"])
            events.append("communicate_finished")
            self.returncode = 130
            return ENVIRONMENT["TOKEN"], None

        def send_signal(self, received):
            assert received == signal.SIGINT
            self.signal_attempts += 1
            if self.signal_attempts == 1:
                events.append("signal_error")
                raise failure
            events.append("signal_sent")

        def kill(self):
            pytest.fail("A failed SIGINT relay must not force-kill sudo")

        def terminate(self):
            pytest.fail("A failed SIGINT relay must still wait for helper completion")

        def __enter__(self):
            return self

        def __exit__(self, *_arguments):
            assert self.returncode == 130
            assert self.communication_attempts == interrupted_communications + 1
            events.append("context_exited")

    child = Child()
    monkeypatch.setattr(machine.subprocess, "Popen", lambda *_arguments, **_kwargs: child)
    command = ["/usr/bin/sudo", "-n", "--", PYTHON, "-I", ENTRYPOINT,
               "__machine", "off", "--owner", "1000"]
    exception_type = KeyboardInterrupt if error_number == errno.EINTR else OSError
    with pytest.raises(exception_type) as error:
        machine._run_helper(command, stdout=subprocess.PIPE, text=True, env=SAFE_ENVIRONMENT)
    if error_number == errno.EINTR:
        assert child.signal_attempts == 2
        assert events == ["communicate_interrupted", "signal_error", "signal_sent",
                          "communicate_finished", "context_exited"]
    else:
        assert error.value is failure
        assert child.signal_attempts == 1
        assert events == ["communicate_interrupted", "signal_error", "communicate_interrupted",
                          "communicate_finished", "context_exited"]
    captured = capsys.readouterr()
    assert ENVIRONMENT["TOKEN"] not in captured.out + captured.err
    assert "traceback" not in (captured.out + captured.err).lower()
