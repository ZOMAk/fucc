import json
import os
import subprocess

import pytest

from fucc.apps import create_user_adapters, desktop_quote, desktop_resource_id, snapshot_resource, wrap_desktop_entry
from fucc.errors import FuccError
from fucc.files import read_snapshot
from fucc.jsonc import edit, parse


@pytest.fixture
def context(tmp_path):
    home = tmp_path / "home folder'quoted"
    home.mkdir()
    applications = tmp_path / "applications"
    applications.mkdir()
    environment = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local" / "share")}
    return home, applications, environment


def put(path, text):
    from pathlib import Path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def adapters(context, **kwargs):
    _, applications, environment = context
    return create_user_adapters(environment=environment, application_dirs=[str(applications)], **kwargs)


def resource(adapters, identity):
    return next(item for item in adapters.resources if item.id == identity)


def test_claude_projection_restore_preserves_later_user_changes(context):
    home, _, _ = context
    path = put(home / ".claude" / "settings.json", '{"env":{"TZ":null,"KEEP":"original"},"model":"example","precise":9007199254740993}\n')
    app = adapters(context)
    target = resource(app, "claude-settings")
    baseline = target.read()
    target.write(app.desired("jp")[target.id])
    text = path.read_text()
    assert '"precise":9007199254740993' in text
    text = edit(text, ["env", "KEEP"], "changed")
    text = edit(text, ["env", "NEW_USER_VALUE"], "added")
    text = edit(text, ["custom"], True)
    path.write_text(text)
    assert target.read() == app.desired("jp")[target.id]
    target.write(baseline)
    assert json.loads(path.read_text()) == {"env": {"TZ": None, "KEEP": "changed", "NEW_USER_VALUE": "added"}, "model": "example", "precise": 9007199254740993, "custom": True}
    assert target.read() == baseline


def test_vscode_jsonc_retains_comments_extra_properties_and_user_rows(context):
    app = adapters(context)
    path = put(app.paths["vscodeSettings"], '''{
  // editor comment
  "editor.fontSize": 1.50e1,
  "claudeCode.environmentVariables": [
    // keep custom environment
    { "name": "KEEP", "value": "hello" },
    { "name": "TZ", "value": "old-timezone", "custom": true },
  ],
}
''')
    target = resource(app, "vscode-settings")
    baseline = target.read()
    target.write(app.desired("us")[target.id])
    text = path.read_text()
    text = edit(text, ["claudeCode.environmentVariables", -1], {"name": "NEW_USER_VALUE", "value": "added"})
    path.write_text(text)
    assert target.read() == app.desired("us")[target.id]
    target.write(baseline)
    text = path.read_text()
    assert '// editor comment' in text and '// keep custom environment' in text
    assert '"editor.fontSize": 1.50e1' in text
    assert parse(text) == {"editor.fontSize": 15, "claudeCode.environmentVariables": [{"name": "KEEP", "value": "hello"}, {"name": "TZ", "value": "old-timezone", "custom": True}, {"name": "NEW_USER_VALUE", "value": "added"}]}
    assert target.read() == baseline


def test_vscode_restores_duplicate_managed_rows_and_missing_values(context):
    app = adapters(context)
    path = put(app.paths["vscodeSettings"], '{"claudeCode.environmentVariables":[{"name":"TZ","value":null,"custom":1},{"name":"KEEP","value":true},{"name":"TZ","value":42},{"name":"LANG"}]}')
    target = resource(app, "vscode-settings")
    baseline = target.read()
    assert baseline == {"TZ": [{"present": True, "value": None}, {"present": True, "value": 42}], "LANG": [{"present": False}], "LC_ALL": []}
    target.write(app.desired("sg")[target.id])
    target.write(baseline)
    assert target.read() == baseline
    rows = parse(path.read_text())["claudeCode.environmentVariables"]
    assert next(row for row in rows if row["name"] == "TZ")["custom"] == 1
    assert {"name": "KEEP", "value": True} in rows


@pytest.mark.parametrize("identity", ["claude-settings", "vscode-settings"])
def test_creates_settings_and_retains_additions_on_restore(context, identity):
    app = adapters(context)
    target = resource(app, identity)
    baseline = target.read()
    target.write(app.desired("sg")[identity])
    put(target.identity, edit(open(target.identity).read(), ["keepMe"], True))
    target.write(baseline)
    assert parse(open(target.identity).read()) == {"keepMe": True}
    assert target.read() == baseline


def test_managed_row_comments_survive_restore(context):
    app = adapters(context)
    path = put(app.paths["vscodeSettings"], '{"claudeCode.environmentVariables":[\n// original custom env\n{"name":"KEEP","value":"x"}]}')
    target = resource(app, "vscode-settings")
    baseline = target.read()
    target.write(app.desired("us")[target.id])
    path.write_text(path.read_text().replace('"name": "LANG"', '// user locale comment\n"name": "LANG"'))
    target.write(baseline)
    text = path.read_text()
    assert '// original custom env' in text and '// user locale comment' in text
    assert parse(text) == {"claudeCode.environmentVariables": [{"name": "KEEP", "value": "x"}]}


@pytest.mark.parametrize("identity,text", [
    ("claude-settings", '{"env":"wrong"}'),
    ("claude-settings", '{"env":null}'),
    ("claude-settings", '{"env":{"TZ":"one","TZ":"two"}}'),
    ("claude-settings", '{"env":{},"env":{}}'),
    ("claude-settings", '{/* comment */"env":{}}'),
    ("claude-settings", '{"env":{},}'),
    ("vscode-settings", '{"claudeCode.environmentVariables":null}'),
    ("vscode-settings", '{"claudeCode.environmentVariables":[],"claudeCode.environmentVariables":[]}'),
    ("vscode-settings", '{"claudeCode.environmentVariables":[{"name":"TZ","value":1,"value":2}]}'),
    ("vscode-settings", '{"claudeCode.environmentVariables":[{"name":"TZ","name":"KEEP"}]}'),
    ("vscode-settings", '[1,2]'),
])
def test_invalid_managed_settings_refused_without_replacement(context, identity, text):
    app = adapters(context)
    target = resource(app, identity)
    path = put(target.identity, text)
    for call in (target.read, lambda: target.write(app.desired("us")[identity])):
        with pytest.raises(FuccError) as failure:
            call()
        assert failure.value.code == "CONFIG_CONFLICT"
    assert path.read_text() == text


def test_custom_config_and_xdg_paths(context):
    home, applications, environment = context
    custom = str(home / "custom-claude")
    app = create_user_adapters({**environment, "CLAUDE_CONFIG_DIR": custom}, [str(applications)])
    assert app.paths["claudeSettings"] == os.path.join(custom, "settings.json")
    fallback = create_user_adapters({"HOME": str(home), "XDG_CONFIG_HOME": "relative", "XDG_DATA_HOME": "relative"}, [])
    assert fallback.paths["launcher"] == str(home / ".config" / "fucc" / "launch")
    with pytest.raises(FuccError):
        create_user_adapters({**environment, "CLAUDE_CONFIG_DIR": "relative"}, [])


def test_wraps_actions_and_url_handlers_and_restores_absent_overrides(context):
    _, applications, _ = context
    original = '[Desktop Entry]\nName=Google Chrome\nExec=/opt/google/chrome/google-chrome %U\nIcon=google-chrome\nMimeType=x-scheme-handler/https;\nDBusActivatable=true\nActions=new-window;\n[Desktop Action new-window]\nName=New Window\nExec=/opt/google/chrome/google-chrome --new-window %U\n'
    source = put(applications / "google-chrome.desktop", original)
    put(applications / "com.microsoft.VSCodeURLHandler.desktop", '[Desktop Entry]\nName=Visual Studio Code URL Handler\nNoDisplay=true\nExec=/usr/share/code/code --open-url %U\n')
    app = adapters(context)
    assert len([item for item in app.applications if item["supported"]]) == 2
    for target in [item for item in app.resources if item.id.startswith("desktop.")]:
        baseline = target.read()
        target.write(app.desired("us")[target.id])
        override = open(target.identity).read()
        assert f'Exec="{app.paths["launcher"]}" ' in override
        assert '%U' in override
        assert 'DBusActivatable=false' in override
        assert wrap_desktop_entry(override, app.paths["launcher"]) == override
        target.write(baseline)
        assert read_snapshot(target.identity)["text"] is None
    assert source.read_text() == original


def test_existing_user_desktop_override_takes_precedence(context):
    home, applications, _ = context
    put(applications / "google-chrome.desktop", '[Desktop Entry]\nName=Chrome\nExec=/opt/google/chrome/google-chrome %U\n')
    original = '[Desktop Entry]\nName=My Chrome\nExec=/opt/google/chrome/google-chrome --custom %U\nIcon=my-chrome\n'
    path = put(home / ".local" / "share" / "applications" / "google-chrome.desktop", original)
    app = adapters(context)
    target = resource(app, "desktop.google-chrome.desktop")
    baseline = target.read()
    target.write(app.desired("us")[target.id])
    assert '--custom %U' in path.read_text()
    target.write(baseline)
    assert path.read_text() == original


def test_saved_bindings_survive_uninstall_and_defer_new_apps(context):
    _, applications, _ = context
    source = put(applications / "google-chrome.desktop", '[Desktop Entry]\nName=Google Chrome\nExec=/opt/google/chrome/google-chrome %U\n')
    initial = adapters(context)
    desktop = resource(initial, "desktop.google-chrome.desktop")
    baseline = desktop.read()
    desktop.write(initial.desired("us")[desktop.id])
    source.unlink()
    put(applications / "com.microsoft.VSCode.desktop", '[Desktop Entry]\nName=Visual Studio Code\nExec=/usr/share/code/code %F\n')
    bound = adapters(context, managed_desktop_files=[{"id": desktop.id, "identity": desktop.identity}])
    assert [item.id for item in bound.resources if item.id.startswith("desktop.")] == [desktop.id]
    assert next(item for item in bound.applications if item["kind"] == "vscode")["supported"] is False
    assert bound.desired("jp")[desktop.id] == initial.desired("us")[desktop.id]
    resource(bound, desktop.id).write(baseline)
    assert read_snapshot(desktop.identity)["text"] is None


def test_rejects_invalid_saved_bindings(context):
    home, _, _ = context
    for entries in (
        [{"id": "desktop.google-chrome.desktop", "identity": str(home / "google-chrome.desktop")}],
        [{"id": "desktop.wrong.desktop", "identity": str(home / ".local" / "share" / "applications" / "google-chrome.desktop")}],
        [{"id": "desktop.chrome.desktop", "identity": str(home / ".local" / "share" / "applications" / "chrome.desktop")}] * 2,
    ):
        with pytest.raises(FuccError):
            adapters(context, managed_desktop_files=entries)


def test_sandbox_hidden_and_symlink_entries_report_unsupported(context):
    _, applications, _ = context
    put(applications / "chromium.desktop", '[Desktop Entry]\nName=Chromium\nExec=/snap/bin/chromium %U\n')
    put(applications / "com.microsoft.VSCode.desktop", '[Desktop Entry]\nName=Visual Studio Code\nExec=/usr/bin/flatpak run com.microsoft.VSCode %U\n')
    put(applications / "claude.desktop", '[Desktop Entry]\nName=Claude\nHidden=true\nExec=/usr/bin/claude\n')
    (applications / "google-chrome.desktop").symlink_to(applications / "claude.desktop")
    app = adapters(context)
    assert len([item for item in app.applications if 'Snap / Flatpak' in item.get("reason", "")]) == 2
    assert not any(item["supported"] for item in app.applications)
    assert not any(item.id.startswith("desktop.") for item in app.resources)


def test_firefox_native_is_enrolled_without_replacing_other_saved_bindings(context):
    _, applications, _ = context
    put(applications / "firefox.desktop", '[Desktop Entry]\nName=Firefox\nExec=/usr/lib/firefox/firefox %u\nDBusActivatable=true\n')
    put(applications / "com.microsoft.VSCode.desktop", '[Desktop Entry]\nName=Visual Studio Code\nExec=/usr/share/code/code %F\n')
    frozen = adapters(context, managed_desktop_files=[])
    assert not any(item.id.startswith("desktop.") for item in frozen.resources)
    candidate = adapters(context, managed_desktop_files=[], enroll_firefox=True)
    assert [item.id for item in candidate.resources if item.id.startswith("desktop.")] == ["desktop.firefox.desktop"]
    value = candidate.desired("us")["desktop.firefox.desktop"]["text"]
    assert 'DBusActivatable=false' in value and '%u' in value
    assert next(app for app in candidate.applications if app["kind"] == "firefox")["supported"] is True
    assert next(app for app in candidate.applications if app["kind"] == "vscode")["supported"] is False


def test_firefox_snap_and_flatpak_do_not_receive_native_launcher(context):
    _, applications, _ = context
    put(applications / "firefox_firefox.desktop", '[Desktop Entry]\nName=Firefox\nExec=/snap/bin/firefox %u\nX-SnapInstanceName=firefox\n')
    put(applications / "org.mozilla.firefox.desktop", '[Desktop Entry]\nName=Firefox\nExec=/usr/bin/flatpak run org.mozilla.firefox %u\nX-Flatpak=org.mozilla.firefox\n')
    app = adapters(context, managed_desktop_files=[], enroll_firefox=True)
    assert not any(item.id.startswith("desktop.") for item in app.resources)
    firefox = [item for item in app.applications if item["kind"] == "firefox"]
    assert len(firefox) == 2 and not any(item["supported"] for item in firefox)
    assert any("机器策略" in item["reason"] for item in firefox)
    assert any("Flatpak" in item["reason"] for item in firefox)


def test_launcher_runs_with_empty_path_and_quoted_home(context):
    home, _, _ = context
    app = adapters(context)
    put(home / ".config" / "fucc" / "env.sh", "export TZ='Asia/Tokyo'\nexport LANG='ja_JP.UTF-8'\nexport LC_ALL='ja_JP.UTF-8'\n")
    target = resource(app, "app-launcher")
    target.write(app.desired("jp")[target.id])
    output = subprocess.run([target.identity, "/bin/sh", "-c", 'printf "%s|%s|%s|%s" "$TZ" "$LANG" "$LC_ALL" "$1"', "test", "argument with spaces"], env={"PATH": "", "HOME": str(home)}, capture_output=True, text=True, check=True).stdout
    assert output == "Asia/Tokyo|ja_JP.UTF-8|ja_JP.UTF-8|argument with spaces"
    assert os.stat(target.identity).st_mode & 0o777 == 0o755


def test_desktop_escaping_crlf_and_resource_ids():
    quoted = desktop_quote('/folder\\with"`$%/launch')
    assert quoted == '"/folder\\\\\\\\with\\\\"\\\\`\\\\$%%/launch"'
    original = '[Desktop Entry]\r\nName=Chrome\r\nExec=chrome %U\r\n'
    wrapped = wrap_desktop_entry(original, '/launcher')
    assert '\r\n' in wrapped
    assert wrap_desktop_entry(wrapped, '/launcher') == wrapped
    assert desktop_resource_id('google-chrome.desktop') == 'desktop.google-chrome.desktop'
    assert len(desktop_resource_id('中文.desktop')) == len('desktop.') + 64
    for path in ('/line\nbreak', '/equals=value', '/null\x00'):
        with pytest.raises(FuccError):
            desktop_quote(path)


@pytest.mark.parametrize("identity,kind", [("com.anthropic.Claude.desktop", None), ("custom.desktop", "claude"), ("", None)])
def test_generated_claude_entry_keeps_commands_actions_and_metadata(identity, kind):
    original = '''[Desktop Entry]
Name=Claude Desktop
Exec=claude-desktop --existing-argument "argument with spaces" %U
TryExec=claude-desktop
Icon=claude-desktop
MimeType=x-scheme-handler/claude;
Actions=NewChat;NewCode;ContinueLast;
X-Claude-Generated=true
Comment=Original localized metadata
[Desktop Action NewChat]
Name=New chat
Exec=claude-desktop --new-chat %U
[Desktop Action NewCode]
Name=New code
Exec=claude-desktop --new-code
[Desktop Action ContinueLast]
Name=Continue last
Exec=claude-desktop --continue-last
'''
    actual = wrap_desktop_entry(original, "/fucc/launch", identity=identity, kind=kind)
    expected = original.replace("\nExec=", '\nExec="/fucc/launch" ').replace("X-Claude-Generated=true", "X-Claude-Generated=false").replace("[Desktop Action NewChat]", "DBusActivatable=false\nX-FUCC-Managed=true\n[Desktop Action NewChat]")
    assert actual == expected
    assert "X-Claude-Generated=true" not in actual
    assert wrap_desktop_entry(actual, "/fucc/launch", identity=identity, kind=kind) == actual


def test_generated_claude_neutralizes_keys_across_sections_and_comment_tokens():
    original = '''# X-Claude-Generated=true documents ownership
[Desktop Entry]
Name=Claude Desktop
Exec=claude-desktop %U
X-Claude-Generated=true
X-Claude-Generated=true
  X-Claude-Generated = true
[Desktop Action NewChat]
Name=New chat
Exec=claude-desktop --new-chat
X-Claude-Generated=true
 # old X-Claude-Generated=true marker
[Other metadata]
X-Claude-Generated=true
'''
    actual = wrap_desktop_entry(original, "/fucc/launch")
    assert "X-Claude-Generated=true" not in actual
    assert actual.count("X-Claude-Generated=false") == 4
    assert "  X-Claude-Generated = false" in actual
    assert "# X-Claude-Generated =true documents ownership" in actual
    assert " # old X-Claude-Generated =true marker" in actual
    assert wrap_desktop_entry(actual, "/fucc/launch") == actual


@pytest.mark.parametrize("extra", [
    'Exec=claude-desktop --custom "X-Claude-Generated=true" %U\n',
    'URL=https://example.invalid/?X-Claude-Generated=true\n',
    'Comment=Metadata containing X-Claude-Generated=true\n',
    '[Desktop Action Custom]\nName=Custom\nExec=claude-desktop --custom X-Claude-Generated=true\n',
])
def test_claude_generator_literal_in_commands_or_metadata_is_refused(extra):
    original = '[Desktop Entry]\nName=Claude Desktop\nExec=claude-desktop\nX-Claude-Generated=true\n' + extra
    with pytest.raises(FuccError) as failure:
        wrap_desktop_entry(original, "/fucc/launch")
    assert failure.value.code == "CONFIG_CONFLICT"
    assert extra not in str(failure.value)


def test_non_claude_entry_and_custom_claude_ownership_are_unchanged():
    original = '[Desktop Entry]\nName=Other application\nExec=other-app\nX-Claude-Generated=true\n[Desktop Action Custom]\nName=Claude\nExec=other-app --custom\n'
    actual = wrap_desktop_entry(original, "/fucc/launch", identity="other.desktop")
    assert "X-Claude-Generated=true" in actual
    assert "X-Claude-Generated=false" not in actual
    custom = '[Desktop Entry]\nName=Claude Desktop\nExec=claude-desktop --custom %U\n'
    assert "X-Claude-Generated=" not in wrap_desktop_entry(custom, "/fucc/launch")


def test_claude_false_main_with_residual_guard_is_refused():
    original = '[Desktop Entry]\nName=Claude Desktop\nExec=claude-desktop\nX-Claude-Generated=false\n[Desktop Action Custom]\nName=Custom\nExec=claude-desktop --custom\nX-Claude-Generated=true\n'
    with pytest.raises(FuccError) as failure:
        wrap_desktop_entry(original, "/fucc/launch")
    assert failure.value.code == "CONFIG_CONFLICT"


def test_generated_claude_crlf_and_explicit_identity_are_idempotent():
    original = '[Desktop Entry]\r\nName=Localized desktop name\r\nExec=/opt/custom-launcher %U\r\nX-Claude-Generated=true\r\n'
    wrapped = wrap_desktop_entry(original, "/fucc/launch", identity="com.anthropic.Claude.desktop")
    assert "X-Claude-Generated=false\r\n" in wrapped
    assert "\n" not in wrapped.replace("\r\n", "")
    assert wrap_desktop_entry(wrapped, "/fucc/launch", identity="com.anthropic.Claude.desktop") == wrapped


def test_claude_generated_user_entry_restores_original_marker_and_permissions(context):
    home, applications, _ = context
    identity = "com.anthropic.Claude.desktop"
    system_text = '[Desktop Entry]\nName=Claude Desktop\nExec=claude-desktop\nX-Claude-Generated=true\n'
    source = put(applications / identity, system_text)
    original = '[Desktop Entry]\nName=Claude Desktop\nExec=claude-desktop --custom %U\nX-Claude-Generated=true\nActions=NewChat;\n[Desktop Action NewChat]\nName=New chat\nExec=claude-desktop --new-chat %U\n'
    path = put(home / ".local" / "share" / "applications" / identity, original)
    path.chmod(0o600)
    app = adapters(context)
    target = resource(app, "desktop." + identity)
    baseline = target.read()
    assert baseline == {"text": original, "mode": 0o600}
    desired = app.desired("us")[target.id]
    target.write(desired)
    assert "X-Claude-Generated=false" in path.read_text()
    assert "X-Claude-Generated=true" not in path.read_text()
    assert read_snapshot(path)["mode"] == 0o644
    assert source.read_text() == system_text
    bound = adapters(context, managed_desktop_files=[{"id": target.id, "identity": target.identity}])
    assert bound.desired("us")[target.id] == desired
    target.write(baseline)
    assert read_snapshot(path) == baseline
    assert source.read_text() == system_text


def test_claude_unknown_literal_is_not_enrolled_or_overwritten(context):
    home, applications, _ = context
    identity = "com.anthropic.Claude.desktop"
    original = '[Desktop Entry]\nName=Claude Desktop\nExec=claude-desktop --custom X-Claude-Generated=true\nX-Claude-Generated=true\n'
    source = put(applications / identity, original)
    override = put(home / ".local" / "share" / "applications" / identity, original)
    override.chmod(0o600)
    app = adapters(context)
    assert not any(item.id == "desktop." + identity for item in app.resources)
    assert next(item for item in app.applications if item["kind"] == "claude")["supported"] is False
    assert source.read_text() == original
    assert read_snapshot(override) == {"text": original, "mode": 0o600}
    bound = adapters(context, managed_desktop_files=[{"id": "desktop." + identity, "identity": str(override)}])
    target = resource(bound, "desktop." + identity)
    assert target.read() == {"text": original, "mode": 0o600}
    with pytest.raises(FuccError) as failure:
        bound.desired("us")
    assert failure.value.code == "CONFIG_CONFLICT"
    assert read_snapshot(override) == {"text": original, "mode": 0o600}
    target.write({"text": None, "mode": None})
    assert not override.exists()
    assert source.read_text() == original


@pytest.mark.parametrize("change", ["text", "mode", "appeared"])
def test_snapshot_write_rejects_edits_after_transaction_read(tmp_path, change):
    path = tmp_path / "managed.desktop"
    if change != "appeared":
        put(path, "original\n").chmod(0o600)
    target = snapshot_resource("desktop.test.desktop", str(path))
    baseline = target.read()
    if change == "text":
        path.write_text("external edit\n")
    elif change == "mode":
        path.chmod(0o644)
    else:
        put(path, "external file\n").chmod(0o600)
    external = read_snapshot(path)
    assert external != baseline
    with pytest.raises(FuccError) as failure:
        target.write({"text": "managed\n", "mode": 0o644})
    assert failure.value.code == "CONFIG_CONFLICT"
    assert read_snapshot(path) == external


def test_snapshot_direct_write_and_restore_use_own_successful_snapshot(tmp_path):
    path = tmp_path / "managed.desktop"
    target = snapshot_resource("desktop.test.desktop", str(path))
    first = {"text": "first managed\n", "mode": 0o644}
    target.write(first)
    first["text"] = "caller mutation must not change cached expected\n"
    desired = {"text": "second managed\n", "mode": 0o600}
    target.write(desired)
    baseline = target.read()
    baseline["text"] = "mutating read result must not change cached expected\n"
    target.write({"text": None, "mode": None})
    assert not path.exists()
