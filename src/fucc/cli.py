"""Plain-text CLI. No Node runtime or graphical interface is required."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

from . import __version__
from .bash import BashRuntime, read_persist_state
from .detection import check
from .errors import FuccError
from .machine import run_machine_action
from .network import fetch_ip_intelligence
from .output import check_report, display, ip_report, json_output, summary, table
from .paths import persist_root
from .regions import TARGET_REGIONS, get_target_region, resolve_region, resolve_timezone, target_environment
from .runtime import LocalRuntime, assert_local_inactive, assert_locale
from .transaction import read_committed_target


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise FuccError(message, "INVALID_COMMAND", 10)


def parser():
    root = Parser(prog="fucc", description="Fix You, Claude Code - Ubuntu local Claude environment management")
    root.add_argument("--version", "-V", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)

    def command(parent, name, help_text, region=False, json=True):
        item = parent.add_parser(name, help=help_text, description=help_text)
        if region:
            item.add_argument("--region", help="Target: us, eu, jp, sg")
        if json:
            item.add_argument("--json", action="store_true", help="Print structured JSON")
        return item

    on = command(commands, "on", "Enable unified local Claude environment management", region=True)
    on.add_argument("--timezone", help="Override the US IANA timezone; otherwise select from exit IP")
    on.add_argument("--user-only", action="store_true", help="Manage user configuration without system timezone or browser policies")
    command(commands, "off", "Restore the original local Claude environment")
    command(commands, "recover", "Recover interrupted local environment changes")
    command(commands, "status", "Show configuration coverage and restart requirements")
    inspect = command(commands, "check", "Inspect current process and network signals", region=True)
    inspect.add_argument("--timezone", help="Compare against this IANA timezone instead of the saved target")
    inspect.add_argument("--debug", action="store_true", help="Print timing to stderr")
    run = command(commands, "run", "Run a command with the selected process environment", region=True, json=False)
    run.add_argument("--timezone", help="Override the US IANA timezone; otherwise use the saved target or exit IP")
    run.add_argument("arguments", nargs=argparse.REMAINDER, help="-- command [arguments ...]")
    persist = command(commands, "persist", "Manage the legacy Bash profile", json=False).add_subparsers(dest="operation", required=True)
    persist_on = command(persist, "on", "Save a standard Bash environment profile", region=True)
    persist_on.add_argument("--level", default="standard", help="Profile level (standard only)")
    persist_on.add_argument("--deep", action="store_true", help=argparse.SUPPRESS)
    for name in ("off", "status", "recover"):
        command(persist, name, f"Bash profile {name}")
    proxy = command(commands, "proxy", "Inspect network egress", json=False).add_subparsers(dest="operation", required=True)
    command(proxy, "check", "Query public IP intelligence")
    regions = command(commands, "region", "List targets and manage the saved preference", json=False).add_subparsers(dest="operation", required=True)
    command(regions, "list", "List supported target environments")
    command(regions, "status", "Show preferred and active regions")
    command(regions, "set", "Set the preferred region while the profile is off").add_argument("code")
    return root


def _json(options, payload):
    if getattr(options, "json", False):
        json_output({"schemaVersion": 1, "ok": True, **payload})
        return True
    return False


def _health(status):
    return {"healthy": 0, "degraded": 2, "recovery_required": 21}[status["health"]]


def _network_observation():
    # Network information is a live CLI observation, separate from durable configuration health.
    unavailable = {"availability": "unavailable", "scope": "cli-process", "ip": None, "country": None,
                   "region": None, "city": None, "timezone": None, "timezoneSources": [],
                   "sources": [], "sourceCount": 0, "multiSourceConsistent": None, "geolocationConsistent": None}
    try:
        intel = fetch_ip_intelligence()
        if intel is None:
            return unavailable
        return {"availability": "available", "scope": "cli-process",
                **{key: intel.get(key) for key in ("ip", "country", "region", "city", "timezone")},
                "timezoneSources": intel.get("timezoneSources", []),
                "geolocationConsistent": intel.get("geolocationConsistent", intel["multiSourceConsistent"]),
                "sources": intel.get("sources", []), "sourceCount": intel["sourceCount"],
                "multiSourceConsistent": intel["multiSourceConsistent"]}
    except Exception:
        # Provider or proxy failures must not turn a committed local change into a command failure.
        return unavailable


def _network_rows(network):
    if network["availability"] != "available":
        return [("Egress IP", "unavailable"), ("IP region", "unavailable"), ("IP timezone", "unavailable"), ("IP sources", "unavailable")]
    source_names = ", ".join(network["sources"]) or f"{network['sourceCount']} sources"
    consistency = "single source" if network["sourceCount"] == 1 else "consistent" if network["multiSourceConsistent"] else "inconsistent"
    return [("Egress IP", network["ip"]),
            ("IP region", " / ".join(network[key] or "unknown" for key in ("country", "region", "city"))),
            ("IP timezone", network["timezone"] or "unavailable"),
            ("IP sources", f"{source_names} ({consistency})")]


def _local_rows(status, network, selection=None):
    return [
        ("Mode", "on" if status["active"] else "off"), ("Region", status["region"] or "none"),
        ("Health", status["health"]), ("Transaction", status["transaction"]),
        *_network_rows(network),
        *(([("Timezone selection", f"{selection['source']}: {selection['timezone']}"),
             ("Selection detail", selection["reason"])]) if selection else []),
        ("Scope", "Legacy configuration; run fucc on to migrate" if status.get("nativeMigrationRequired") else
         "Native settings, system timezone/policies, Claude Desktop launcher, Bash"),
        *((key, (status["configured"] or {}).get(key, "none")) for key in ("TZ", "LANG", "LC_ALL")),
        ("Resources aligned", f"{sum(item['aligned'] for item in status['resources'])}/{len(status['resources'])}"),
        ("Machine profile", f"{status['machine']['region'] if status['machine']['active'] else 'off / unknown'}; {status['machine']['health']}"),
        ("System timezone", status["machine"]["actualTimezone"] or "unknown")]


def _bash_rows(status):
    platform = status["platform"]
    return [("Mode", status["mode"]), ("Region", status["target"]["region"] if status["target"] else status["preferredRegion"]),
            ("Health", status["health"]), ("Transaction", status["transaction"]["kind"]),
            ("Scope", "Bash environment: TZ, LANG, LC_ALL"),
            ("Configuration aligned", display(platform.get("configurationAligned"))),
            ("Current process aligned", "No active profile" if status["mode"] == "daily" else display(platform.get("currentProcessAligned"))),
            ("System timezone", f"{display(platform.get('systemTimezone'))} (read only)"),
            *((label, display(platform.get(key))) for label, key in (("Environment file", "envFile"), ("Bash startup file", "bashrc"),
              ("Configured values", "configured"), ("Current process values", "currentProcess"), ("Activation", "activation")))]


def resolve_target(explicit=None, environment=None):
    if explicit is not None:
        return get_target_region(explicit)["code"]
    active = preferred = None
    root = persist_root(environment)
    unified_unavailable = False
    try:
        target = read_committed_target(root / "local")
        if target is not None:
            return target["region"]
    except FuccError:
        unified_unavailable = True
    try:
        value, degraded = read_persist_state(root)
        if value is not None and not degraded:
            active = value["committedTarget"]["region"] if value["committedTarget"] and not unified_unavailable else None
            preferred = value["preferredRegion"]
    except FuccError:
        pass  # Stateless diagnostics remain usable with missing/corrupt saved state.
    return resolve_region(active=active, preferred=preferred)["code"]


def saved_timezone(region, environment=None):
    root = persist_root(environment)
    try:
        target = read_committed_target(root / "local")
        if target is not None:
            return get_target_region(region, target.get("timezone"))["timezone"] if target["region"] == region else None
    except FuccError:
        return None  # Never inherit an uncommitted nested Bash target.
    try:
        state, degraded = read_persist_state(root)
        target = state["committedTarget"] if state is not None and not degraded else None
        if target and target["region"] == region:
            return get_target_region(region, target.get("timezone"))["timezone"]
    except FuccError:
        pass
    return None


def run_command(arguments, region, environment=None, timezone=None):
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    if not arguments:
        raise FuccError("请提供命令，例如：fucc run -- claude", "INVALID_COMMAND", 10)
    assert_locale(region)
    env = {**(os.environ if environment is None else environment), **target_environment(region, timezone)}
    try:
        child = subprocess.Popen(arguments, env=env)
    except OSError as exc:
        raise FuccError("子进程启动失败；请检查命令是否存在且可以执行。", "INTERNAL", 30) from exc
    handlers = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda received, frame: child.send_signal(received) if child.poll() is None else None)
        result = child.wait()
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    return result if result >= 0 else 128 - result


def _result(result, name="fucc recover"):
    if result["kind"] == "compensated":
        raise FuccError("配置更新失败，已验证回退到之前的配置。", "COMPENSATED", 22)
    if result["kind"] == "recovery_required":
        raise FuccError(f"恢复未完成或受管资源存在外部修改；请查看 status --json 并运行 {name}。请保留状态和备份；恢复不会覆盖外部修改。", "INCOMPLETE_RECOVERY", 23)


def dispatch(options):
    if getattr(options, "region", None) is not None:
        get_target_region(options.region)
    command = options.command
    if command in ("on", "off", "status", "recover"):
        runtime = LocalRuntime()
        result = None
        selection = None
        network = None
        if command != "status":
            if command == "on":
                region = runtime.target_region(options.region)
                get_target_region(region, options.timezone)
                network = _network_observation()
                selection = resolve_timezone(region, network if network["availability"] == "available" else None,
                                             runtime.saved_timezone(region), options.timezone)
                result = runtime.on(region, options.user_only, selection["timezone"])
            else:
                result = getattr(runtime, command)()
            _result(result)
        status = runtime.status()
        if network is None:
            network = _network_observation()
        payload = {"status": status, "network": network, **({"result": result["kind"]} if result else {}),
                   **({"timezoneSelection": selection} if selection else {})}
        if not _json(options, payload):
            summary(f"FUCC {__version__} | Local {command}" + (f" | {result['kind']}" if result else ""), _local_rows(status, network, selection))
        return _health(status)
    if command == "check":
        region = resolve_target(options.region)
        get_target_region(region, options.timezone)
        started = time.monotonic()
        timezone = options.timezone or saved_timezone(region)
        response = check(region, timezone=timezone)
        if options.timezone is None and timezone is not None:
            response["timezoneSelection"] = {"timezone": timezone, "source": "saved", "reason": "Saved FUCC target timezone"}
        if options.debug:
            print(f"[debug] check duration: {(time.monotonic() - started) * 1000:.0f}ms", file=sys.stderr)
        if options.json:
            json_output({"schemaVersion": 1, "targetRegion": region, **response})
        else:
            check_report(response, region)
        return 0
    if command == "run":
        if not options.arguments or options.arguments == ["--"]:
            raise FuccError("请提供命令，例如：fucc run -- claude", "INVALID_COMMAND", 10)
        region = resolve_target(options.region)
        timezone = saved_timezone(region)
        get_target_region(region, options.timezone)
        network = _network_observation() if region == "us" and timezone is None and options.timezone is None else None
        selection = resolve_timezone(region, network if network and network["availability"] == "available" else None,
                                     timezone, options.timezone)
        return run_command(options.arguments, region, timezone=selection["timezone"])
    if command == "proxy":
        intel = fetch_ip_intelligence()
        if intel is None:
            raise FuccError("无法获取出口 IP 信息；请检查连接后重试。", "INTERNAL", 30)
        if not _json(options, {"ipIntel": intel}):
            ip_report(intel)
        return 0
    if command == "region" and options.operation == "list":
        if not _json(options, {"default": "us", "regions": list(TARGET_REGIONS.values())}):
            print(table(f"FUCC {__version__} | Regions", ["Code", "Name", "Timezone", "Locale"],
                        [[r["code"], r["name"], r["timezone"], r["lang"]] for r in TARGET_REGIONS.values()], weights=[1, 2, 3, 2]))
        return 0
    if command == "region":
        if options.operation == "set":
            get_target_region(options.code)
            assert_local_inactive()
        runtime = BashRuntime()
        result = runtime.set_preferred_region(options.code) if options.operation == "set" else None
        status = runtime.status()
        payload = {"requested": options.code, "preferredRegion": status["preferredRegion"], "mode": status["mode"], "noOp": result["kind"] == "noop"} if result else {
            "mode": status["mode"], "preferredRegion": status["preferredRegion"], "activeRegion": status["target"]["region"] if status["target"] else None,
            "health": status["health"], "transaction": status["transaction"]}
        if not _json(options, payload):
            summary(f"FUCC {__version__} | Region {options.operation}", [(key, display(value)) for key, value in payload.items()])
        return 0
    if command == "persist":
        operation = options.operation
        if operation == "on":
            if options.deep and options.level == "standard":
                raise FuccError("FUCC 不支持 --deep。", "CONFLICTING_PROTECTION_LEVEL", 10)
            if options.deep or options.level != "standard":
                raise FuccError("FUCC 仅支持 --level standard。", "INVALID_PROTECTION_LEVEL", 10)
        if operation != "status":
            assert_local_inactive()
        runtime = BashRuntime()
        if operation == "status":
            status = runtime.status()
            if not _json(options, {"status": status}):
                summary(f"FUCC {__version__} | Persist status", _bash_rows(status))
            return 0
        before = runtime.status()
        requested = {"mode": "standard", "region": resolve_region(options.region, before["target"]["region"] if before["target"] else None, before["preferredRegion"])["code"]} if operation == "on" else None
        result = runtime.protect(requested) if operation == "on" else runtime.restore() if operation == "off" else runtime.recover()
        _result(result, "fucc persist recover")
        after = runtime.status()
        if not _json(options, {"requested": requested, "committed": after["target"], "preferredRegion": after["preferredRegion"],
                              "health": after["health"], "transaction": after["transaction"], "noOp": result["kind"] == "noop", "platform": after["platform"]}):
            summary(f"FUCC {__version__} | Persist {operation} | {result['kind']}", _bash_rows(after))
        return 2 if result["kind"] == "degraded" else 0
    raise FuccError("无效命令", "INVALID_COMMAND", 10)


def _machine(arguments):
    root = Parser(prog="fucc __machine")
    root.add_argument("action", choices=("on", "off", "recover", "status"))
    root.add_argument("--owner", required=True)
    root.add_argument("--region")
    root.add_argument("--timezone")
    root.add_argument("--firefox", choices=("on", "off"), default="off")
    options = root.parse_args(arguments)
    if not options.owner.isascii() or not options.owner.isdigit() or int(options.owner) > 9007199254740991:
        raise FuccError("Invalid owner UID", "INVALID_COMMAND", 10)
    json_output(run_machine_action(options.action, options.region, int(options.owner),
                                   firefox=options.firefox == "on",
                                   **({"timezone": options.timezone} if options.timezone is not None else {})))
    return 0


def _classify(error):
    if error.code in ("MACHINE_RECOVERY_REQUIRED", "RECOVERY_REQUIRED", "RECOVERY_CONTEXT_INVALID"):
        return 21, "RECOVERY_REQUIRED"
    if error.code in ("OWNER_MISMATCH", "ROOT_USER_SESSION", "ADMINISTRATOR_REQUIRED", "UNSUPPORTED_FEATURE", "LOCALE_UNAVAILABLE", "INVALID_ACTION", "INVALID_OWNER", "REGION_SET_REQUIRES_DAILY"):
        return 10, "REGION_SET_REQUIRES_DAILY" if error.code == "REGION_SET_REQUIRES_DAILY" else "INVALID_COMMAND"
    if error.code == "MACHINE_HELPER_FAILED":
        return 30, "INTERNAL"
    if error.exit_code == 24:
        return 24, "STATE_INVALID"
    return error.exit_code, error.code


def main(arguments=None):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    is_helper = bool(arguments and arguments[0] == "__machine")
    structured = "--json" in arguments or is_helper
    try:
        if not sys.platform.startswith("linux"):
            raise FuccError("FUCC requires Linux and Bash.", "INVALID_COMMAND", 10)
        if is_helper:
            return _machine(arguments[1:])
        return dispatch(parser().parse_args(arguments))
    except FuccError as exc:
        code, identifier = _classify(exc)
        if structured:
            json_output({"schemaVersion": 1, "ok": False, "error": {"id": identifier, "code": code, "message": str(exc)}})
        else:
            print(f"Error [{identifier}]: {exc}", file=sys.stderr)
        return code
    except KeyboardInterrupt:
        message = "已取消。若操作已经开始，状态和恢复日志会保留；可重试 fucc off 继续关闭，或运行 fucc recover。"
        if structured:
            json_output({"schemaVersion": 1, "ok": False, "error": {"id": "CANCELLED", "code": 130, "message": message}})
        else:
            print(f"Error [CANCELLED]: {message}", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0
    except Exception:
        # Never expose configuration values, credentials, or a traceback in public output.
        message = "内部操作失败；请保留状态和备份，检查安装及文件权限。"
        if structured:
            json_output({"schemaVersion": 1, "ok": False, "error": {"id": "INTERNAL", "code": 30, "message": message}})
        else:
            print(f"Error [INTERNAL]: {message}", file=sys.stderr)
        return 30
