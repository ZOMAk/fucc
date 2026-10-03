"""Native user settings, Claude Desktop launchers, and legacy resource restoration."""
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
    verified: dict | None = None

    def read() -> dict:
        nonlocal verified
        verified = read_snapshot(path)
        return dict(verified)

    def write(value: Any) -> None:
        nonlocal verified
        if not isinstance(value, dict) or set(value) != {"text", "mode"}:
            raise TypeError("Invalid managed file snapshot")
        # Keep the snapshot that the transaction checked; a fresh read here
        # would accept an external edit made between validation and replacement.
        before = read() if verified is None else verified
        updated = dict(value)
        replace_snapshot(path, updated, before)
        verified = updated
    return Resource(resource_id, path, read, write)


def desktop_quote(value: str) -> str:
    if any(character in value for character in "\r\n\x00="):
        raise _conflict("桌面启动适配路径包含不支持的字符")
    value = value.replace("\\", "\\\\\\\\")
    value = re.sub(r'["`$]', lambda match: "\\\\" + match[0], value)
    return '"' + value.replace("%", "%%") + '"'


def wrap_desktop_entry(text: str, launcher: str, *, identity: str = "", kind: str | None = None) -> str:
    prefix = desktop_quote(launcher) + " "
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = re.split(r"\r?\n", text)
    main_lines = []
    section = ""
    for line in lines:
        if re.fullmatch(r"\[[^\]]+\]", line):
            section = line[1:-1]
        elif section == "Desktop Entry":
            main_lines.append(line)
    application = _application_kind(identity, "\n".join(main_lines)) if kind is None else None
    claude = os.path.basename(identity) == "com.anthropic.Claude.desktop" or kind == "claude" or (application is not None and application["kind"] == "claude")
    generated_key = re.compile(r"^(\s*X-Claude-Generated\s*=\s*)true(\s*)$")
    generated = claude and any(generated_key.fullmatch(line) for line in main_lines)
    if generated:
        # Claude Desktop checks the entire file with a literal substring guard.
        # Disable its ownership keys in every section, including duplicates.
        # Comments can retain their meaning without that exact substring; do
        # not rewrite command arguments, URLs, or arbitrary metadata values.
        lines = [generated_key.sub(r"\g<1>false\g<2>", line) if not line.lstrip().startswith("#") else line.replace("X-Claude-Generated=true", "X-Claude-Generated =true") for line in lines]
    section = ""
    main = main_exec = dbus = marker = False
    result = []
    for line in lines:
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
    if claude and "X-Claude-Generated=true" in joined:
        raise _conflict("Claude 桌面入口在命令或元数据中包含生成标记，无法安全阻止客户端重生成")
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
        chromium = bool(re.search("chromium", identity, re.I))
        browser_id = "chromium-browser" if re.search(r"\bchromium-browser\b", identity, re.I) else "chromium" if chromium else "chrome"
        return {"kind": "chrome", "browserId": browser_id, "name": "Chromium" if chromium else "Google Chrome"}
    if re.search(r"microsoft[.-]?edge|\bmsedge\b", identity, re.I):
        return {"kind": "edge", "browserId": "edge", "name": "Microsoft Edge"}
    if re.search(r"\bfirefox\b|org\.mozilla\.firefox", identity, re.I):
        return {"kind": "firefox", "browserId": "firefox", "name": "Firefox"}
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
    retired_ids: set[str]

    def desired(self, region: str, timezone: str | None = None, retired_originals: Mapping[str, dict] | None = None) -> dict:
        target = get_target_region(region, timezone)
        env = {"TZ": target["timezone"], "LANG": target["lang"], "LC_ALL": target["lcAll"]}
        desktop_paths = {resource.id: resource.identity for resource in self.resources if resource.id in self.desired_files}
        desktop_files = {
            resource_id: dict(snapshot) if snapshot["text"] is None else {"text": wrap_desktop_entry(snapshot["text"], self.paths["launcher"], identity=os.path.basename(desktop_paths[resource_id])), "mode": 0o644}
            for resource_id, snapshot in self.desired_files.items()
        }
        desired = {
            "claude-settings": {key: {"present": True, "value": env[key]} for key in KEYS},
            "vscode-settings": {key: [{"present": True, "value": env[key]}] for key in KEYS},
            **desktop_files,
        }
        if any(resource.id == "app-launcher" for resource in self.resources) and "app-launcher" not in self.retired_ids:
            desired["app-launcher"] = {"text": self.launcher, "mode": 0o755}
        for resource_id in self.retired_ids:
            original = (retired_originals or {}).get(resource_id)
            if not isinstance(original, dict) or set(original) != {"text", "mode"}:
                raise _conflict("退休资源缺少已提交的原始快照")
            # Never infer a retirement baseline from the current, possibly
            # managed file. Only the transaction's original can restore it.
            desired[resource_id] = dict(original)
        if set(desired) != {resource.id for resource in self.resources}:
            raise _conflict("用户资源目标与已保存绑定不一致")
        return desired


def create_user_adapters(environment: Mapping[str, str] | None = None, application_dirs: list[str] | None = None, managed_desktop_files: list[dict] | None = None, *, managed_resources: list[dict] | None = None, enroll_firefox: bool = False) -> UserAdapters:
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
    resources = [claude_resource(paths["claudeSettings"]), vscode_resource(paths["vscodeSettings"])]
    applications = []
    desired_files = {}
    retired_ids = set()
    bound = None if managed_resources is None and managed_desktop_files is None else []
    saved_ids, saved_paths = {}, set()
    fixed = {"session-environment": paths["sessionFile"], "app-launcher": paths["launcher"],
             "claude-settings": paths["claudeSettings"], "vscode-settings": paths["vscodeSettings"]}
    for entries in (managed_resources, managed_desktop_files):
        group_ids, group_paths = set(), set()
        for entry in entries or []:
            if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                    or not isinstance(entry.get("identity"), str)):
                raise _conflict("已保存用户资源绑定无效")
            identity, resource_id = entry["identity"], entry["id"]
            if resource_id in group_ids or identity in group_paths:
                raise _conflict("已保存用户资源绑定重复")
            group_ids.add(resource_id)
            group_paths.add(identity)
            if not resource_id.startswith("desktop.") and resource_id not in fixed:
                continue  # Bash and machine resources belong to other adapters.
            if resource_id in saved_ids:
                if saved_ids[resource_id] != identity:
                    raise _conflict("已保存用户资源绑定冲突")
                continue  # The two compatible arguments can describe the same binding.
            if identity in saved_paths:
                raise _conflict("已保存用户资源路径重复")
            saved_ids[resource_id] = identity
            saved_paths.add(identity)
            if resource_id in fixed:
                if identity != fixed[resource_id]:
                    raise _conflict("已保存用户资源绑定不属于当前用户配置路径")
                if resource_id in ("session-environment", "app-launcher"):
                    resources.append(snapshot_resource(resource_id, identity))
                    retired_ids.add(resource_id)
                continue
            if not os.path.isabs(identity) or os.path.dirname(identity) != user_applications or not os.path.basename(identity).endswith(".desktop") or resource_id != desktop_resource_id(os.path.basename(identity)):
                raise _conflict("已保存桌面入口绑定不属于当前用户应用目录")
            bound.append({"id": resource_id, "identity": identity})
            current = read_snapshot(identity)
            resources.append(snapshot_resource(resource_id, identity))
            # Recovery must construct old bindings even when their current
            # contents cannot safely be used to generate a new launcher.
            kind = _application_kind(os.path.basename(identity), current["text"] or "")
            if kind and kind["kind"] == "claude":
                desired_files[resource_id] = dict(current)
            else:
                retired_ids.add(resource_id)
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
            flatpak = re.search(r"\bflatpak\b|^X-Flatpak=", snapshot["text"], re.M) or "flatpak/" in str(directory)
            application["installation"] = "flatpak" if flatpak else "snap" if sandboxed else "native"
            if kind["kind"] != "claude":
                adaptation = "native-settings" if kind["kind"] == "vscode" else "system-policy"
                hidden = re.search(r"^Hidden=true\s*$", snapshot["text"], re.M)
                supported = not hidden and not flatpak and not (sandboxed and kind["kind"] == "vscode")
                reason = ("桌面入口已隐藏" if hidden else "Flatpak 配置路径尚未适配" if flatpak else
                          "沙箱 VS Code 的用户配置路径尚未适配" if sandboxed and kind["kind"] == "vscode" else
                          "使用 Claude Code 原生环境设置；不改写 VS Code 桌面入口" if kind["kind"] == "vscode" else
                          "使用机器浏览器策略及系统时区；实际覆盖以机器策略状态为准")
                applications.append({**application, "supported": bool(supported), "adaptation": adaptation, "reason": reason})
                continue
            if sandboxed:
                applications.append({**application, "adaptation": "launcher", "reason": "Snap / Flatpak 沙箱入口尚不支持环境继承"})
                continue
            if re.search(r"^Hidden=true\s*$", snapshot["text"], re.M):
                applications.append({**application, "adaptation": "launcher", "reason": "桌面入口已隐藏"})
                continue
            destination = os.path.join(user_applications, identity)
            resource_id = desktop_resource_id(identity)
            enrolled = bound is not None and any(entry["id"] == resource_id for entry in bound)
            if enrolled:
                applications.append({**application, "supported": True, "adaptation": "launcher"})
                continue
            try:
                contents = wrap_desktop_entry(snapshot["text"], paths["launcher"], identity=identity, kind=kind["kind"])
                read_snapshot(destination)
                if bound is None:
                    resources.append(snapshot_resource(resource_id, destination))
                    desired_files[resource_id] = {"text": contents, "mode": 0o644}
                    enrolled = True
                reason = "开启期间发现新入口；请先关闭再开启 FUCC 以接入"
                applications.append({**application, "supported": enrolled, "adaptation": "launcher", **({} if enrolled else {"reason": reason})})
            except (OSError, FuccError):
                applications.append({**application, "adaptation": "launcher", "reason": "桌面入口格式或用户覆盖路径不支持安全适配"})
    for entry in bound or []:
        identity = os.path.basename(entry["identity"])
        if not any(application["id"] == identity for application in applications):
            snapshot = read_snapshot(entry["identity"])
            kind = _application_kind(identity, snapshot["text"] or "") or {"kind": "application", "name": identity}
            applications.append({"id": identity, **kind, "desktopFile": entry["identity"], "supported": snapshot["text"] is not None,
                                 "adaptation": "launcher" if entry["id"] in desired_files else "native-settings" if kind["kind"] == "vscode" else "system-policy",
                                 "reason": "已保存受管入口缺失；需要恢复配置冲突" if snapshot["text"] is None else "保留已保存桌面入口绑定以恢复原始配置"})
    for kind, name in (("chrome", "Chrome / Chromium"), ("firefox", "Firefox"), ("vscode", "Visual Studio Code"), ("claude", "Claude Desktop")):
        if not any(application["kind"] == kind for application in applications):
            applications.append({"id": kind, "kind": kind, "name": name, "desktopFile": None, "supported": False, "reason": "未发现本机桌面入口"})
    env_path = os.path.join(config_home, "fucc", "env.sh")
    launcher = f'#!/bin/sh\n# FUCC application environment launcher\nif [ -r {_shell_quote(env_path)} ]; then\n  . {_shell_quote(env_path)}\nfi\nexec "$@"\n'
    if desired_files:
        if "app-launcher" not in saved_ids:
            resources.append(snapshot_resource("app-launcher", paths["launcher"]))
        retired_ids.discard("app-launcher")
    return UserAdapters(resources, applications, paths, desired_files, launcher, retired_ids)
