"""User-scoped Claude, VS Code, session, and desktop-entry adapters."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import FuccError
from .files import read_snapshot, replace_snapshot
from .jsonc import MISSING, edit, parse_document
from .regions import get_target_region
from .transaction import Resource

KEYS = ("TZ", "LANG", "LC_ALL")
VSCODE_ENV = "claudeCode.environmentVariables"


def _conflict(message: str) -> FuccError:
    return FuccError(message, code="CONFIG_CONFLICT", exit_code=24)


def _property(value: dict, key: str) -> dict:
    return {"present": True, "value": value[key]} if key in value else {"present": False}


def _settings(snapshot: dict, path: str, jsonc: bool) -> dict:
    if snapshot["text"] is None or not snapshot["text"].strip():
        return {}
    try:
        tree = parse_document(snapshot["text"], comments=jsonc, trailing=jsonc)
    except (ValueError, RecursionError) as error:
        raise _conflict(f"配置必须是有效{' JSONC' if jsonc else ' JSON'}对象：{path}") from error
    if tree.kind != "object":
        raise _conflict(f"配置必须是有效{' JSONC' if jsonc else ' JSON'}对象：{path}")
    containers = tree.properties(VSCODE_ENV if jsonc else "env")
    if len(containers) > 1:
        raise _conflict(f"受管配置包含重复字段：{path}")
    if containers:
        container = containers[0].children[0]
        if not jsonc:
            if any(len(container.properties(key)) > 1 for key in KEYS):
                raise _conflict(f"受管环境包含重复字段：{path}")
        else:
            for row in container.children:
                if len(row.properties("name")) > 1 or len(row.properties("value")) > 1:
                    raise _conflict(f"环境条目包含重复字段：{path}")
    return tree.value


def _require_environment(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != set(KEYS):
        raise TypeError("Invalid managed environment")
    for key in KEYS:
        item = value[key]
        if not isinstance(item, dict) or type(item.get("present")) is not bool or set(item) != ({"present", "value"} if item["present"] else {"present"}):
            raise TypeError("Invalid managed environment property")
    return value


def claude_resource(path: str) -> Resource:
    def environment(root: dict) -> dict:
        if "env" in root and not isinstance(root["env"], dict):
            raise _conflict(f"Claude env 必须是对象：{path}")
        return root.get("env", {})

    def read() -> dict:
        env = environment(_settings(read_snapshot(path), path, False))
        return {key: _property(env, key) for key in KEYS}

    def write(value: Any) -> None:
        target = _require_environment(value)
        before = read_snapshot(path)
        environment(_settings(before, path, False))
        text = before["text"] if before["text"] and before["text"].strip() else "{}\n"
        for key in KEYS:
            text = edit(text, ["env", key], target[key]["value"] if target[key]["present"] else MISSING)
        updated = _settings({"text": text}, path, False)
        if isinstance(updated.get("env"), dict) and not updated["env"]:
            text = edit(text, ["env"])
        replace_snapshot(path, {"text": text, "mode": before["mode"] if before["mode"] is not None else 0o600}, before)

    return Resource("claude-settings", path, read, write)


def _vscode_entries(root: dict, path: str) -> list:
    if VSCODE_ENV not in root:
        return []
    if not isinstance(root[VSCODE_ENV], list):
        raise _conflict(f"VS Code {VSCODE_ENV} 必须是数组：{path}")
    return root[VSCODE_ENV]


def _is_managed_entry(value: Any, key: str) -> bool:
    return isinstance(value, dict) and value.get("name") == key


def vscode_resource(path: str) -> Resource:
    def read() -> dict:
        entries = _vscode_entries(_settings(read_snapshot(path), path, True), path)
        return {key: [_property(entry, "value") for entry in entries if _is_managed_entry(entry, key)] for key in KEYS}

    def write(value: Any) -> None:
        if not isinstance(value, dict) or set(value) != set(KEYS):
            raise TypeError("Invalid VS Code managed environment")
        for key in KEYS:
            if not isinstance(value[key], list):
                raise TypeError("Invalid VS Code environment entries")
            for item in value[key]:
                if not isinstance(item, dict) or type(item.get("present")) is not bool or (item["present"] and "value" not in item):
                    raise TypeError("Invalid VS Code environment value")
        before = read_snapshot(path)
        _vscode_entries(_settings(before, path, True), path)
        text = before["text"] if before["text"] and before["text"].strip() else "{}\n"

        def matching(key: str) -> list[int]:
            entries = _vscode_entries(_settings({"text": text}, path, True), path)
            return [index for index, entry in enumerate(entries) if _is_managed_entry(entry, key)]

        for key in KEYS:
            targets = value[key]
            matches = matching(key)
            for index in reversed(matches[len(targets):]):
                text = edit(text, [VSCODE_ENV, index])
            matches = matching(key)
            for index, target in enumerate(targets):
                if index < len(matches):
                    text = edit(text, [VSCODE_ENV, matches[index], "value"], target["value"] if target["present"] else MISSING)
                else:
                    entry = {"name": key}
                    if target["present"]:
                        entry["value"] = target["value"]
                    text = edit(text, [VSCODE_ENV, -1], entry)
        if not _vscode_entries(_settings({"text": text}, path, True), path):
            text = edit(text, [VSCODE_ENV])
        replace_snapshot(path, {"text": text, "mode": before["mode"] if before["mode"] is not None else 0o600}, before)

    return Resource("vscode-settings", path, read, write)


def snapshot_resource(resource_id: str, path: str) -> Resource:
    def write(value: Any) -> None:
        if not isinstance(value, dict) or set(value) != {"text", "mode"}:
            raise TypeError("Invalid managed file snapshot")
        replace_snapshot(path, value, read_snapshot(path))
    return Resource(resource_id, path, lambda: read_snapshot(path), write)


def desktop_quote(value: str) -> str:
    if any(character in value for character in "\r\n\x00="):
        raise _conflict("桌面启动适配路径包含不支持的字符")
    value = value.replace("\\", "\\\\\\\\")
    value = re.sub(r'["`$]', lambda match: "\\\\" + match[0], value)
    return '"' + value.replace("%", "%%") + '"'


def wrap_desktop_entry(text: str, launcher: str) -> str:
    prefix = desktop_quote(launcher) + " "
    eol = "\r\n" if "\r\n" in text else "\n"
    section = ""
    main = main_exec = dbus = marker = False
    result = []
    for line in re.split(r"\r?\n", text):
        if re.fullmatch(r"\[[^\]]+\]", line):
            if section == "Desktop Entry":
                if not dbus:
                    result.append("DBusActivatable=false")
                if not marker:
                    result.append("X-FUCC-Managed=true")
            section = line[1:-1]
            if section == "Desktop Entry":
                main = True
        if (section == "Desktop Entry" or section.startswith("Desktop Action ")) and line.startswith("Exec="):
            if section == "Desktop Entry":
                main_exec = True
            command = line[5:]
            if not command.strip():
                raise _conflict("桌面入口的 Exec 为空")
            result.append("Exec=" + (command if command.startswith(prefix) else prefix + command))
        elif section == "Desktop Entry" and line.startswith("DBusActivatable="):
            if dbus:
                raise _conflict("桌面入口包含重复 DBusActivatable")
            dbus = True
            result.append("DBusActivatable=false")
        elif section == "Desktop Entry" and line.startswith("X-FUCC-Managed="):
            marker = True
            result.append("X-FUCC-Managed=true")
        else:
            result.append(line)
    if section == "Desktop Entry":
        if not dbus:
            result.append("DBusActivatable=false")
        if not marker:
            result.append("X-FUCC-Managed=true")
    if not main or not main_exec:
        raise _conflict("桌面入口缺少 Desktop Entry / Exec")
    joined = eol.join(result)
    return joined.removesuffix(eol) + eol


def _desktop_files(directory: str, relative: str = "") -> list[str]:
    files = []
    try:
        with os.scandir(os.path.join(directory, relative)) as entries:
            for entry in entries:
                path = os.path.join(relative, entry.name)
                if entry.is_dir(follow_symlinks=False):
                    files.extend(_desktop_files(directory, path))
                elif entry.name.endswith(".desktop"):
                    files.append(path)
    except FileNotFoundError:
        return []
    return sorted(files)


def _application_kind(identity: str, text: str) -> dict | None:
    name = re.search(r"^Name=(.*)$", text, re.M)
    command = re.search(r"^Exec=(.*)$", text, re.M)
    identity += "\n" + (name[1] if name else "") + "\n" + (command[1] if command else "")
    if re.search(r"google[.-]?chrome|com\.google\.Chrome|\bchromium\b", identity, re.I):
        return {"kind": "chrome", "name": "Chromium" if re.search("chromium", identity, re.I) else "Google Chrome"}
    if re.search(r"microsoft[.-]?edge|\bmsedge\b", identity, re.I):
        return {"kind": "edge", "name": "Microsoft Edge"}
    if re.search(r"\bfirefox\b|org\.mozilla\.firefox", identity, re.I):
        return {"kind": "firefox", "name": "Firefox"}
    if re.search(r"com\.microsoft\.VSCode|visual studio code|(?:^|[\s/])code(?:-url-handler|\.desktop|\s|$)", identity, re.I):
        return {"kind": "vscode", "name": "Visual Studio Code"}
    if re.search(r"\bclaude\b", identity, re.I):
        return {"kind": "claude", "name": "Claude Desktop"}
    return None


def desktop_resource_id(identity: str) -> str:
    return "desktop." + (identity if re.fullmatch(r"[a-zA-Z0-9._-]{1,115}", identity) else hashlib.sha256(identity.encode()).hexdigest())


def _home(environment: Mapping[str, str]) -> str:
    configured = environment.get("HOME", "")
    return configured if os.path.isabs(configured) else str(Path.home())


def _config_home(environment: Mapping[str, str]) -> str:
    configured = environment.get("XDG_CONFIG_HOME", "")
    return configured if os.path.isabs(configured) else os.path.join(_home(environment), ".config")


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


@dataclass
class UserAdapters:
    resources: list[Resource]
    applications: list[dict]
    paths: dict[str, str]
    desired_files: dict
    launcher: str

    def desired(self, region: str, timezone: str | None = None) -> dict:
        target = get_target_region(region, timezone)
        env = {"TZ": target["timezone"], "LANG": target["lang"], "LC_ALL": target["lcAll"]}
        return {
            "session-environment": {"text": "# FUCC managed environment\n" + "".join(f"{key}={env[key]}\n" for key in KEYS), "mode": 0o600},
            "app-launcher": {"text": self.launcher, "mode": 0o755},
            "claude-settings": {key: {"present": True, "value": env[key]} for key in KEYS},
            "vscode-settings": {key: [{"present": True, "value": env[key]}] for key in KEYS},
            **self.desired_files,
        }


def create_user_adapters(environment: Mapping[str, str] | None = None, application_dirs: list[str] | None = None, managed_desktop_files: list[dict] | None = None, *, enroll_firefox: bool = False) -> UserAdapters:
    environment = os.environ if environment is None else environment
    home = _home(environment)
    config_home = _config_home(environment)
    if environment.get("CLAUDE_CONFIG_DIR") and not os.path.isabs(environment["CLAUDE_CONFIG_DIR"]):
        raise _conflict("CLAUDE_CONFIG_DIR 必须是绝对路径")
    data_home = environment.get("XDG_DATA_HOME", "")
    if not os.path.isabs(data_home):
        data_home = os.path.join(home, ".local", "share")
    user_applications = os.path.join(data_home, "applications")
    data_dirs = [path for path in environment.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":") if os.path.isabs(path)]
    defaults = [os.path.join(directory, "applications") for directory in data_dirs] + ["/var/lib/snapd/desktop/applications", "/var/lib/flatpak/exports/share/applications", os.path.join(data_home, "flatpak", "exports", "share", "applications")]
    directories = list(dict.fromkeys([user_applications, *(defaults if application_dirs is None else application_dirs)]))
    paths = {
        "sessionFile": os.path.join(config_home, "environment.d", "60-fucc.conf"),
        "launcher": os.path.join(config_home, "fucc", "launch"),
        "claudeSettings": os.path.join(environment.get("CLAUDE_CONFIG_DIR", os.path.join(home, ".claude")), "settings.json"),
        "vscodeSettings": os.path.join(config_home, "Code", "User", "settings.json"),
    }
    resources = [snapshot_resource("session-environment", paths["sessionFile"]), snapshot_resource("app-launcher", paths["launcher"]), claude_resource(paths["claudeSettings"]), vscode_resource(paths["vscodeSettings"])]
    applications = []
    desired_files = {}
    bound = managed_desktop_files
    if bound is not None:
        bound_ids, bound_paths = set(), set()
        for entry in bound:
            identity, resource_id = entry["identity"], entry["id"]
            if not os.path.isabs(identity) or os.path.dirname(identity) != user_applications or not os.path.basename(identity).endswith(".desktop") or resource_id != desktop_resource_id(os.path.basename(identity)) or resource_id in bound_ids or identity in bound_paths:
                raise _conflict("已保存桌面入口绑定不属于当前用户应用目录")
            bound_ids.add(resource_id)
            bound_paths.add(identity)
            current = read_snapshot(identity)
            resources.append(snapshot_resource(resource_id, identity))
            desired_files[resource_id] = dict(current) if current["text"] is None else {"text": wrap_desktop_entry(current["text"], paths["launcher"]), "mode": 0o644}
    seen = set()
    for directory in directories:
        for relative in _desktop_files(directory):
            identity = relative.replace("/", "-")
            if identity in seen:
                continue
            seen.add(identity)
            source = os.path.join(directory, relative)
            try:
                snapshot = read_snapshot(source)
            except (OSError, FuccError):
                kind = _application_kind(identity, "")
                if kind:
                    applications.append({"id": identity, **kind, "desktopFile": source, "supported": False, "reason": "桌面入口不可安全读取（链接或权限限制）"})
                continue
            if snapshot["text"] is None:
                continue
            kind = _application_kind(identity, snapshot["text"])
            if not kind:
                continue
            application = {"id": identity, **kind, "desktopFile": source, "supported": False}
            sandboxed = re.search(r"(?:^|[\s/])(?:flatpak|snap)(?:\s|/)|^X-(?:Flatpak|SnapInstanceName)=|/snap/", snapshot["text"], re.M) or re.search(r"(?:flatpak|snapd)/", str(directory))
            if sandboxed:
                snap_firefox = kind["kind"] == "firefox" and not re.search(r"\bflatpak\b|^X-Flatpak=", snapshot["text"], re.M) and "flatpak/" not in str(directory)
                applications.append({**application, "reason": "Snap Firefox 使用机器策略；沙箱入口不注入环境变量" if snap_firefox else "Snap / Flatpak 沙箱入口尚不支持环境继承"})
                continue
            if re.search(r"^Hidden=true\s*$", snapshot["text"], re.M):
                applications.append({**application, "reason": "桌面入口已隐藏"})
                continue
            destination = os.path.join(user_applications, identity)
            try:
                contents = wrap_desktop_entry(snapshot["text"], paths["launcher"])
                read_snapshot(destination)
                resource_id = desktop_resource_id(identity)
                enrolled = bound is None or any(entry["id"] == resource_id for entry in bound)
                if bound is None or (enroll_firefox and kind["kind"] == "firefox" and not enrolled):
                    resources.append(snapshot_resource(resource_id, destination))
                    desired_files[resource_id] = {"text": contents, "mode": 0o644}
                    enrolled = True
                reason = "运行 fucc on 接入 Firefox" if kind["kind"] == "firefox" else "开启期间发现新入口；请先关闭再开启 FUCC 以接入"
                applications.append({**application, "supported": enrolled, **({} if enrolled else {"reason": reason})})
            except (OSError, FuccError):
                applications.append({**application, "reason": "桌面入口格式或用户覆盖路径不支持安全适配"})
    for entry in bound or []:
        identity = os.path.basename(entry["identity"])
        if not any(application["id"] == identity for application in applications):
            snapshot = read_snapshot(entry["identity"])
            kind = _application_kind(identity, snapshot["text"] or "") or {"kind": "application", "name": identity}
            applications.append({"id": identity, **kind, "desktopFile": entry["identity"], "supported": snapshot["text"] is not None, "reason": "已保存受管入口缺失；需要恢复配置冲突" if snapshot["text"] is None else "使用已保存的用户桌面入口"})
    for kind, name in (("chrome", "Chrome / Chromium"), ("firefox", "Firefox"), ("vscode", "Visual Studio Code"), ("claude", "Claude Desktop")):
        if not any(application["kind"] == kind for application in applications):
            applications.append({"id": kind, "kind": kind, "name": name, "desktopFile": None, "supported": False, "reason": "未发现本机桌面入口"})
    env_path = os.path.join(config_home, "fucc", "env.sh")
    launcher = f'#!/bin/sh\n# FUCC application environment launcher\nif [ -r {_shell_quote(env_path)} ]; then\n  . {_shell_quote(env_path)}\nfi\nexec "$@"\n'
    return UserAdapters(resources, applications, paths, desired_files, launcher)
