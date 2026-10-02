"""Bash persistence, including the checked state and immutable v1 backup.

The environment vocabulary, hook text and durable schemas are shared with
FUCC 0.0.1. User edits outside the exact hook remain live through restoration.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid

from .errors import FuccError
from .files import read_snapshot, replace_snapshot
from .paths import home_directory, persist_root, xdg_directory
from .regions import TARGET_REGIONS, get_target_region, target_environment
from .state import MutationLock, read_checked, write_checked

KEYS = ("TZ", "LANG", "LC_ALL")
EMPTY_ENV = dict.fromkeys(KEYS)
HEADER = "# FUCC managed environment v1\n"
BEGIN = "# >>> fucc environment >>>"
END = "# <<< fucc environment <<<"
PHASES = ("planned", "applying", "verified", "compensating", "compensated", "recovery_required")
TRANSITIONS = {"planned": ("applying", "compensated", "recovery_required"),
               "applying": ("verified", "compensating", "recovery_required"),
               "verified": ("applying", "compensating", "recovery_required"),
               "compensating": ("compensated", "recovery_required"), "compensated": (),
               "recovery_required": ("applying", "compensating")}
STATE_SCHEMA = "fucc-state-v1"
STATE_SCHEMA_V2 = "fucc-state-v2"
BACKUP_SCHEMA = "fucc.linux-backup.v1"
JOURNAL_SCHEMA = "fucc-transaction-journal-v1"
VALUES_SCHEMA = "fucc-transaction-journal-values-v1"


def _fail(code, message, exit_code=24):
    return FuccError(message, code=code, exit_code=exit_code)


def _region(value):
    return type(value) is str and value in TARGET_REGIONS


def _v1(value):
    # JavaScript === 1 accepts the JSON number 1.0, but never the boolean true.
    return type(value) in (int, float) and value == 1


def _target(value):
    if value is None:
        return True
    if (type(value) is not dict or not {"mode", "region"}.issubset(value)
            or not set(value).issubset({"mode", "region", "timezone"})
            or value["mode"] != "standard" or not _region(value["region"])):
        return False
    if "timezone" not in value:
        return True
    try:
        # Persist only canonical non-default overrides, keeping the original
        # wire representation identical for every fixed regional preset.
        zone = value["timezone"]
        return (type(zone) is str and get_target_region(value["region"], zone)["timezone"] == zone
                and zone != TARGET_REGIONS[value["region"]]["timezone"])
    except FuccError:
        return False


def _target_environment(target):
    return target_environment(target["region"], target.get("timezone"))


def valid_environment(value):
    return (type(value) is dict and set(value) == set(KEYS) and
            (all(value[key] is None for key in KEYS) or
             all(type(value[key]) is str and not re.search(r"[\r\n\0]", value[key]) for key in KEYS)))


def _stored(value):
    return type(value) is dict and set(value) == {"kind", "value"} and value["kind"] == "value" and valid_environment(value["value"])


def _store(value):
    return {"kind": "value", "value": deepcopy(value)}


def _timestamp(value):
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _state_valid(value):
    if type(value) is not dict or set(value) != {"schemaVersion", "revision", "committedTarget", "preferredRegion", "health", "degradation", "activeTransactionId", "updatedAt"}:
        return False
    degradation = value["degradation"]
    version = value["schemaVersion"]
    return (type(version) in (int, float) and version in (1, 2)
            and (version == 2 or not (type(value["committedTarget"]) is dict and "timezone" in value["committedTarget"]))
            and type(value["revision"]) is int and 0 <= value["revision"] <= 9007199254740991
            and _target(value["committedTarget"]) and _region(value["preferredRegion"])
            and value["health"] in ("healthy", "degraded", "recovery_required")
            and type(degradation) is list and all(x == {"kind": "environment_unaligned", "cause": "write_denied"} for x in degradation)
            and len(degradation) <= 1 and (bool(degradation) if value["health"] == "degraded" else not degradation)
            and (value["activeTransactionId"] is None or type(value["activeTransactionId"]) is str and 0 < len(value["activeTransactionId"]) <= 128)
            and _timestamp(value["updatedAt"]))


def _context_valid(value):
    if type(value) is not dict or set(value) != {"previousState", "requestedTarget"}:
        return False
    previous = value["previousState"]
    if type(previous) is not dict or set(previous) != {"committedTarget", "preferredRegion", "health", "degradation"}:
        return False
    return _target(value["requestedTarget"]) and _state_valid({"schemaVersion": 2, "revision": 0, **previous, "activeTransactionId": None, "updatedAt": "2026-01-01T00:00:00.000Z"})


def _journal_valid(value):
    if not (type(value) is dict and set(value).issubset({"transactionId", "kind", "steps", "context"})
            and {"transactionId", "kind", "steps"}.issubset(value)
            and type(value["transactionId"]) is str and 0 < len(value["transactionId"]) <= 128
            and type(value["kind"]) is str and value["kind"] in ("protect", "restore") and type(value["steps"]) is list):
        return False
    ids = []
    for step in value["steps"]:
        if (type(step) is not dict or not {"id", "phase"}.issubset(step) or not set(step).issubset({"id", "phase", "original", "desired"})
                or type(step["id"]) is not str or step["id"] not in ("environment", "backup_cleanup")
                or type(step["phase"]) is not str or step["phase"] not in PHASES):
            return False
        ids.append(step["id"])
        if any(not _stored(step[key]) for key in ("original", "desired") if key in step):
            return False
    return (len(set(ids)) == len(ids) and
            ("context" not in value or _context_valid(value["context"]) and
             (value["context"]["requestedTarget"] is not None if value["kind"] == "protect" else value["context"]["requestedTarget"] is None)))


def _values_valid(value):
    if type(value) is not dict or set(value) != {"transactionId", "values"} or type(value["transactionId"]) is not str or type(value["values"]) is not dict:
        return False
    return all(key in ("environment", "backup_cleanup") and type(entry) is dict and set(entry).issubset({"original", "desired"}) and all(_stored(child) for child in entry.values()) for key, entry in value["values"].items())


def _backup_valid(value):
    return (type(value) is dict and set(value) == {"schemaVersion", "snapshotId", "active", "envPath", "bashrcPath", "bashrcExisted", "values"}
            and _v1(value["schemaVersion"]) and type(value["snapshotId"]) is str and 0 < len(value["snapshotId"]) <= 128
            and type(value["active"]) is bool and type(value["bashrcExisted"]) is bool
            and all(type(value[key]) is str and Path(value[key]).is_absolute() for key in ("envPath", "bashrcPath"))
            and type(value["values"]) is dict and set(value["values"]) == {"environment"}
            and _stored(value["values"]["environment"]) and value["values"]["environment"]["value"] == EMPTY_ENV)


def shell_quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def environment_script(profile):
    if not valid_environment(profile):
        raise _fail("STATE_INVALID", "Invalid Linux environment profile")
    return HEADER + "".join(f"export {key}={shell_quote(profile[key] or '')}\n" for key in KEYS)


def parse_environment(contents):
    lines = contents.split("\n")
    value = {}
    for index, key in enumerate(KEYS, 1):
        prefix = f"export {key}='"
        if len(lines) <= index or not lines[index].startswith(prefix) or not lines[index].endswith("'"):
            raise _fail("CONFIG_CONFLICT", "FUCC env.sh 已被修改；请保留文件并修复冲突。")
        value[key] = lines[index][len(prefix):-1].replace("'\"'\"'", "'")
    if not valid_environment(value) or contents != environment_script(value):
        raise _fail("CONFIG_CONFLICT", "FUCC env.sh 已被修改或不是工具拥有的配置。")
    return value


def list_linux_locales():
    try:
        return subprocess.run(["/usr/bin/locale", "-a"], check=True, text=True, capture_output=True, timeout=5).stdout.strip().splitlines()
    except (OSError, subprocess.SubprocessError) as exc:
        raise _fail("LOCALE_UNAVAILABLE", "无法列出已安装 Locale；请检查 locale 命令。") from exc


def _normalize_locale(value):
    return value.lower().replace("-", "").replace("_", "")


def _state_result(root):
    root = Path(root)
    # Unknown schemas are a refusal, never a reason to silently resurrect an
    # older generation. Known corrupt generations may be read as degraded.
    schema = STATE_SCHEMA
    for path in (root / "state.json", root / "state.json.prev"):
        snapshot = read_snapshot(path)
        if snapshot["text"] is None:
            continue
        try:
            envelope = json.loads(snapshot["text"])
        except ValueError:
            envelope = None
        if type(envelope) is dict:
            payload = envelope.get("payload")
            schema = envelope.get("schema")
            expected = 1 if schema == STATE_SCHEMA else 2 if schema == STATE_SCHEMA_V2 else None
            version = payload.get("schemaVersion", expected) if type(payload) is dict else expected
            if expected is None or type(version) not in (int, float) or version != expected:
                raise _fail("STATE_INVALID", "A newer or unknown protection state schema cannot fall back to a predecessor")
            break
    return read_checked(root, root / "state.json", schema,
                        lambda value: _state_valid(value) and value["schemaVersion"] == (2 if schema == STATE_SCHEMA_V2 else 1))


def read_persist_state(root):
    """Read the committed preset without creating state or touching backups."""
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts:
        raise _fail("STATE_INVALID", "Persist root must be an absolute literal path")
    try:
        info = root.lstat()
    except FileNotFoundError:
        return None, False
    if not stat.S_ISDIR(info.st_mode):
        raise _fail("STATE_INVALID", "Persist root must be a real directory")
    result = _state_result(root)
    return (None, False) if result["kind"] == "missing" else (result["payload"], result.get("degraded", False))


class BashRuntime:
    def __init__(self, environment=None, list_locales=None, root=None):
        self.environment = dict(os.environ if environment is None else environment)
        self.root = Path(root) if root is not None else persist_root(self.environment)
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise _fail("STATE_INVALID", "Persist root must be an absolute literal path")
        self.env_path = xdg_directory(self.environment, "XDG_CONFIG_HOME") / "fucc" / "env.sh"
        self.bashrc_path = home_directory(self.environment) / ".bashrc"
        self.paths = {"env": str(self.env_path), "bashrc": str(self.bashrc_path)}
        self.hook = f"\n{BEGIN}\nif [ -r {shell_quote(str(self.env_path))} ]; then . {shell_quote(str(self.env_path))}; fi\n{END}\n"
        self.list_locales = list_locales or list_linux_locales
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        existing = self._state_result()
        if existing["kind"] == "missing":
            # Read backup and journal first: lost state must never become a new baseline.
            backup = self._read_backup()
            journal, _ = self._read_journal()
            if backup and backup["active"] or journal is not None:
                raise _fail("RECOVERY_CONTEXT_INVALID", "状态丢失但仍有恢复记录；保留数据，不得重新初始化。", 21)
            with self._lock():
                if self._state_result()["kind"] == "missing":
                    self._write_state({"schemaVersion": 1, "revision": 0, "committedTarget": None, "preferredRegion": "us", "health": "healthy", "degradation": [], "activeTransactionId": None, "updatedAt": self._now()})
        self._read_backup()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _lock(self, recover=False):
        return MutationLock(self.root, str(self.root) + "\0mutation-root", recover=recover)

    def _state_result(self):
        return _state_result(self.root)

    def _read_state(self):
        result = self._state_result()
        if result["kind"] != "ok":
            raise _fail("STATE_MISSING", "Protection state is missing")
        return result["payload"], result.get("degraded", False)

    def _write_state(self, state):
        schema = STATE_SCHEMA_V2 if state["schemaVersion"] == 2 else STATE_SCHEMA
        write_checked(self.root, self.root / "state.json", schema, state, _state_valid)
        if schema == STATE_SCHEMA_V2:
            # Both generations must carry the gate before dynamic settings
            # reach disk. A damaged current file must not expose a v1 baseline
            # to an older executable through its predecessor fallback.
            write_checked(self.root, self.root / "state.json", schema, state, _state_valid)

    def _commit(self, state, **changes):
        current, degraded = self._read_state()
        if degraded:
            raise _fail("RECOVERY_REQUIRED", "Protection state was recovered from a predecessor and cannot be committed", 21)
        if current["revision"] != state["revision"]:
            raise _fail("REVISION_MISMATCH", "Protection state revision does not match")
        next_state = {**state, **deepcopy(changes), "revision": state["revision"] + 1, "updatedAt": self._now()}
        self._write_state(next_state)
        return next_state

    def _read_backup(self):
        try:
            result = read_checked(self.root, self.root / "linux-backup.json", BACKUP_SCHEMA, _backup_valid)
        except FuccError as exc:
            raise _fail("BACKUP_CORRUPT", "Linux 原始快照校验失败；请保留恢复数据。") from exc
        if result["kind"] == "missing":
            return None
        snapshot = result["payload"]
        if snapshot["envPath"] != str(self.env_path) or snapshot["bashrcPath"] != str(self.bashrc_path):
            raise _fail("CONFIG_CONFLICT", "恢复数据属于不同的 HOME / XDG_CONFIG_HOME；请使用启用保护时的目录。")
        # Never mutate a fallback snapshot: it may be an obsolete identity.
        if result.get("degraded"):
            raise _fail("BACKUP_CORRUPT", "Linux 原始快照来自旧代；请保留恢复数据。")
        return snapshot

    def _create_backup(self, values):
        current = self._read_backup()
        if current and current["active"]:
            if current["values"] != values:
                raise _fail("BACKUP_CONFLICT", "Existing daily backup does not match the current daily authorities")
            return current
        snapshot = {"schemaVersion": 1, "snapshotId": str(uuid.uuid4()), "active": True,
                    "envPath": str(self.env_path), "bashrcPath": str(self.bashrc_path),
                    "bashrcExisted": read_snapshot(self.bashrc_path)["text"] is not None, "values": values}
        write_checked(self.root, self.root / "linux-backup.json", BACKUP_SCHEMA, snapshot, _backup_valid)
        return snapshot

    def _retire_backup(self, snapshot):
        current = self._read_backup()
        if current is None or current["snapshotId"] != snapshot["snapshotId"] or current["values"] != snapshot["values"]:
            raise _fail("CONFIG_CONFLICT", "Linux 原始快照身份已改变，拒绝清理。")
        if current["active"]:
            if not current["bashrcExisted"]:
                rc = read_snapshot(self.bashrc_path)
                if rc["text"] == "":
                    replace_snapshot(self.bashrc_path, {"text": None, "mode": None}, rc)
            write_checked(self.root, self.root / "linux-backup.json", BACKUP_SCHEMA, {**current, "active": False}, _backup_valid)

    def _read_journal(self):
        path = self.root / "transaction-journal.json"
        result = read_checked(self.root, path, JOURNAL_SCHEMA, _journal_valid)
        if result["kind"] == "missing":
            return None, False
        journal = result["payload"]
        if journal["steps"] and not any("original" in step or "desired" in step for step in journal["steps"]):
            values_result = read_checked(self.root, str(path) + ".values", VALUES_SCHEMA, _values_valid)
            if values_result["kind"] != "ok" or values_result["payload"]["transactionId"] != journal["transactionId"]:
                raise _fail("STATE_INVALID", "Journal values snapshot is missing or belongs to a different transaction")
            values = values_result["payload"]["values"]
            journal = {**journal, "steps": [{**step, **values.get(step["id"], {})} for step in journal["steps"]]}
        if not _journal_valid(journal):
            raise _fail("STATE_INVALID", "Transaction journal has invalid values")
        return journal, result.get("degraded", False)

    def _plan(self, kind, state, target, steps):
        journal = {"transactionId": str(uuid.uuid4()), "kind": kind, "steps": [{**step, "phase": "planned"} for step in steps],
                   "context": {"previousState": {key: deepcopy(state[key]) for key in ("committedTarget", "preferredRegion", "health", "degradation")}, "requestedTarget": target}}
        values = {"transactionId": journal["transactionId"], "values": {step["id"]: {key: step[key] for key in ("original", "desired") if key in step} for step in steps}}
        write_checked(self.root, self.root / "transaction-journal.json.values", VALUES_SCHEMA, values, _values_valid)
        self._write_journal(journal)
        return journal

    def _write_journal(self, journal):
        stripped = {**journal, "steps": [{"id": step["id"], "phase": step["phase"]} for step in journal["steps"]]}
        write_checked(self.root, self.root / "transaction-journal.json", JOURNAL_SCHEMA, stripped, _journal_valid)

    def _transition(self, journal, id, phase):
        step = next((step for step in journal["steps"] if step["id"] == id), None)
        if step is None or phase not in TRANSITIONS[step["phase"]]:
            raise _fail("STATE_INVALID", "Illegal journal phase transition")
        step["phase"] = phase
        # Migrate an old embedded-values journal only after persisting its values.
        values = {"transactionId": journal["transactionId"], "values": {entry["id"]: {key: entry[key] for key in ("original", "desired") if key in entry} for entry in journal["steps"]}}
        existing = read_checked(self.root, self.root / "transaction-journal.json.values", VALUES_SCHEMA, _values_valid)
        if existing["kind"] == "missing" or existing["payload"]["transactionId"] != journal["transactionId"]:
            write_checked(self.root, self.root / "transaction-journal.json.values", VALUES_SCHEMA, values, _values_valid)
        self._write_journal(journal)

    def _mark_failed(self, journal, id):
        step = next((step for step in journal["steps"] if step["id"] == id), None)
        if step and step["phase"] != "recovery_required":
            try:
                self._transition(journal, id, "recovery_required")
            except Exception:
                pass

    def _remove_hook(self, rc):
        if BEGIN not in rc and END not in rc:
            return rc
        if self.hook not in rc or rc.count(BEGIN) != 1 or rc.count(END) != 1:
            raise _fail("CONFIG_CONFLICT", "FUCC .bashrc 标记块已被修改或重复；请保留文件并修复冲突。")
        return rc.replace(self.hook, "", 1)

    def _read_environment(self):
        env = read_snapshot(self.env_path)
        rc = read_snapshot(self.bashrc_path)
        self._remove_hook(rc["text"] or "")
        return _store(EMPTY_ENV if env["text"] is None else parse_environment(env["text"]))

    def _write_environment(self, value):
        if not _stored(value):
            raise _fail("STATE_INVALID", "Invalid Linux environment profile")
        profile = value["value"]
        rc = read_snapshot(self.bashrc_path)
        base = self._remove_hook(rc["text"] or "")
        previous = read_snapshot(self.env_path)
        if previous["text"] is not None:
            current = _store(parse_environment(previous["text"]))
            state, _ = self._read_state()
            journal, _ = self._read_journal()
            allowed = [_store(EMPTY_ENV)]
            if state["committedTarget"] is not None:
                allowed.append(_store(_target_environment(state["committedTarget"])))
            if journal:
                step = next((step for step in journal["steps"] if step["id"] == "environment"), {})
                allowed.extend(step[key] for key in ("original", "desired") if _stored(step.get(key)))
            if current not in allowed:
                raise _fail("CONFIG_CONFLICT", "env.sh 包含事务未记录的用户修改，拒绝覆盖。")
        empty = profile == EMPTY_ENV
        if empty:
            if base != (rc["text"] or ""):
                replace_snapshot(self.bashrc_path, {"text": base, "mode": rc["mode"] if rc["mode"] is not None else 0o600}, rc)
            if previous["text"] is not None:
                replace_snapshot(self.env_path, {"text": None, "mode": None}, previous)
        else:
            replace_snapshot(self.env_path, {"text": environment_script(profile), "mode": previous["mode"] if previous["mode"] is not None else 0o600}, previous)
            if self.hook not in (rc["text"] or ""):
                replace_snapshot(self.bashrc_path, {"text": base + self.hook, "mode": rc["mode"] if rc["mode"] is not None else 0o600}, rc)
        self._assert_environment(profile)

    def _assert_environment(self, expected):
        current = self._read_environment()
        rc = read_snapshot(self.bashrc_path)["text"] or ""
        if current != _store(expected) or ((expected["TZ"] is not None) != (self.hook in rc)):
            raise _fail("CONFIG_CONFLICT", "FUCC 受管配置与已提交状态不一致；保留备份并检查 env.sh / .bashrc 冲突。")

    @staticmethod
    def _decision(state, journal):
        if journal and not all(step["phase"] in ("verified", "compensated") for step in journal["steps"]):
            return {"kind": "protect_compensation" if journal["kind"] == "protect" else "restore_convergence", "transactionId": journal["transactionId"]}
        if state["activeTransactionId"] is not None:
            return {"kind": "state_reconciliation", "transactionId": state["activeTransactionId"]}
        return {"kind": "none"}

    def status(self):
        state, degraded = self._read_state()
        journal, _ = self._read_journal()
        transaction = self._decision(state, journal)
        health = "recovery_required" if degraded or transaction["kind"] != "none" else state["health"]
        platform = {"os": "linux", "scope": "bash-environment", "managed": list(KEYS),
                    "unsupported": ["system_timezone", "browser_policies", "deep", "fonts", "desktop"],
                    "systemTimezone": self._system_timezone(), "systemTimezoneManaged": False, "browserPoliciesManaged": False}
        aligned = False
        try:
            configured = self._read_environment()["value"]
            platform.update({"envFile": str(self.env_path), "bashrc": str(self.bashrc_path), "shellHookInstalled": self.hook in (read_snapshot(self.bashrc_path)["text"] or ""),
                             "configured": configured, "currentProcess": {key: self.environment.get(key) for key in KEYS},
                             "currentProcessAligned": all(configured[key] is not None and configured[key] == self.environment.get(key) for key in KEYS)})
            self._assert_environment(EMPTY_ENV if state["committedTarget"] is None else _target_environment(state["committedTarget"]))
            aligned = True
        except Exception as exc:
            platform["conflict"] = str(exc)
        platform["configurationAligned"] = aligned
        recovery = health == "recovery_required" or transaction["kind"] != "none"
        platform["activation"] = (f"FUCC 配置存在冲突；请先修复 env.sh / .bashrc，再{'运行 fucc persist recover' if recovery else '重新查看状态'}。" if not aligned else
                                  "存在未完成事务；请先运行 fucc persist recover，再重新查看状态。" if recovery else
                                  f"FUCC 持久化尚未开启；运行 fucc persist on --region {state['preferredRegion']}，成功后新开 Bash 终端。" if state["committedTarget"] is None else
                                  f"新开 Bash 终端，或运行 . {shell_quote(str(self.env_path))}；已有终端和桌面应用不会自动更新。")
        return {"mode": "daily" if state["committedTarget"] is None else "standard", "target": state["committedTarget"],
                "preferredRegion": state["preferredRegion"], "health": "degraded" if health == "healthy" and not aligned else health,
                "degradation": state["degradation"], "transaction": transaction, "revision": state["revision"], "updatedAt": state["updatedAt"], "platform": platform}

    @staticmethod
    def _system_timezone():
        try:
            actual = Path("/etc/localtime").resolve(strict=True)
            name = str(actual.relative_to("/usr/share/zoneinfo"))
            for prefix in ("posix/", "right/"):
                if name.startswith(prefix):
                    name = name[len(prefix):]
            return name if name and name != "localtime" else None
        except (OSError, ValueError):
            try:
                result = subprocess.run(["/usr/bin/timedatectl", "show", "--property=Timezone", "--value"],
                                        capture_output=True, text=True, check=True, timeout=5,
                                        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
                return result.stdout.strip() or None
            except (OSError, subprocess.SubprocessError):
                return None

    def _ready(self, state, degraded, journal):
        if degraded or state["health"] == "recovery_required" or self._decision(state, journal)["kind"] != "none":
            raise _fail("RECOVERY_REQUIRED", "The previous persist transaction must be recovered first", 21)

    def set_preferred_region(self, region):
        if not _region(region):
            raise _fail("INVALID_REGION", "Invalid region", 10)
        with self._lock():
            state, degraded = self._read_state()
            journal, _ = self._read_journal()
            self._ready(state, degraded, journal)
            if state["committedTarget"] is not None:
                raise _fail("REGION_SET_REQUIRES_DAILY", "Preferred region can only change in daily mode; use fucc persist on --region")
            if state["preferredRegion"] == region:
                return {"kind": "noop", "preferredRegion": region}
            self._commit(state, preferredRegion=region)
            return {"kind": "updated", "preferredRegion": region}

    def protect(self, target):
        if type(target) is str:
            target = {"mode": "standard", "region": target}
        if (type(target) is dict and set(target) == {"mode", "region", "timezone"}
                and target["mode"] == "standard" and _region(target["region"])):
            if type(target["timezone"]) is not str:
                raise _fail("INVALID_TIMEZONE", "时区必须是受支持的 IANA 名称", 10)
            canonical = get_target_region(target["region"], target["timezone"])["timezone"]
            target = {"mode": "standard", "region": target["region"]}
            if canonical != TARGET_REGIONS[target["region"]]["timezone"]:
                target["timezone"] = canonical
        if not _target(target) or target is None:
            raise _fail("UNSUPPORTED_FEATURE", "Ubuntu 仅支持 standard（Bash 环境变量）。", 10)
        if self.environment.get("SHELL") and not self.environment["SHELL"].endswith("/bash"):
            raise _fail("UNSUPPORTED_FEATURE", "持久化仅支持 Bash；其他 Shell 请使用 fucc run。", 10)
        locale = TARGET_REGIONS[target["region"]]["lang"]
        if not any(_normalize_locale(available) == _normalize_locale(locale) for available in self.list_locales()):
            raise _fail("LOCALE_UNAVAILABLE", f"未安装 Locale {locale}；请先运行 sudo locale-gen {locale}，再重试。", 10)
        with self._lock():
            initial, degraded = self._read_state()
            old_journal, _ = self._read_journal()
            self._ready(initial, degraded, old_journal)
            expected = EMPTY_ENV if initial["committedTarget"] is None else _target_environment(initial["committedTarget"])
            self._assert_environment(expected)
            original = self._read_environment()
            desired = _store(_target_environment(target))
            if initial["committedTarget"] == target and original == desired:
                return {"kind": "noop", "degraded": []}
            if "timezone" in target:
                initial = self._commit(initial, schemaVersion=2)
            if initial["committedTarget"] is None:
                self._create_backup({"environment": original})
            else:
                backup = self._read_backup()
                if backup is None or not backup["active"]:
                    raise _fail("BACKUP_REQUIRED", "Protected mode requires the immutable daily backup")
            journal = self._plan("protect", initial, target, [{"id": "environment", "original": original, "desired": desired}])
            state = self._commit(initial, activeTransactionId=journal["transactionId"])
            try:
                self._transition(journal, "environment", "applying")
                self._write_environment(desired)
                self._transition(journal, "environment", "verified")
            except Exception:
                try:
                    phase = journal["steps"][0]["phase"]
                    if phase == "planned":
                        self._transition(journal, "environment", "compensated")
                    else:
                        self._transition(journal, "environment", "compensating")
                        self._write_environment(original)
                        self._transition(journal, "environment", "compensated")
                    kind = "compensated"
                except Exception:
                    self._mark_failed(journal, "environment")
                    kind = "recovery_required"
                self._commit(state, activeTransactionId=None, health="recovery_required" if kind == "recovery_required" else initial["health"], degradation=[] if kind == "recovery_required" else initial["degradation"])
                return {"kind": kind, "degraded": []}
            self._commit(state, committedTarget=target, preferredRegion=target["region"], health="healthy", degradation=[], activeTransactionId=None)
            return {"kind": "committable", "degraded": []}

    def restore(self):
        with self._lock():
            state, degraded = self._read_state()
            existing, _ = self._read_journal()
            self._ready(state, degraded, existing)
            if state["committedTarget"] is None:
                return {"kind": "noop"}
            self._assert_environment(_target_environment(state["committedTarget"]))
            backup = self._read_backup()
            if backup is None or not backup["active"]:
                raise _fail("BACKUP_REQUIRED", "Protected mode cannot be restored without the immutable daily backup")
            journal = self._plan("restore", state, None, [{"id": "environment", "original": self._read_environment(), "desired": backup["values"]["environment"]}, {"id": "backup_cleanup"}])
            state = self._commit(state, activeTransactionId=journal["transactionId"])
            return self._finish_restore(state, journal, backup, recovering=False)

    def _finish_restore(self, state, journal, backup, recovering):
        step = next((x for x in journal["steps"] if x["id"] == "environment"), None)
        cleanup = next((x for x in journal["steps"] if x["id"] == "backup_cleanup"), None)
        if step is None or cleanup is None or step["phase"] in ("compensating", "compensated"):
            raise _fail("RECOVERY_CONTEXT_INVALID", "Restore journal lacks required steps")
        if backup and backup["active"]:
            try:
                desired = backup["values"]["environment"]
                if step["phase"] not in ("applying", "verified"):
                    self._transition(journal, "environment", "applying")
                self._write_environment(desired)
                if step["phase"] != "verified":
                    self._transition(journal, "environment", "verified")
            except Exception:
                self._mark_failed(journal, "environment")
                self._commit(state, health="recovery_required", degradation=[], activeTransactionId=None)
                return {"kind": "recovery_required", "failed": ["environment"]}
        elif state["committedTarget"] is not None or cleanup["phase"] not in ("applying", "verified", "recovery_required"):
            raise _fail("BACKUP_REQUIRED", "Restore recovery requires the immutable daily backup")
        preferred = journal["context"]["previousState"]["preferredRegion"]
        if state["committedTarget"] is not None or state["activeTransactionId"] != journal["transactionId"]:
            state = self._commit(state, committedTarget=None, preferredRegion=preferred, health="healthy", degradation=[], activeTransactionId=journal["transactionId"])
        try:
            if cleanup["phase"] == "verified" and backup and backup["active"]:
                raise _fail("RECOVERY_CONTEXT_INVALID", "Restore journal says backup cleanup verified but backup still exists")
            if cleanup["phase"] != "verified":
                if cleanup["phase"] != "applying":
                    self._transition(journal, "backup_cleanup", "applying")
                if backup and backup["active"]:
                    self._retire_backup(backup)
                self._transition(journal, "backup_cleanup", "verified")
        except Exception:
            self._mark_failed(journal, "backup_cleanup")
            self._commit(state, health="recovery_required", degradation=[], activeTransactionId=journal["transactionId"])
            return {"kind": "recovery_required", "failed": ["backup_cleanup"]}
        self._commit(state, committedTarget=None, preferredRegion=preferred, health="healthy", degradation=[], activeTransactionId=None)
        return {"kind": "recovered", "failed": []} if recovering else {"kind": "restored"}

    def recover(self):
        with self._lock(recover=True):
            state, degraded = self._read_state()
            journal, journal_degraded = self._read_journal()
            if degraded:
                raise _fail("RECOVERY_CONTEXT_INVALID", "Protection state is from a predecessor; preserve evidence for manual recovery", 21)
            if journal is None:
                if state["activeTransactionId"] is None and state["health"] != "recovery_required":
                    return {"kind": "noop", "failed": []}
                raise _fail("RECOVERY_CONTEXT_INVALID", "Recovery journal is missing", 21)
            if self._decision(state, journal)["kind"] == "none" and state["health"] != "recovery_required":
                return {"kind": "noop", "failed": []}
            if not _context_valid(journal.get("context")) or state["activeTransactionId"] not in (None, journal["transactionId"]):
                raise _fail("RECOVERY_CONTEXT_INVALID", "Recovery journal context does not match state", 21)
            if journal["kind"] == "restore":
                return self._finish_restore(state, journal, self._read_backup(), recovering=True)
            # Protect recovery always rolls back to the last committed profile.
            if set(step["id"] for step in journal["steps"]) != {"environment"}:
                raise _fail("RECOVERY_CONTEXT_INVALID", "Protect journal contains unsupported steps", 21)
            failed = []
            for step in reversed(journal["steps"]):
                if step["phase"] == "compensated":
                    continue
                try:
                    if step["phase"] == "planned" and not journal_degraded:
                        self._transition(journal, step["id"], "compensated")
                        continue
                    if not _stored(step.get("original")):
                        raise _fail("RECOVERY_CONTEXT_INVALID", "Original environment is missing")
                    if step["phase"] == "planned":
                        self._transition(journal, step["id"], "recovery_required")
                    if step["phase"] != "compensating":
                        self._transition(journal, step["id"], "compensating")
                    self._write_environment(step["original"])
                    self._transition(journal, step["id"], "compensated")
                except Exception:
                    failed.append(step["id"])
                    self._mark_failed(journal, step["id"])
            previous = journal["context"]["previousState"]
            self._commit(state, **{**previous, "health": "recovery_required" if failed else previous["health"], "degradation": [] if failed else previous["degradation"], "activeTransactionId": None})
            return {"kind": "recovery_required" if failed else "recovered", "failed": failed}
