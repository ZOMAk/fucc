"""Checked state files and process locks compatible with FUCC's v1 format."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Callable, Iterator
import uuid

import jcs

from .errors import FuccError


def _error(message: str, code: str = "STATE_INVALID", exit_code: int = 24) -> FuccError:
    return FuccError(message, code=code, exit_code=exit_code)


def _json_value(value: Any, ancestors: set[int]) -> None:
    if value is None or type(value) in (bool, str):
        if type(value) is str:
            # JCS cannot encode lone UTF-16 surrogates. Refuse them explicitly.
            value.encode("utf-8", errors="strict")
        return
    if type(value) in (int, float):
        if not math.isfinite(value) or (type(value) is int and abs(value) > 2**53):
            raise ValueError("Unsupported JSON number")
        return
    if type(value) not in (list, dict) or id(value) in ancestors:
        raise ValueError("Only acyclic plain JSON values are supported")
    ancestors.add(id(value))
    try:
        if type(value) is dict:
            for key, child in value.items():
                if type(key) is not str:
                    raise ValueError("JSON object keys must be strings")
                key.encode("utf-8", errors="strict")
                _json_value(child, ancestors)
        else:
            for child in value:
                _json_value(child, ancestors)
    finally:
        ancestors.remove(id(value))


def canonical_json(value: Any) -> str:
    """ECMAScript-number, UTF-16-key-sorted canonical JSON without Node."""
    try:
        _json_value(value, set())
        return jcs.canonicalize(value).decode("utf-8")
    except Exception as exc:
        raise _error("Value is not supported canonical JSON") from exc


def is_safe_json(value: Any) -> bool:
    try:
        canonical_json(value)
        return True
    except FuccError:
        return False


def clone_json(value: Any) -> Any:
    return _parse_json(canonical_json(value))


def _parse_json(text: str) -> Any:
    def number(text: str) -> int | float:
        value = int(text)
        return value if abs(value) <= 2**53 else float(text)

    def invalid_constant(_: str) -> None:
        raise ValueError("Non-finite JSON value")

    return json.loads(text, parse_int=number, parse_constant=invalid_constant)


def _serialize(schema: str, payload: Any) -> str:
    if type(schema) is not str or not schema:
        raise _error("Checked file schema must be nonempty")
    body = {"envelopeVersion": 1, "schema": schema, "algorithm": "sha256", "payload": payload}
    checksum = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    return canonical_json({**body, "checksum": checksum}) + "\n"


def _decode(text: str, schema: str, validator: Callable[[Any], bool] | None) -> Any:
    value = _parse_json(text)
    if (type(value) is not dict or set(value) != {"envelopeVersion", "schema", "algorithm", "payload", "checksum"}
            or type(value["envelopeVersion"]) not in (int, float) or value["envelopeVersion"] != 1
            or value["algorithm"] != "sha256" or value["schema"] != schema
            or type(value["checksum"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", value["checksum"])):
        raise ValueError("Invalid checked file envelope")
    body = {key: value[key] for key in ("envelopeVersion", "schema", "algorithm", "payload")}
    checksum = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    if not hmac.compare_digest(value["checksum"], checksum):
        raise ValueError("Checked file checksum does not match")
    if validator is not None and not validator(value["payload"]):
        raise ValueError("Checked file payload schema is invalid")
    return value["payload"]


def checked_root(root: str | Path) -> Path:
    text = os.fspath(root)
    if not os.path.isabs(text) or ".." in re.split(r"[\\/]+", text):
        raise _error("State root must be an absolute literal path")
    return Path(os.path.abspath(text))


def _target(root: str | Path, path: str | Path) -> tuple[Path, Path]:
    root = checked_root(root)
    text = os.fspath(path)
    if not os.path.isabs(text) or ".." in re.split(r"[\\/]+", text):
        raise _error("State path must be an absolute literal descendant of its root")
    path = Path(os.path.abspath(text))
    if root == path or root not in path.parents:
        raise _error("State path must be a strict descendant of its root")
    return root, path


class _Boundary:
    def __init__(self, root: str | Path, path: str | Path):
        self.root, self.path = _target(root, path)
        self.fds: list[int] = []

    def __enter__(self) -> "_Boundary":
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            fd = os.open(self.root, flags)
            self.fds.append(fd)
            self.root_identity = self._identity(os.fstat(fd))
            self.directories = [(self.root, self.root_identity)]
            cursor = self.root
            for part in self.path.relative_to(self.root).parts[:-1]:
                fd = os.open(part, flags, dir_fd=fd)
                self.fds.append(fd)
                cursor = cursor / part
                self.directories.append((cursor, self._identity(os.fstat(fd))))
            self.parent_fd = fd
            self.name = self.path.name
            self.parent_identity = self._identity(os.fstat(fd))
            self.check()
            self.check_leaf(self.name)
            return self
        except Exception as exc:
            self.close()
            if isinstance(exc, FuccError):
                raise
            raise _error("State file boundary could not be validated") from exc

    @staticmethod
    def _identity(info: os.stat_result) -> tuple[int, int]:
        return info.st_dev, info.st_ino

    def check(self) -> None:
        # dirfd traversal keeps operations attached to the checked directories;
        # identities also detect renaming/replacement of their public paths.
        for path, expected in self.directories:
            try:
                info = os.lstat(path)
            except OSError as exc:
                raise _error("State boundary cannot be revalidated") from exc
            if not stat.S_ISDIR(info.st_mode) or self._identity(info) != expected:
                raise _error("State boundary identity changed during the operation")

    def check_leaf(self, name: str) -> None:
        try:
            info = os.stat(name, dir_fd=self.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode):
            raise _error("State path is not a regular file")

    def read(self, name: str) -> str:
        self.check()
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=self.parent_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise _error("State file is not regular")
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
                result = stream.read()
        finally:
            os.close(fd)
        self.check()
        return result

    def create(self, name: str, text: str, cleanup_on_failure: bool = False) -> bool:
        self.check()
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=self.parent_fd)
        except FileExistsError:
            return False
        identity = self._identity(os.fstat(fd))
        try:
            try:
                with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(fd)
            finally:
                os.close(fd)
            self.check()
            os.fsync(self.parent_fd)
        except Exception:
            if cleanup_on_failure:
                try:
                    self.check()
                    info = os.stat(name, dir_fd=self.parent_fd, follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and self._identity(info) == identity:
                        os.unlink(name, dir_fd=self.parent_fd)
                except (OSError, FuccError):
                    pass
            raise
        return True

    def replace(self, name: str, text: str) -> None:
        self.check_leaf(name)
        temporary = f"{name}.fucc-tmp-{os.getpid()}-{uuid.uuid4()}"
        owned = False
        try:
            owned = self.create(temporary, text, cleanup_on_failure=True)
            if not owned:
                raise _error("Cannot create checked file temporary")
            self.check()
            self.check_leaf(name)
            os.replace(temporary, name, src_dir_fd=self.parent_fd, dst_dir_fd=self.parent_fd)
            owned = False
            self.check()
            os.fsync(self.parent_fd)
        finally:
            if owned:
                try:
                    self.check()
                    os.unlink(temporary, dir_fd=self.parent_fd)
                except (OSError, FuccError):
                    pass

    def remove(self, name: str) -> None:
        self.check()
        self.check_leaf(name)
        os.unlink(name, dir_fd=self.parent_fd)
        self.check()
        os.fsync(self.parent_fd)

    def close(self) -> None:
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds.clear()

    def __exit__(self, *_: Any) -> None:
        self.close()


def validate_boundary(root: str | Path, path: str | Path) -> None:
    with _Boundary(root, path):
        pass


def _generation(boundary: _Boundary, name: str, schema: str,
                validator: Callable[[Any], bool] | None) -> dict[str, Any]:
    boundary.check_leaf(name)
    try:
        text = boundary.read(name)
    except FileNotFoundError:
        return {"kind": "missing"}
    except UnicodeError:
        return {"kind": "corrupt"}
    except OSError as exc:
        return {"kind": "io", "cause": exc}
    try:
        return {"kind": "valid", "payload": _decode(text, schema, validator)}
    except Exception:
        return {"kind": "corrupt"}


def read_checked(root: str | Path, path: str | Path, schema: str,
                 validator: Callable[[Any], bool] | None = None) -> dict[str, Any]:
    with _Boundary(root, path) as boundary:
        current = _generation(boundary, boundary.name, schema, validator)
        if current["kind"] == "valid":
            return {"kind": "ok", "payload": current["payload"], "source": "current", "degraded": False}
        previous = _generation(boundary, boundary.name + ".prev", schema, validator)
        if previous["kind"] == "valid":
            return {"kind": "ok", "payload": previous["payload"], "source": "previous", "degraded": True}
        if current["kind"] == previous["kind"] == "missing":
            return {"kind": "missing"}
        raise _error("No valid checked state generation is available")


def write_checked(root: str | Path, path: str | Path, schema: str, payload: Any,
                  validator: Callable[[Any], bool] | None = None) -> None:
    try:
        if validator is not None and not validator(payload):
            raise _error("Checked state payload validation failed")
        serialized = _serialize(schema, payload)
        with _Boundary(root, path) as boundary:
            current = _generation(boundary, boundary.name, schema, validator)
            if current["kind"] == "io":
                raise _error("Current checked state cannot be read") from current["cause"]
            if current["kind"] == "valid":
                boundary.replace(boundary.name + ".prev", _serialize(schema, current["payload"]))
            boundary.replace(boundary.name, serialized)
    except FuccError:
        raise
    except Exception as exc:
        raise _error("Durable checked state write failed") from exc


def process_start_ticks(pid: int) -> int | None:
    """Keep the legacy startedAtMs field's actual Linux start-tick identity."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return None
    fields = text[text.rfind(")") + 2:].split()
    if len(fields) < 20 or not fields[19].isdigit():
        raise _error("Cannot establish process identity")
    ticks = int(fields[19])
    if not 0 < ticks <= 2**53 - 1:
        raise _error("Cannot establish process identity")
    return ticks


def _lock_record(text: str) -> dict[str, Any] | None:
    try:
        value = _parse_json(text)
        if (type(value) is not dict or type(value.get("pid")) is not int
                or type(value.get("startedAtMs")) not in (int, float)
                or type(value.get("heartbeatAtMs")) not in (int, float)
                or not math.isfinite(value["startedAtMs"]) or not math.isfinite(value["heartbeatAtMs"])
                or type(value.get("lockId")) is not str):
            return None
        return {key: value[key] for key in ("pid", "startedAtMs", "heartbeatAtMs", "lockId")}
    except (ValueError, TypeError, OverflowError):
        return None


def _record_text(record: dict[str, Any]) -> str:
    return json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"


_claim_turns_guard = threading.Lock()
_claim_turns: dict[str, threading.RLock] = {}


class MutationLock:
    """Exclusive PID/start-tick lock with the legacy .claim CAS protocol."""
    def __init__(self, root: str | Path, lock_key: str, recover: bool = False):
        self.root = checked_root(root)
        self.path = self.root / "locks" / (hashlib.sha256(lock_key.encode("utf-8")).hexdigest() + ".lock")
        self.recover = recover
        self.record: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._heartbeat_error: Exception | None = None
        self._thread: threading.Thread | None = None
        with _claim_turns_guard:
            self._turn = _claim_turns.setdefault(str(self.path), threading.RLock())

    def _own(self) -> dict[str, Any]:
        ticks = process_start_ticks(os.getpid())
        if ticks is None:
            raise _error("Cannot establish process identity")
        return {"pid": os.getpid(), "startedAtMs": ticks,
                "heartbeatAtMs": int(time.time() * 1000), "lockId": str(uuid.uuid4())}

    def _read(self, boundary: _Boundary, name: str) -> dict[str, Any] | None:
        try:
            return _lock_record(boundary.read(name))
        except FileNotFoundError:
            return None

    @staticmethod
    def _live(record: dict[str, Any]) -> bool:
        # Permission/parse failures propagate. Unknown liveness never means dead.
        try:
            return process_start_ticks(record["pid"]) == record["startedAtMs"]
        except Exception as exc:
            raise _error("Cannot determine mutation lock owner liveness") from exc

    @contextmanager
    def _claim(self, boundary: _Boundary) -> Iterator[bool]:
        name = boundary.name + ".claim"
        record = self._own()
        owned = boundary.create(name, _record_text(record))
        if not owned:
            holder = self._read(boundary, name)
            if holder is not None and self._live(holder):
                yield False
                return
            try:
                boundary.remove(name)
            except FileNotFoundError:
                pass
            owned = boundary.create(name, _record_text(record))
        try:
            yield owned
        finally:
            if owned:
                try:
                    if self._read(boundary, name) == record:
                        boundary.remove(name)
                except (OSError, FuccError):
                    pass

    def _change(self, expected: dict[str, Any], next_record: dict[str, Any] | None) -> bool:
        with self._turn, _Boundary(self.root, self.path) as boundary:
            with self._claim(boundary) as claimed:
                if not claimed:
                    return False
                actual = self._read(boundary, boundary.name)
                if actual is None or actual["lockId"] != expected["lockId"]:
                    return False
                if next_record is None:
                    boundary.remove(boundary.name)
                else:
                    boundary.replace(boundary.name, _record_text(next_record))
                return True

    def acquire(self) -> "MutationLock":
        if self.record is not None:
            raise _error("Mutation lock has already been acquired")
        self._stop.clear()
        self._heartbeat_error = None
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        validate_boundary(self.root, self.root / "boundary-check")
        with _Boundary(self.root, self.root / "boundary-check") as root_boundary:
            try:
                os.mkdir("locks", mode=0o700, dir_fd=root_boundary.parent_fd)
                os.fsync(root_boundary.parent_fd)
            except FileExistsError:
                pass
        proposed = self._own()
        for _ in range(16):
            with _Boundary(self.root, self.path) as boundary:
                if boundary.create(boundary.name, _record_text(proposed)):
                    self.record = proposed
                    break
                existing = self._read(boundary, boundary.name)
            if existing is None:
                continue
            if self._live(existing):
                raise _error("Another live process owns the mutation lock", "LOCK_BUSY", 20)
            if not self.recover:
                raise _error("A previous mutation owner requires recovery", "RECOVERY_REQUIRED", 21)
            if self._change(existing, proposed):
                self.record = proposed
                break
        if self.record is None:
            raise _error("Mutation lock acquisition did not converge", "RECOVERY_REQUIRED", 21)
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()
        return self

    def heartbeat(self) -> None:
        with self._turn:
            if self.record is None:
                raise _error("Cannot heartbeat an unowned mutation lock")
            next_record = {**self.record, "heartbeatAtMs": int(time.time() * 1000)}
            if not self._change(self.record, next_record):
                raise _error("Mutation lock ownership was lost", "RECOVERY_REQUIRED", 21)
            self.record = next_record

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(5):
            try:
                self.heartbeat()
                self._heartbeat_error = None
            except Exception as exc:
                self._heartbeat_error = exc

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        with self._turn:
            if self.record is None:
                return
            if not self._change(self.record, None):
                raise _error("Mutation lock ownership was lost", "RECOVERY_REQUIRED", 21)
            self.record = None
            if self._heartbeat_error is not None:
                raise _error("Mutation lock heartbeat failed", "RECOVERY_REQUIRED", 21) from self._heartbeat_error

    def __enter__(self) -> "MutationLock":
        return self.acquire()

    def __exit__(self, *_: Any) -> None:
        self.release()
