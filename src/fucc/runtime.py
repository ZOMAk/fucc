"""Unified user coordinator; privileged machine changes remain in their own journal."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .apps import create_user_adapters
from .bash import BashRuntime, list_linux_locales
from .errors import FuccError
from .machine import MachineAdapter
from .paths import persist_root
from .regions import get_target_region, resolve_region, target_environment
from .transaction import LocalTransaction, Resource, read_resource_bindings


def assert_locale(region, list_locales=None):
    wanted = get_target_region(region)["lang"]
    normalize = lambda value: value.lower().replace("-", "").replace("_", "")
    if not any(normalize(value) == normalize(wanted) for value in (list_locales or list_linux_locales)()):
        raise FuccError(f"未安装 Locale {wanted}；请先运行 sudo locale-gen {wanted}，再重试。", "INVALID_COMMAND", 10)


def assert_local_inactive(environment=None):
    try:
        bindings = read_resource_bindings(persist_root(environment) / "local")
    except FuccError as exc:
        raise FuccError("统一配置状态无效；请保留状态和备份，运行 fucc recover。") from exc
    if bindings:
        raise FuccError("统一本机管理已开启或需要恢复；请使用 fucc on/off/recover。", "INVALID_COMMAND", 10)


class LocalRuntime:
    def __init__(self, environment=None, application_dirs=None, machine=None, list_locales=None):
        if machine is None and os.getuid() == 0:
            raise FuccError("请以日常桌面用户运行 FUCC；仅机器助手使用管理员权限。", "INVALID_COMMAND", 10)
        self.environment = dict(os.environ if environment is None else environment)
        self.application_dirs = application_dirs
        self.list_locales = list_locales
        self.root = persist_root(self.environment) / "local"
        try:
            bindings = read_resource_bindings(self.root)
        except FuccError:
            bindings = []  # The coordinator itself retains and rejects the invalid state.
        self.bash = BashRuntime(self.environment, list_locales=list_locales)
        self.user = create_user_adapters(self.environment, application_dirs,
                                        [entry for entry in bindings if entry["id"].startswith("desktop.")] if bindings else None)
        self.machine = machine if machine is not None else MachineAdapter(
            str(Path(__file__).with_name("helper.py")), sys.executable, self.environment)
        self.transaction = self._transaction(self.user)

    def _transaction(self, user):
        resource = Resource("bash-profile", str(persist_root(self.environment)), self._read_bash, self._write_bash,
                            lambda value: {"region": None})
        return LocalTransaction(self.root, [resource, *user.resources, self.machine.resource],
                                before_recover=self._recover_nested)

    def _reload_bindings(self):
        bindings = read_resource_bindings(self.root)
        self.user = create_user_adapters(self.environment, self.application_dirs,
                                        [entry for entry in bindings if entry["id"].startswith("desktop.")] if bindings else None)
        self.transaction = self._transaction(self.user)

    def _read_bash(self):
        status = self.bash.status()
        if status["health"] != "healthy" or status["transaction"]["kind"] != "none" or not status["platform"]["configurationAligned"]:
            raise FuccError("Bash 配置存在冲突或需要恢复；请检查 fucc status。", "RECOVERY_REQUIRED", 21)
        target = status["target"]
        return {"region": target["region"] if target else None,
                **({"timezone": target["timezone"]} if target and "timezone" in target else {})}

    def _write_bash(self, value):
        if type(value) is not dict or set(value) not in ({"region"}, {"region", "timezone"}) or (value["region"] is None and "timezone" in value):
            raise FuccError("Invalid Bash resource target")
        status = self.bash.status()
        if status["health"] == "recovery_required" or status["transaction"]["kind"] != "none":
            if self.bash.recover()["kind"] == "recovery_required":
                raise FuccError("Bash 恢复未完成。", "RECOVERY_REQUIRED", 21)
        target = None if value["region"] is None else get_target_region(value["region"], value.get("timezone"))
        result = self.bash.restore() if target is None else self.bash.protect(
            {"mode": "standard", "region": target["code"], **({"timezone": value["timezone"]} if "timezone" in value else {})})
        if result["kind"] in ("recovery_required", "compensated"):
            raise FuccError("Bash 配置转换未提交。", "RECOVERY_REQUIRED", 21)

    def _recover_nested(self):
        try:
            if self.machine.inspect()["recoveryRequired"]:
                self.machine.recover()
        except FuccError as exc:
            if exc.code != "MACHINE_STATE_INVALID":
                raise
            self.machine.recover()
        status = self.bash.status()
        if status["health"] == "recovery_required" or status["transaction"]["kind"] != "none":
            if self.bash.recover()["kind"] == "recovery_required":
                raise FuccError("Bash 恢复未完成。", "RECOVERY_REQUIRED", 21)

    def target_region(self, region=None):
        saved = self.transaction.status()
        bash = self.bash.status()
        return resolve_region(region, saved["region"] or (bash["target"]["region"] if bash["target"] else None), bash["preferredRegion"])["code"]

    def saved_timezone(self, region):
        saved = self.transaction.status()
        if saved["region"] == region:
            target = self.transaction.committed_desired("bash-profile")
            if target is not None:
                return get_target_region(region, target.get("timezone"))["timezone"]
        bash = self.bash.status()
        if bash["target"] and bash["target"]["region"] == region:
            return get_target_region(region, bash["target"].get("timezone"))["timezone"]
        return None

    def on(self, region=None, user_only=False, timezone=None):
        target = self.target_region(region)
        profile = get_target_region(target, timezone)
        dynamic = profile["timezone"] != get_target_region(target)["timezone"]
        assert_locale(target, self.list_locales)
        bindings = read_resource_bindings(self.root)
        candidate = create_user_adapters(self.environment, self.application_dirs,
                                        [entry for entry in bindings if entry["id"].startswith("desktop.")] if bindings else None,
                                        enroll_firefox=True)
        enrollment = {entry.id for entry in candidate.resources} != {entry.id for entry in self.user.resources}
        if enrollment:
            self.user = candidate
            self.transaction = self._transaction(self.user)
        desired = {**self.user.desired(target, profile["timezone"]),
                   "bash-profile": {"region": target, **({"timezone": profile["timezone"]} if dynamic else {})},
                   self.machine.resource.id: self.machine.resource.read() if user_only else
                   self.machine.desired(target, profile["timezone"]) if dynamic else self.machine.desired(target)}
        firefox = desired[self.machine.resource.id].get("firefox") is True or any(
            app["kind"] == "firefox" and app["supported"] for app in self.user.applications)
        result = self.transaction.apply(target, desired, wire_version=3 if firefox else 2 if dynamic else 1,
                                        enroll=enrollment)
        if enrollment:
            # A compensated enrollment publishes the previous, smaller resource set.
            self._reload_bindings()
        return result

    def off(self):
        return self.transaction.restore()

    def recover(self):
        result = self.transaction.recover()
        if result["kind"] != "recovery_required":
            self._reload_bindings()
        return result

    def status(self):
        saved = self.transaction.status()
        bash = self.bash.status()
        try:
            machine = self.machine.inspect()
        except Exception:
            machine = {"scope": "machine", "active": False, "region": None, "ownerUid": None,
                       "recoveryRequired": True, "health": "recovery_required", "transaction": "none", "revision": 0,
                       "actualTimezone": None, "timezoneAligned": None, "requiresAdministrator": True, "browsers": [],
                       "readError": "机器状态无法核实；请保留私有状态并运行 fucc recover。"}
        visible = self.user.applications if saved["active"] or saved["transaction"] != "none" else create_user_adapters(
            self.environment, self.application_dirs).applications
        applications = []
        for app in visible:
            resource = next((entry for entry in saved["resources"] if entry["id"].startswith("desktop.")
                             and Path(entry["identity"]).name == app["id"]), None)
            managed = saved["active"] and resource is not None
            applications.append({**app, "managed": managed, "configurationAligned": resource["aligned"] if managed else None})
        nested_recovery = machine["recoveryRequired"] or bash["health"] == "recovery_required" or bash["transaction"]["kind"] != "none"
        health = "recovery_required" if nested_recovery else "degraded" if saved["health"] == "healthy" and "degraded" in (machine["health"], bash["health"]) else saved["health"]
        configured = None
        if saved["region"] is not None:
            try:
                committed = self.transaction.committed_desired("bash-profile")
                configured = target_environment(saved["region"], committed.get("timezone") if committed else None)
            except FuccError:
                health = "recovery_required"
        return {**saved, "health": health, "scope": "local-claude-environment",
                "configured": configured,
                "bash": bash["platform"], "machine": machine, "applications": applications,
                "coverage": {"claudeSettings": "当前用户 settings.json 的 env 三键；项目、settings.local 和管理配置可能覆盖。",
                             "vscodeSettings": "VS Code 默认用户配置；独立 profiles、Insiders、自定义 user-data-dir 和远程扩展环境未逐一管理。",
                             "machineConfigurationVerified": machine["health"] == "healthy", "applicationRuntimeVerified": False},
                "paths": self.user.paths,
                "activation": {
                    "applications": "完全退出 Chrome、Firefox、VS Code 和 Claude Desktop 后，从桌面入口重新启动；VS Code 新窗口可能复用旧进程。" if saved["active"] else "未开启统一配置。关闭后已运行的应用需完全退出后重开。",
                    "terminal": "新开交互式 Bash，或加载 bash.activation 给出的 env.sh。" if saved["active"] else "关闭后从桌面重新打开终端，已有父终端可能仍保留旧环境。",
                    "session": "environment.d 配置在下一次完整桌面会话启动时读取；已有会话和进程不会自动更换环境。",
                    "verifiedRunningApplications": False}}
