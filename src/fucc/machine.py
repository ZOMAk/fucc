"""Fixed, privileged machine resources and their unprivileged adapter.

Only the internal CLI helper calls ``run_machine_action`` with production paths.
The options argument exists for unit tests and is never exposed as CLI flags.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess

from .errors import FuccError
from .files import read_snapshot, replace_snapshot
from .regions import TARGET_REGIONS, get_target_region
from .state import MutationLock, canonical_json, is_safe_json
from .transaction import LocalTransaction, Resource, matches_resource_claim

MACHINE_ROOT = "/var/lib/fucc"
BROWSERS = [
    {"id": "chrome", "label": "Chrome", "kind": "not_installed", "directory": "/etc/opt/chrome/policies/managed"},
    {"id": "chromium", "label": "Chromium", "kind": "not_installed", "directory": "/etc/chromium/policies/managed"},
    {"id": "chromium-browser", "label": "Chromium (distro)", "kind": "not_installed", "directory": "/etc/chromium-browser/policies/managed"},
    {"id": "edge", "label": "Edge", "kind": "not_installed", "directory": "/etc/opt/edge/policies/managed"},
]
CANDIDATES = {
    "chrome": ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/opt/google/chrome/chrome"],
    "chromium": ["/usr/bin/chromium", "/usr/lib/chromium/chromium"],
    "chromium-browser": ["/usr/bin/chromium-browser", "/usr/lib/chromium-browser/chromium-browser"],
    "edge": ["/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable", "/opt/microsoft/msedge/msedge"],
}
FLATPAK_IDS = {"chrome": "com.google.Chrome", "chromium": "org.chromium.Chromium", "edge": "com.microsoft.Edge"}
LANGUAGES = {"us": ["en-US", "en"], "eu": ["en-GB", "en"], "jp": ["ja-JP", "ja"], "sg": ["en-SG", "en"]}
SYSTEM_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}
ZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+.-]*(?:/[A-Za-z0-9_+.-]+)*\Z")


def _fail(code, message):
    return FuccError(message, code=code)


def _uid(value):
    return type(value) is int and 0 <= value <= 9007199254740991


def _region(value):
    return type(value) is str and value in TARGET_REGIONS


def _valid_switch(value):
    if (type(value) is not dict or not {"active", "region", "ownerUid"}.issubset(value)
            or not set(value).issubset({"active", "region", "ownerUid", "timezone"})
            or type(value["active"]) is not bool):
        return False
    if not value["active"]:
        return value["region"] is None and value["ownerUid"] is None and "timezone" not in value
    return (_region(value["region"]) and _uid(value["ownerUid"])
            and ("timezone" not in value or _dynamic_zone(value["region"], value["timezone"])))


def _dynamic_zone(region, zone):
    try:
        return (type(zone) is str and get_target_region(region, zone)["timezone"] == zone
                and zone != TARGET_REGIONS[region]["timezone"])
    except FuccError:
        return False


def _blank():
    return {"schema": "fucc-machine-public-v1", "active": False, "region": None, "ownerUid": None,
            "recoveryRequired": False, "revision": 0, "transaction": "none", "managedBrowsers": []}


def _valid_public(value):
    if type(value) is not dict or set(value) not in (set(_blank()), set(_blank()) | {"timezone"}):
        return False
    dynamic = "timezone" in value
    return (value["schema"] == ("fucc-machine-public-v2" if dynamic else "fucc-machine-public-v1")
            and (not dynamic or value["active"] is True and _region(value["region"]) and _dynamic_zone(value["region"], value["timezone"]))
            and type(value["active"]) is bool
            and ((_region(value["region"]) and _uid(value["ownerUid"])) if value["active"] else
                 value["region"] is None and (value["ownerUid"] is None or _uid(value["ownerUid"])))
            and type(value["recoveryRequired"]) is bool and _uid(value["revision"])
            and value["transaction"] in ("none", "apply", "restore")
            and type(value["managedBrowsers"]) is list
            and all(type(x) is str and x in CANDIDATES for x in value["managedBrowsers"])
            and len(set(value["managedBrowsers"])) == len(value["managedBrowsers"]))


def _parse_snapshot(value):
    if not (type(value) is dict and set(value) == {"text", "mode"} and
            ((value["mode"] is None) if value["text"] is None else
             type(value["text"]) is str and type(value["mode"]) is int and 0 <= value["mode"] <= 0o777)):
        raise _fail("MACHINE_STATE_INVALID", "机器配置快照无效；需要恢复")
    return dict(value)


def _policy_object(text, label):
    def constant(_value):
        raise ValueError("Non-finite JSON policy value")

    def pairs(entries):
        result = {}
        managed_keys = {"ForcedLanguages", "WebRtcIPHandling", "DefinePreferredLanguages"}
        for key, value in entries:
            if key in result and key in managed_keys:
                raise ValueError("Duplicate managed JSON policy")
            result[key] = value
        return result

    try:
        value = json.loads(text, parse_constant=constant, object_pairs_hook=pairs)
        if not is_safe_json(value):
            raise ValueError("Invalid JSON policy value")
    except (ValueError, TypeError) as exc:
        raise _fail("CONFIG_CONFLICT", f"{label} 策略不是有效 JSON；请检查现有策略") from exc
    if type(value) is not dict:
        raise _fail("CONFIG_CONFLICT", f"{label} 策略必须是 JSON 对象")
    return value


def _command(command, args):
    try:
        result = subprocess.run([command, *args], capture_output=True, text=True, timeout=15,
                                check=True, env=SYSTEM_ENV)
        if len(result.stdout.encode()) > 1024 * 1024:
            raise ValueError("oversized output")
        return result.stdout
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise _fail("MACHINE_COMMAND_FAILED", "系统时区命令失败；请检查 timedatectl 和 systemd") from exc


def discover_machine_browsers():
    """Inspect installation files without executing browser binaries or scripts."""
    result = []
    for browser in BROWSERS:
        kind = "not_installed"
        for candidate in CANDIDATES[browser["id"]]:
            try:
                resolved = Path(candidate).resolve(strict=True)
                if str(resolved).startswith(("/snap/", "/var/lib/flatpak/")):
                    kind = "snap" if str(resolved).startswith("/snap/") else "flatpak"
                    break
                info = resolved.stat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                if info.st_size < 128 * 1024:
                    script = resolved.read_bytes().decode("utf-8", errors="replace")
                    if re.search(r"/snap/bin/chromium|snap\s+(?:run\s+)?chromium|exec\s+[^\n]*/usr/bin/snap", script):
                        kind = "snap"
                        break
                kind = "native"
                break
            except FileNotFoundError:
                continue
        if kind == "not_installed":
            if browser["id"].startswith("chromium") and Path("/snap/bin/chromium").exists():
                kind = "snap"
            elif browser["id"] in FLATPAK_IDS and Path("/var/lib/flatpak/app", FLATPAK_IDS[browser["id"]]).exists():
                kind = "flatpak"
        result.append({**browser, "kind": kind})
    return result


class _Context:
    def __init__(self, options=None):
        options = options or {}
        self.root = Path(options.get("root", MACHINE_ROOT))
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise _fail("MACHINE_PATH_INVALID", "机器状态目录必须是绝对路径")
        self.private_root = self.root / "machine"
        self.public_path = self.root / "status.json"
        self.owner_path = self.private_root / "owner.json"
        self.trusted_uid = options.get("trusted_uid", options.get("trustedUid", 0))
        self.effective_uid = options.get("effective_uid", options.get("effectiveUid", os.geteuid))
        self.sudo_uid = options.get("sudo_uid", options.get("sudoUid", lambda: os.environ.get("SUDO_UID")))
        self.run = options.get("run_command", options.get("runCommand", _command))
        self.discover = options.get("discover_browsers", options.get("discoverBrowsers", discover_machine_browsers))
        self.test_boundary = "root" in options

    def trusted_path(self, path):
        path = Path(path)
        for current in (path, *path.parents):
            try:
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or (current == path and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)):
                    raise _fail("CONFIG_CONFLICT", "拒绝修改链接或特殊文件")
                if info.st_uid != self.trusted_uid or info.st_mode & 0o022:
                    raise _fail("MACHINE_PATH_UNTRUSTED", "机器配置路径必须由 root 拥有且不可由普通用户写入")
            except FileNotFoundError:
                pass
            if self.test_boundary and current == self.root:
                break

    def create_directory(self, path, mode):
        path = Path(path)
        self.trusted_path(path / ".fucc-path-check")
        # pathlib's parents=True ignores the supplied mode on ancestors. Build
        # every absent component with the requested mode, matching Node mkdir.
        missing = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            try:
                directory.mkdir(mode=mode)
            except FileExistsError:
                pass
            self.trusted_path(directory / ".fucc-path-check")
            directory.chmod(mode)
        self.trusted_path(path / ".fucc-path-check")
        path.chmod(mode)

    def read_public(self):
        self.trusted_path(self.public_path)
        snapshot = read_snapshot(self.public_path)
        if snapshot["text"] is None:
            try:
                initialized = (self.private_root / "local-transaction.initialized").exists()
            except PermissionError:
                initialized = True
            return {**_blank(), "recoveryRequired": initialized}
        try:
            value = json.loads(snapshot["text"])
        except ValueError as exc:
            raise _fail("MACHINE_STATE_INVALID", "公开机器状态损坏；需要管理员恢复") from exc
        if not _valid_public(value):
            raise _fail("MACHINE_STATE_INVALID", "公开机器状态无效；需要管理员恢复")
        return value

    def publish(self, value):
        self.trusted_path(self.public_path)
        replace_snapshot(self.public_path, {"text": json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n", "mode": 0o644}, read_snapshot(self.public_path))

    def read_owner(self):
        self.trusted_path(self.owner_path)
        snapshot = read_snapshot(self.owner_path)
        if snapshot["text"] is None:
            return None
        try:
            value = json.loads(snapshot["text"])
        except ValueError as exc:
            raise _fail("MACHINE_STATE_INVALID", "机器配置所有者记录损坏") from exc
        if type(value) is not dict or set(value) != {"ownerUid"} or not _uid(value["ownerUid"]):
            raise _fail("MACHINE_STATE_INVALID", "机器配置所有者记录无效")
        return value["ownerUid"]

    def read_timezone(self):
        zone = self.run("/usr/bin/timedatectl", ["show", "--property=Timezone", "--value"]).strip()
        if len(zone) > 128 or not ZONE.fullmatch(zone):
            raise _fail("MACHINE_TIMEZONE_INVALID", "无法读取有效系统时区")
        return zone

    def readable_directory(self, browser):
        path = Path(browser["directory"])
        for current in (path, *path.parents):
            if self.test_boundary and current == self.root:
                return
            try:
                info = current.lstat()
                if not stat.S_ISDIR(info.st_mode) or not info.st_mode & 0o001:
                    raise _fail("MACHINE_PATH_UNREADABLE", f"{browser['label']} 策略目录需要允许普通用户读取")
            except FileNotFoundError:
                pass

    def conflicts(self, browser):
        directory = Path(browser["directory"])
        try:
            paths = list(directory.iterdir())
        except FileNotFoundError:
            return
        keys = ["DefinePreferredLanguages"] if browser["id"] == "edge" else ["ForcedLanguages", "WebRtcIPHandling"]
        for path in paths:
            if path.suffix != ".json" or path.name == "fucc.json":
                continue
            self.trusted_path(path)
            snapshot = read_snapshot(path)
            if snapshot["text"] is not None and any(key in _policy_object(snapshot["text"], browser["label"]) for key in keys):
                raise _fail("CONFIG_CONFLICT", f"{browser['label']} 已有其他文件管理相同策略；FUCC 不会覆盖")


def desired_policies(browser, region):
    return {"DefinePreferredLanguages": ",".join(LANGUAGES[region])} if browser["id"] == "edge" else {
        "ForcedLanguages": list(LANGUAGES[region]), "WebRtcIPHandling": "disable_non_proxied_udp"}


def _summary(ctx, state, installations):
    try:
        actual_timezone = ctx.read_timezone()
    except Exception:
        actual_timezone = None
    configured_timezone = get_target_region(state["region"], state.get("timezone"))["timezone"] if state["active"] else None
    timezone_aligned = actual_timezone == configured_timezone if state["active"] else None
    browsers = []
    for browser in installations:
        managed = state["active"] and browser["id"] in state["managedBrowsers"]
        aligned = None
        if managed:
            try:
                path = Path(browser["directory"]) / "fucc.json"
                ctx.trusted_path(path)
                ctx.readable_directory(browser)
                snapshot = read_snapshot(path)
                actual = {} if snapshot["text"] is None else _policy_object(snapshot["text"], browser["label"])
                expected = desired_policies(browser, state["region"])
                ctx.conflicts(browser)
                aligned = snapshot["mode"] == 0o644 and all(canonical_json(actual.get(key)) == canonical_json(value) for key, value in expected.items())
            except Exception:
                aligned = False
        unsupported = browser["kind"] in ("snap", "flatpak")
        message = ("Snap/Flatpak 策略路径隔离；此版本不支持，请使用原生安装。" if unsupported else
                   "原生 Edge 语言策略已适配；Linux WebRTC 策略未获得官方支持确认。需在 edge://policy 验证策略。" if browser["id"] == "edge" else
                   "配置文件已核对；完全退出浏览器后启动，并在 chrome://policy 验证生效。" if managed else "未配置浏览器策略。")
        browsers.append({"id": browser["id"], "label": browser["label"], "installation": browser["kind"],
                         "policyPath": str(Path(browser["directory"]) / "fucc.json"), "managed": managed,
                         "configurationAligned": aligned, "effectivePolicy": "unsupported" if unsupported else "verify_in_browser" if managed else "not_configured",
                         "webRtcPolicy": "unsupported" if unsupported or browser["id"] == "edge" else "configured" if managed else "not_configured",
                         "restartRequired": managed, "message": message})
    health = "recovery_required" if state["recoveryRequired"] else "degraded" if timezone_aligned is False or any(x["configurationAligned"] is False for x in browsers) else "healthy"
    return {"scope": "machine", "active": state["active"], "region": state["region"], "ownerUid": state["ownerUid"],
            "recoveryRequired": state["recoveryRequired"], "health": health, "transaction": state["transaction"],
            "revision": state["revision"], "actualTimezone": actual_timezone, "configuredTimezone": configured_timezone,
            "timezoneAligned": timezone_aligned, "requiresAdministrator": ctx.effective_uid() != 0, "browsers": browsers,
            **({"timezone": state["timezone"]} if "timezone" in state else {})}


def _resources(ctx, installations):
    def write_owner(value):
        if value is not None and not _uid(value):
            raise _fail("MACHINE_STATE_INVALID", "无效机器所有者")
        ctx.trusted_path(ctx.owner_path)
        desired = {"text": None if value is None else json.dumps({"ownerUid": value}, separators=(",", ":")) + "\n", "mode": None if value is None else 0o600}
        replace_snapshot(ctx.owner_path, desired, read_snapshot(ctx.owner_path))

    def write_timezone(value):
        if type(value) is not str or len(value) > 128 or not ZONE.fullmatch(value):
            raise _fail("MACHINE_STATE_INVALID", "无效恢复时区")
        ctx.run("/usr/bin/timedatectl", ["set-timezone", value])

    resources = [Resource("owner", str(ctx.owner_path), ctx.read_owner, write_owner),
                 Resource("timezone", "/usr/bin/timedatectl:system-timezone", ctx.read_timezone, write_timezone)]
    for browser in installations:
        path = Path(browser["directory"]) / "fucc.json"

        def read(path=path):
            ctx.trusted_path(path)
            return read_snapshot(path)

        def write(value, path=path, browser=browser):
            desired = _parse_snapshot(value)
            ctx.trusted_path(path)
            if desired["text"] is not None:
                ctx.conflicts(browser)
                ctx.readable_directory(browser)
                ctx.create_directory(browser["directory"], 0o755)
            replace_snapshot(path, desired, read_snapshot(path))

        resources.append(Resource("policy." + browser["id"], str(path), read, write))
    return resources


def _public_from_status(status, owner, browsers, force_recovery, request_owner, committed_timezone=None):
    active = status["active"] and status["region"] is not None and owner is not None
    recovery = force_recovery or status["health"] == "recovery_required" or (status["active"] and not active)
    result = {"schema": "fucc-machine-public-v1", "active": active, "region": status["region"] if active else None,
            "ownerUid": owner if owner is not None else request_owner if recovery else None,
            "recoveryRequired": recovery, "revision": status["revision"], "transaction": status["transaction"],
            "managedBrowsers": [x["id"] for x in browsers if x["kind"] == "native"] if active else []}
    if active:
        canonical = get_target_region(status["region"], committed_timezone)["timezone"]
        if canonical != TARGET_REGIONS[status["region"]]["timezone"]:
            result.update(schema="fucc-machine-public-v2", timezone=canonical)
    return result


def run_machine_action(action, region=None, owner_uid=None, options=None, timezone=None):
    if not _uid(owner_uid):
        raise _fail("INVALID_OWNER", "owner UID 必须是非负整数")
    if action not in ("on", "off", "recover", "status"):
        raise _fail("INVALID_ACTION", "无效机器动作")
    if action == "on" and not _region(region):
        raise _fail("INVALID_REGION", "开启机器配置必须指定有效地区")
    if timezone is not None and action != "on":
        raise _fail("INVALID_ACTION", "时区参数仅用于开启配置；恢复使用保存的目标")
    target_timezone = get_target_region(region, timezone)["timezone"] if action == "on" else None
    ctx = _Context(options)
    installations = ctx.discover()
    if (len(installations) != len(BROWSERS) or {x["id"] for x in installations} != set(CANDIDATES)
            or any(x.get("kind") not in ("native", "snap", "flatpak", "not_installed") for x in installations)):
        raise _fail("MACHINE_CATALOG_INVALID", "机器浏览器目录无效")
    if action == "status":
        return _summary(ctx, ctx.read_public(), installations)
    if ctx.effective_uid() != 0:
        raise _fail("ADMINISTRATOR_REQUIRED", "机器配置需要管理员权限；请通过 FUCC 的提权入口执行")
    sudo_uid = ctx.sudo_uid()
    if sudo_uid is not None and (not re.fullmatch(r"[0-9]+", sudo_uid) or int(sudo_uid) != owner_uid):
        raise _fail("OWNER_MISMATCH", "机器配置 owner UID 必须与 sudo 调用者一致")
    ctx.create_directory(ctx.root, 0o755)
    ctx.create_directory(ctx.private_root, 0o700)
    ctx.create_directory(ctx.root / "locks", 0o700)
    with MutationLock(ctx.root, str(ctx.root) + "\0machine-action", recover=action == "recover"):
        try:
            previous = ctx.read_public()
        except FuccError as exc:
            if action != "recover" or exc.code != "MACHINE_STATE_INVALID":
                raise
            previous = {**_blank(), "recoveryRequired": True}
        owner = ctx.read_owner()
        if (owner is not None and owner != owner_uid) or (previous["ownerUid"] is not None and previous["ownerUid"] != owner_uid):
            raise _fail("OWNER_MISMATCH", "机器配置属于另一个用户，当前用户不能修改或还原")
        if previous["recoveryRequired"] and action != "recover":
            raise _fail("MACHINE_RECOVERY_REQUIRED", "机器配置事务未完成；请先运行 fucc recover")
        transaction = LocalTransaction(ctx.private_root, _resources(ctx, installations),
                                       wire_version=2 if action == "on" and target_timezone != TARGET_REGIONS[region]["timezone"] else 1)
        before = transaction.status()
        if action == "recover" and previous["ownerUid"] is None and owner is None and (before["active"] or before["transaction"] != "none" or before["health"] == "recovery_required"):
            if not matches_resource_claim(ctx.private_root, "owner", owner_uid):
                raise _fail("OWNER_MISMATCH", "恢复调用者不属于机器事务的所有者")
            previous = {**previous, "ownerUid": owner_uid}
        if action != "recover" and before["health"] == "recovery_required":
            raise _fail("MACHINE_RECOVERY_REQUIRED", "机器配置状态需要恢复")
        desired = None
        if action == "on":
            desired = {"owner": owner_uid, "timezone": target_timezone}
            for browser in installations:
                path = Path(browser["directory"]) / "fucc.json"
                ctx.trusted_path(path)
                current = read_snapshot(path)
                if browser["kind"] == "native":
                    ctx.conflicts(browser)
                    ctx.readable_directory(browser)
                    policies = {} if current["text"] is None else _policy_object(current["text"], browser["label"])
                    desired["policy." + browser["id"]] = {"text": json.dumps({**policies, **desired_policies(browser, region)}, ensure_ascii=False, indent=2) + "\n", "mode": 0o644}
                else:
                    desired["policy." + browser["id"]] = current
        ctx.publish({**previous, "ownerUid": previous["ownerUid"] if previous["ownerUid"] is not None else owner_uid,
                     "recoveryRequired": True, "transaction": "apply" if action == "on" else "restore" if action == "off" else previous["transaction"]})
        result = transaction.apply(region, desired) if action == "on" else transaction.restore() if action == "off" else transaction.recover()
        after = transaction.status()
        state = _public_from_status(after, ctx.read_owner(), installations, result["kind"] == "recovery_required", owner_uid,
                                    transaction.committed_desired("timezone"))
        ctx.publish(state)
        return {**_summary(ctx, state, installations), "kind": result["kind"]}


def _invoke_helper(python_path, entrypoint, action, owner_uid, environment, region=None, timezone=None):
    command = ["/usr/bin/sudo", "--", python_path, "-I", entrypoint, "__machine", action, "--owner", str(owner_uid)]
    if region is not None:
        command.extend(["--region", region])
    if timezone is not None:
        command.extend(["--timezone", get_target_region(region, timezone)["timezone"]])
    env = {"PATH": SYSTEM_ENV["PATH"]}
    if "TERM" in environment:
        env["TERM"] = environment["TERM"]
    try:
        # The controlling terminal, stdin and stderr retain sudo's normal prompt.
        result = subprocess.run(command, stdout=subprocess.PIPE, text=True, env=env)
        if result.returncode or len(result.stdout.encode()) > 1024 * 1024:
            raise ValueError("helper failed")
        envelope = json.loads(result.stdout)
        parsed = envelope if type(envelope) is dict and envelope.get("scope") == "machine" else envelope.get("machine") if type(envelope) is dict and type(envelope.get("schemaVersion")) in (int, float) and envelope.get("schemaVersion") == 1 and envelope.get("ok") is True else None
        switch = {key: parsed.get(key) for key in ("active", "region", "ownerUid")} if type(parsed) is dict else None
        reported_timezone = None
        if type(parsed) is dict and parsed.get("active") is True and _region(parsed.get("region")):
            reported_timezone = parsed.get("configuredTimezone", parsed.get("timezone", TARGET_REGIONS[parsed["region"]]["timezone"]))
            if "timezone" in parsed or reported_timezone != TARGET_REGIONS[parsed["region"]]["timezone"]:
                switch["timezone"] = parsed.get("timezone", reported_timezone)
        if (type(parsed) is not dict or parsed.get("scope") != "machine" or parsed.get("recoveryRequired") is not False
                or not _valid_switch(switch)
                or ("timezone" in parsed and reported_timezone != parsed["timezone"])
                or (action == "on" and (parsed["active"] is not True or parsed["region"] != region or parsed["ownerUid"] != owner_uid
                                         or reported_timezone != get_target_region(region, timezone)["timezone"]))
                or (action == "off" and parsed["active"] is not False)):
            raise ValueError("invalid helper result")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise _fail("MACHINE_HELPER_FAILED", "管理员助手未完成或结果无效；请检查终端输出并运行 fucc status") from exc


class MachineAdapter:
    def __init__(self, entrypoint, python_path, environment=None, test_options=None):
        if any(not Path(path).is_absolute() or any(char in str(path) for char in "\0\r\n") for path in (entrypoint, python_path)):
            raise _fail("MACHINE_PATH_INVALID", "管理员助手必须使用有效绝对路径")
        self.owner_uid = os.getuid()
        if not _uid(self.owner_uid) or self.owner_uid == 0:
            raise _fail("ROOT_USER_SESSION", "请以普通用户运行本机环境管理；FUCC 仅在机器助手内提权")
        self.entrypoint, self.python_path = str(entrypoint), str(python_path)
        self.environment = dict(os.environ if environment is None else environment)
        self.test_options = test_options
        self.resource = Resource("machine", "fucc-machine:v1:/var/lib/fucc", self._read, self._write)

    def inspect(self):
        return run_machine_action("status", owner_uid=self.owner_uid, options=self.test_options)

    def desired(self, region, timezone=None):
        if not _region(region):
            raise _fail("INVALID_REGION", "无效机器配置地区")
        canonical = get_target_region(region, timezone)["timezone"]
        return {"active": True, "region": region, "ownerUid": self.owner_uid,
                **({"timezone": canonical} if canonical != TARGET_REGIONS[region]["timezone"] else {})}

    def recover(self):
        _invoke_helper(self.python_path, self.entrypoint, "recover", self.owner_uid, self.environment)

    def _read(self):
        state = self.inspect()
        if state["recoveryRequired"] or state["health"] != "healthy":
            raise _fail("MACHINE_RECOVERY_REQUIRED", "机器配置未对齐或事务需要恢复；请检查 fucc status")
        return {"active": state["active"], "region": state["region"], "ownerUid": state["ownerUid"] if state["active"] else None,
                **({"timezone": state["timezone"]} if "timezone" in state else {})}

    def _write(self, value):
        if not _valid_switch(value) or value["active"] and value["ownerUid"] != self.owner_uid:
            raise _fail("OWNER_MISMATCH", "不能操作另一个用户的机器配置")
        current = self.inspect()
        if current["ownerUid"] is not None and current["ownerUid"] != self.owner_uid:
            raise _fail("OWNER_MISMATCH", "机器配置属于另一个用户")
        if current["recoveryRequired"]:
            self.recover()
        _invoke_helper(self.python_path, self.entrypoint, "on" if value["active"] else "off", self.owner_uid, self.environment,
                       value["region"], value.get("timezone"))
