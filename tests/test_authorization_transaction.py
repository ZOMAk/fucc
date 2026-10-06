"""Authorization and repeated off use isolated homes and never execute sudo."""
from copy import deepcopy
import json

import pytest

from fucc.errors import FuccError
from fucc.runtime import LocalRuntime
from fucc.transaction import LocalTransaction, Resource
from test_runtime import FakeMachine


DESKTOP = "com.anthropic.Claude.desktop"
ORIGINAL_DESKTOP = ("[Desktop Entry]\nName=Claude Desktop\nType=Application\n"
                    "Exec=/opt/claude/claude %U\nDBusActivatable=true\nX-Claude-Generated=true\n")


class AuthorizedMachine(FakeMachine):
    def __init__(self):
        super().__init__()
        self.authorizations = 0
        self.authorization_error = None
        self.during_authorization = None

    def authorize(self):
        self.authorizations += 1
        if self.during_authorization is not None:
            self.during_authorization()
        if self.authorization_error is not None:
            raise self.authorization_error


def files(home):
    def lock_metadata(path):
        parts = path.relative_to(home).parts
        return parts[:3] == (".local", "state", "fucc") and "locks" in parts[3:-1]

    return {str(path.relative_to(home)): (path.read_bytes(), path.stat().st_mode & 0o777)
            for path in home.rglob("*") if path.is_file()
            and not lock_metadata(path)}


def document(runtime):
    return json.loads((runtime.root / "local-transaction.json").read_text())["payload"]


def resources(runtime):
    return {resource.id: deepcopy(resource.read()) for resource in runtime.transaction.resources}


def assert_refused(operation):
    """Both a caller-facing authentication error and a recovery result abort."""
    try:
        result = operation()
    except FuccError:
        return
    assert result == {"kind": "recovery_required"}


@pytest.fixture
def context(tmp_path, monkeypatch):
    # Also intercept the actual process API so accidentally invoking a helper
    # fails the test, even when run from a developer's authenticated terminal.
    monkeypatch.setattr("fucc.machine.subprocess.run", lambda *args, **kwargs: pytest.fail("No process or sudo permitted"))
    monkeypatch.setattr("fucc.bash.BashRuntime._system_timezone", staticmethod(lambda: "Asia/Tokyo"))
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text("# Original startup\nexport KEEP=original\n")
    desktop = home / ".local/share/applications" / DESKTOP
    desktop.parent.mkdir(parents=True)
    desktop.write_text(ORIGINAL_DESKTOP)
    desktop.chmod(0o600)
    claude = home / ".claude/settings.json"
    claude.parent.mkdir()
    claude.write_text('{"env":{"TZ":"Asia/Tokyo","KEEP":"preserved"},"theme":"dark"}\n')
    vscode = home / ".config/Code/User/settings.json"
    vscode.parent.mkdir(parents=True)
    vscode.write_text('{"editor.fontSize":14,"claudeCode.environmentVariables":[{"name":"TZ","value":"Asia/Tokyo"}]}\n')
    applications = tmp_path / "applications"
    applications.mkdir()
    environment = {"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/bash",
                   "XDG_DATA_DIRS": str(tmp_path / "empty-data")}
    machine = AuthorizedMachine()
    locales = lambda: ["en_US.utf8", "en_GB.utf8", "ja_JP.utf8", "en_SG.utf8"]
    factory = lambda: LocalRuntime(environment, [str(applications)], machine, locales)
    return {"factory": factory, "home": home, "machine": machine, "claude": claude,
            "vscode": vscode, "desktop": desktop}


def enable(context):
    runtime = context["factory"]()
    assert runtime.on("us") == {"kind": "committed"}
    assert runtime.status()["health"] == "healthy"
    assert context["machine"].authorizations == 1
    context["machine"].authorizations = 0
    return runtime


def assert_disabled(context, runtime):
    assert runtime.status()["health"] == "healthy" and runtime.status()["active"] is False
    assert context["machine"].current == {"active": False, "region": None, "ownerUid": None}
    assert context["desktop"].read_text() == ORIGINAL_DESKTOP
    assert context["desktop"].stat().st_mode & 0o777 == 0o600
    assert (context["home"] / ".bashrc").read_text() == "# Original startup\nexport KEEP=original\n"
    assert json.loads(context["claude"].read_text()) == {"env": {"TZ": "Asia/Tokyo", "KEEP": "preserved"}, "theme": "dark"}
    assert json.loads(context["vscode"].read_text())["claudeCode.environmentVariables"] == [{"name": "TZ", "value": "Asia/Tokyo"}]
    assert not (context["home"] / ".config/fucc/launch").exists()
    assert not (context["home"] / ".config/fucc/env.sh").exists()
    assert document(runtime)["journal"] is None and document(runtime)["committed"]["resources"] == []


@pytest.mark.parametrize("operation", ["initial_on", "off", "switch"])
def test_cancelled_authentication_does_not_change_any_configuration_or_state(context, operation):
    runtime = context["factory"]() if operation == "initial_on" else enable(context)
    before = files(context["home"])
    machine_before = deepcopy(context["machine"].current)
    writes_before = deepcopy(context["machine"].writes)
    context["machine"].authorization_error = KeyboardInterrupt("mock cancelled password input")
    with pytest.raises(KeyboardInterrupt):
        runtime.off() if operation == "off" else runtime.on("eu" if operation == "switch" else "us")
    assert context["machine"].authorizations == 1
    assert files(context["home"]) == before
    assert context["machine"].current == machine_before and context["machine"].writes == writes_before
    if operation == "initial_on":
        assert not (runtime.root / "local-transaction.json").exists()
        assert not (runtime.root / "local-transaction.initialized").exists()
    else:
        assert document(runtime)["journal"] is None
        assert runtime.status()["health"] == "healthy"


@pytest.mark.parametrize("operation", ["initial_on", "off"])
def test_refused_authentication_does_not_publish_an_empty_state_or_journal(context, operation):
    runtime = context["factory"]() if operation == "initial_on" else enable(context)
    before = files(context["home"])
    machine_before = deepcopy(context["machine"].current)
    writes_before = deepcopy(context["machine"].writes)
    context["machine"].authorization_error = FuccError("mock authentication refused", "MACHINE_AUTH_FAILED", 30)
    assert_refused(runtime.off if operation == "off" else lambda: runtime.on("us"))
    assert context["machine"].authorizations == 1
    assert files(context["home"]) == before
    assert context["machine"].current == machine_before and context["machine"].writes == writes_before


def test_authentication_runs_before_user_resource_writes_and_before_journal(context):
    runtime = context["factory"]()
    before = files(context["home"])
    context["machine"].during_authorization = lambda: (
        pytest.fail("A configuration changed before authentication") if files(context["home"]) != before else None)
    assert runtime.on("us") == {"kind": "committed"}
    assert context["machine"].authorizations == 1
    context["machine"].authorizations = 0
    before = files(context["home"])
    assert runtime.off() == {"kind": "restored"}
    assert context["machine"].authorizations == 1
    assert_disabled(context, runtime)


def test_on_noop_and_off_inactive_never_authenticate(context):
    runtime = enable(context)
    context["machine"].authorization_error = AssertionError("Noop must not request authentication")
    before = files(context["home"])
    assert runtime.on("us") == {"kind": "noop"}
    assert files(context["home"]) == before and context["machine"].authorizations == 0
    context["machine"].authorization_error = None
    assert runtime.off() == {"kind": "restored"}
    context["machine"].authorizations = 0
    context["machine"].authorization_error = AssertionError("Inactive off must not authenticate")
    before = files(context["home"])
    assert context["factory"]().off() == {"kind": "noop"}
    assert files(context["home"]) == before and context["machine"].authorizations == 0


@pytest.mark.parametrize("machine_active", [False, True])
def test_user_only_on_switch_and_off_never_authenticate_or_change_machine(context, machine_active):
    machine = context["machine"]
    if machine_active:
        machine.current = machine.desired("jp")
    initial_machine = deepcopy(machine.current)
    machine.authorization_error = AssertionError("User-only must not request authentication")
    runtime = context["factory"]()
    assert runtime.on("us", user_only=True) == {"kind": "committed"}
    assert runtime.on("eu", user_only=True) == {"kind": "committed"}
    assert runtime.off() == {"kind": "restored"}
    assert machine.authorizations == 0 and machine.writes == [] and machine.current == initial_machine


def mutate_claude(context):
    value = json.loads(context["claude"].read_text())
    value["env"]["TZ"] = "Europe/Paris"
    context["claude"].write_text(json.dumps(value) + "\n")


def test_unknown_live_value_is_rejected_before_authentication_or_any_write(context):
    runtime = enable(context)
    mutate_claude(context)
    before = files(context["home"])
    writes_before = deepcopy(context["machine"].writes)
    assert runtime.off() == {"kind": "recovery_required"}
    assert files(context["home"]) == before and context["machine"].writes == writes_before
    assert context["machine"].authorizations == 0 and document(runtime)["journal"] is None


@pytest.mark.parametrize("operation", ["on", "off"])
def test_external_write_during_successful_authentication_is_rechecked_before_journal(context, operation):
    runtime = context["factory"]() if operation == "on" else enable(context)
    before = files(context["home"])
    writes_before = deepcopy(context["machine"].writes)
    context["machine"].during_authorization = lambda: mutate_claude(context)
    assert_refused(runtime.off if operation == "off" else lambda: runtime.on("us"))
    expected = dict(before)
    relative = str(context["claude"].relative_to(context["home"]))
    expected[relative] = (context["claude"].read_bytes(), context["claude"].stat().st_mode & 0o777)
    assert files(context["home"]) == expected
    assert context["machine"].authorizations == 1 and context["machine"].writes == writes_before
    if operation == "on":
        assert not (runtime.root / "local-transaction.json").exists()
    else:
        assert document(runtime)["journal"] is None


def test_external_write_during_cancelled_authentication_is_preserved(context):
    runtime = enable(context)
    before = files(context["home"])
    context["machine"].during_authorization = lambda: mutate_claude(context)
    context["machine"].authorization_error = KeyboardInterrupt("mock cancel with concurrent edit")
    with pytest.raises(KeyboardInterrupt):
        runtime.off()
    relative = str(context["claude"].relative_to(context["home"]))
    before[relative] = (context["claude"].read_bytes(), context["claude"].stat().st_mode & 0o777)
    assert files(context["home"]) == before and document(runtime)["journal"] is None


def interrupt_old_restore(context, runtime, *, after_machine_write=False):
    """Model the actual predecessor, which first authenticates in machine.write."""
    resource = context["machine"].resource
    writer = resource.write

    def interrupted(value):
        if after_machine_write:
            writer(value)
        raise KeyboardInterrupt("mock interrupted old off")

    resource.write = interrupted
    old = LocalTransaction(runtime.root, runtime.transaction.resources, before_recover=runtime._recover_nested)
    try:
        with pytest.raises(KeyboardInterrupt):
            old.restore()
    finally:
        resource.write = writer
    assert document(runtime)["journal"]["kind"] == "restore"


def test_repeated_off_safely_resumes_the_predecessor_restore_journal(context):
    runtime = enable(context)
    interrupt_old_restore(context, runtime)
    assert context["machine"].current["active"] is True
    resumed = context["factory"]()
    result = resumed.off()
    assert result["kind"] in ("restored", "recovered")
    assert context["machine"].authorizations == 1
    assert_disabled(context, resumed)


@pytest.mark.parametrize("operation", ["off", "recover"])
def test_pending_restore_with_machine_already_restored_does_not_authenticate(context, operation):
    runtime = enable(context)
    interrupt_old_restore(context, runtime, after_machine_write=True)
    assert context["machine"].current["active"] is False
    context["machine"].authorization_error = AssertionError("Machine already restored; no authentication needed")
    resumed = context["factory"]()
    result = getattr(resumed, operation)()
    assert result["kind"] in ("restored", "recovered")
    assert context["machine"].authorizations == 0
    assert_disabled(context, resumed)


def test_repeated_off_never_consumes_an_apply_journal(context):
    runtime = enable(context)
    resource = context["machine"].resource
    writer = resource.write
    desired = runtime.user.desired("eu")
    desired.update({"bash-profile": {"region": "eu"}, "machine": context["machine"].desired("eu")})
    old = LocalTransaction(runtime.root, runtime.transaction.resources, before_recover=runtime._recover_nested)
    resource.write = lambda _: (_ for _ in ()).throw(KeyboardInterrupt("mock old apply interruption"))
    try:
        with pytest.raises(KeyboardInterrupt):
            old.apply("eu", desired)
    finally:
        resource.write = writer
    assert document(runtime)["journal"]["kind"] == "apply"
    before = files(context["home"])
    writes_before = deepcopy(context["machine"].writes)
    resumed = context["factory"]()
    assert resumed.off() == {"kind": "recovery_required"}
    assert files(context["home"]) == before and context["machine"].writes == writes_before
    assert context["machine"].authorizations == 0 and document(runtime)["journal"]["kind"] == "apply"


def test_pending_restore_unknown_external_edit_is_not_overwritten_or_authenticated(context):
    runtime = enable(context)
    interrupt_old_restore(context, runtime)
    mutate_claude(context)
    before = files(context["home"])
    writes_before = deepcopy(context["machine"].writes)
    resumed = context["factory"]()
    assert resumed.off() == {"kind": "recovery_required"}
    assert files(context["home"]) == before and context["machine"].writes == writes_before
    assert context["machine"].authorizations == 0 and document(runtime)["journal"]["kind"] == "restore"


def test_noninteractive_helper_failure_preserves_restore_journal_for_another_off(context):
    runtime = enable(context)
    context["machine"].fail = True
    assert runtime.off() == {"kind": "recovery_required"}
    assert context["machine"].authorizations == 1 and document(runtime)["journal"]["kind"] == "restore"
    context["machine"].fail = False
    resumed = context["factory"]()
    assert resumed.off()["kind"] in ("restored", "recovered")
    assert context["machine"].authorizations == 2
    assert_disabled(context, resumed)


def test_generic_preflight_runs_inside_lock_and_does_not_expose_mutable_plan(tmp_path):
    values = {"a": "original-a", "machine": "original-machine"}
    entries_seen = []
    resources_list = [Resource(resource_id, str(tmp_path / resource_id),
                               lambda resource_id=resource_id: values[resource_id],
                               lambda value, resource_id=resource_id: values.__setitem__(resource_id, value))
                      for resource_id in values]
    root = tmp_path / "state"
    holder = {}

    def prepare(entries):
        entries_seen.append(deepcopy(entries))
        with pytest.raises(FuccError) as busy:
            holder["engine"].apply("jp", {"a": "jp-a", "machine": "jp-machine"})
        assert busy.value.code == "LOCK_BUSY"
        entries[0]["desired"] = "callback-mutated-plan"

    engine = holder["engine"] = LocalTransaction(root, resources_list, before_mutation=prepare)
    assert engine.apply("us", {"a": "us-a", "machine": "us-machine"}) == {"kind": "committed"}
    assert values == {"a": "us-a", "machine": "us-machine"}
    assert {entry["id"] for entry in entries_seen[0]} == {"a", "machine"}
    assert entries_seen[0][0]["before"] == "original-a"


def test_generic_invalid_binding_prevents_preflight(tmp_path):
    values = {"a": "original"}
    original = Resource("a", str(tmp_path / "original-path"), lambda: values["a"], lambda value: values.__setitem__("a", value))
    root = tmp_path / "state"
    assert LocalTransaction(root, [original]).apply("us", {"a": "managed"}) == {"kind": "committed"}
    changed = Resource("a", str(tmp_path / "different-path"), original.read, original.write)
    engine = LocalTransaction(root, [changed], before_mutation=lambda _: pytest.fail("Invalid binding must not authenticate"))
    state = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    assert engine.restore(resume=True) == {"kind": "recovery_required"}
    assert engine.recover() == {"kind": "recovery_required"}
    assert values == {"a": "managed"}
    assert {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()} == state


@pytest.mark.parametrize("cancel", [False, True])
def test_apply_recovery_authenticates_machine_compensation_before_any_user_write(context, cancel):
    runtime = enable(context)
    resource = context["machine"].resource
    writer = resource.write
    desired = runtime.user.desired("eu")
    desired.update({"bash-profile": {"region": "eu"}, "machine": context["machine"].desired("eu")})
    old = LocalTransaction(runtime.root, runtime.transaction.resources, before_recover=runtime._recover_nested)

    def interrupt(value):
        writer(value)
        raise KeyboardInterrupt("mock interrupted after privileged apply")

    resource.write = interrupt
    try:
        with pytest.raises(KeyboardInterrupt):
            old.apply("eu", desired)
    finally:
        resource.write = writer
    assert context["machine"].current["region"] == "eu"
    before = files(context["home"])
    context["machine"].during_authorization = lambda: (
        pytest.fail("Apply compensation wrote before authentication") if files(context["home"]) != before else None)
    resumed = context["factory"]()
    if cancel:
        context["machine"].authorization_error = KeyboardInterrupt("mock cancelled recovery authentication")
        with pytest.raises(KeyboardInterrupt):
            resumed.recover()
        assert files(context["home"]) == before and document(resumed)["journal"]["kind"] == "apply"
        assert context["machine"].authorizations == 1
        context["machine"].authorizations = 0
        context["machine"].authorization_error = None
    assert resumed.recover() == {"kind": "recovered"}
    assert context["machine"].authorizations == 1 and context["machine"].current["region"] == "us"
    assert document(resumed)["journal"] is None and resumed.status()["health"] == "healthy"


def test_authentication_rechecks_unchanged_resources_as_well_as_changed_resources(tmp_path):
    values = {"a": "unchanged", "machine": "original-machine"}
    resource_list = [Resource(resource_id, str(tmp_path / resource_id),
                              lambda resource_id=resource_id: values[resource_id],
                              lambda value, resource_id=resource_id: values.__setitem__(resource_id, value))
                     for resource_id in values]
    observations = []

    def authenticate(entries):
        observations.append(deepcopy(entries))
        values["a"] = "external-unknown-edit"

    engine = LocalTransaction(tmp_path / "state", resource_list, before_mutation=authenticate)
    assert_refused(lambda: engine.apply("us", {"a": "unchanged", "machine": "managed-machine"}))
    assert [entry["id"] for entry in observations[0]] == ["machine"]
    assert values == {"a": "external-unknown-edit", "machine": "original-machine"}
    assert not engine.path.exists() and not (engine.root / "local-transaction.initialized").exists()


def test_apply_failure_compensation_does_not_start_another_interactive_authorization(context):
    runtime = context["factory"]()
    context["machine"].fail = True
    assert runtime.on("us") == {"kind": "compensated"}
    assert context["machine"].authorizations == 1
    assert_disabled(context, runtime)
