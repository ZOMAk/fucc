"""Durable replacements of plain text files, guarded by inspected snapshots."""
from __future__ import annotations

import os
import stat
import uuid
from typing import TypedDict

from .errors import FuccError


class TextSnapshot(TypedDict):
    text: str | None
    mode: int | None


def _conflict(message: str) -> FuccError:
    return FuccError(message, code="CONFIG_CONFLICT", exit_code=24)


def assert_safe_file_path(path: str) -> None:
    path = os.fspath(path)
    if not os.path.isabs(path) or any(character in path for character in "\x00\r\n"):
        raise _conflict("配置路径必须是有效绝对路径")
    current = path
    while True:
        try:
            info = os.lstat(current)
            safe = stat.S_ISREG(info.st_mode) and info.st_nlink == 1 if current == path else stat.S_ISDIR(info.st_mode)
            if stat.S_ISLNK(info.st_mode) or not safe:
                raise _conflict(f"拒绝读取或修改链接及特殊文件：{current}")
        except FileNotFoundError:
            pass
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def read_snapshot(path: str) -> TextSnapshot:
    path = os.fspath(path)
    assert_safe_file_path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {"text": None, "mode": None}
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise _conflict(f"拒绝读取链接及特殊文件：{path}")
        chunks = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        assert_safe_file_path(path)
        try:
            current = os.lstat(path)
        except FileNotFoundError as error:
            raise _conflict(f"配置在读取期间被修改：{path}") from error
        signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_nlink)
        if signature(before) != signature(after) or signature(after) != signature(current):
            raise _conflict(f"配置在读取期间被修改：{path}")
        try:
            text = b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as error:
            raise _conflict(f"配置必须是有效 UTF-8 文本：{path}") from error
        return {"text": text, "mode": stat.S_IMODE(after.st_mode) & 0o777}
    finally:
        os.close(descriptor)


def _sync_parent(path: str) -> None:
    descriptor = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def replace_snapshot(path: str, desired: TextSnapshot, expected: TextSnapshot, default_mode: int = 0o600) -> None:
    path = os.fspath(path)
    if not isinstance(desired, dict) or set(desired) != {"text", "mode"}:
        raise TypeError("Invalid text snapshot")
    if desired["text"] is not None:
        if not isinstance(desired["text"], str) or type(desired["mode"]) is not int or not 0 <= desired["mode"] <= 0o777:
            raise TypeError("Invalid text snapshot mode")
    elif desired["mode"] is not None:
        raise TypeError("Absent snapshot cannot carry a mode")
    assert_safe_file_path(path)
    if read_snapshot(path) != expected:
        raise _conflict(f"配置在写入前被修改：{path}")
    if desired == expected:
        return
    if desired["text"] is None:
        if expected["text"] is not None:
            if read_snapshot(path) != expected:
                raise _conflict(f"配置在还原期间被修改：{path}")
            os.unlink(path)
            _sync_parent(path)
    else:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        assert_safe_file_path(path)
        temporary = f"{path}.{uuid.uuid4()}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, desired["mode"])
        try:
            os.fchmod(descriptor, desired["mode"])
            contents = desired["text"].encode("utf-8")
            while contents:
                written = os.write(descriptor, contents)
                contents = contents[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            if read_snapshot(path) != expected:
                raise _conflict(f"配置在写入期间被修改：{path}")
            os.replace(temporary, path)
            _sync_parent(path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    if read_snapshot(path) != desired:
        raise _conflict(f"配置写入后验证失败：{path}")
