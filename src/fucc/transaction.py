"""Recoverable resources with guarded v2 targets and v3 resource enrollment."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Callable, Iterable

from .errors import FuccError
from .regions import get_target_region
from .state import (MutationLock, _Boundary, _decode, _parse_json, canonical_json, checked_root, clone_json,
                    is_safe_json, read_checked, write_checked)

SCHEMA = "fucc-local-transaction-v1"
SCHEMA_V2 = "fucc-local-transaction-v2"
SCHEMA_V3 = "fucc-local-transaction-v3"
SCHEMAS = {1: SCHEMA, 2: SCHEMA_V2, 3: SCHEMA_V3}
MARKER_SCHEMA = "fucc-local-transaction-initialized-v1"
EMPTY = {"region": None, "revision": 0, "resources": []}
REGIONS = ("us", "eu", "jp", "sg")


@dataclass
class Resource:
    id: str
    identity: str
    read: Callable[[], Any]
    write: Callable[[Any], None]
    baseline: Callable[[Any], Any] | None = None


def _unsafe() -> FuccError:
    return FuccError("Local environment state requires recovery; no resource values were disclosed",
                     code="RECOVERY_REQUIRED", exit_code=21)


def _equal(left: Any, right: Any) -> bool:
    return canonical_json(left) == canonical_json(right)


def _exact(value: Any, keys: Iterable[str]) -> bool:
    return type(value) is dict and set(value) == set(keys)


def _id(value: Any) -> bool:
    return type(value) is str and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", value) is not None


def _identity(value: Any) -> bool:
    if type(value) is not str or "\0" in value:
        return False
    try:
        return 0 < len(value.encode("utf-16-le")) // 2 <= 4096
    except UnicodeError:
        return False


def _unique(values: list[dict[str, Any]]) -> bool:
    return len({value["id"] for value in values}) == len(values)


def _resource_state(value: Any) -> bool:
    return (_exact(value, ("id", "identity", "original", "desired")) and _id(value["id"])
            and _identity(value["identity"]) and is_safe_json(value["original"]) and is_safe_json(value["desired"]))


def _snapshot(value: Any) -> bool:
    if (not _exact(value, ("region", "revision", "resources"))
            or type(value["revision"]) not in (int, float)
            or not 0 <= value["revision"] <= 2**53 - 1 or value["revision"] % 1 != 0
            or type(value["resources"]) is not list or not all(_resource_state(item) for item in value["resources"])
            or not _unique(value["resources"])):
        return False
    return not value["resources"] if value["region"] is None else type(value["region"]) is str and value["region"] in REGIONS and bool(value["resources"])


def _entry(value: Any) -> bool:
    return (_exact(value, ("id", "identity", "before", "desired")) and _id(value["id"])
            and _identity(value["identity"]) and is_safe_json(value["before"]) and is_safe_json(value["desired"]))


def _targets(left: list[Any], right: list[Any]) -> bool:
    def key(value: Any) -> tuple[str, str]:
        return (value["id"], value["identity"]) if type(value) is dict else (value.id, value.identity)
    return len(left) == len(right) and all(key(a) == key(b) for a, b in zip(left, right))


def _subset_targets(left: list[Any], right: list[Any]) -> bool:
    def key(value: Any) -> tuple[str, str]:
        return (value["id"], value["identity"]) if type(value) is dict else (value.id, value.identity)
    candidates = dict(key(value) for value in right)
    return all(candidates.get(id) == identity for id, identity in map(key, left))


def valid_document(value: Any) -> bool:
    if (not _exact(value, ("version", "committed", "journal"))
            or type(value["version"]) not in (int, float) or value["version"] not in SCHEMAS
            or not _snapshot(value["committed"])):
        return False
    journal = value["journal"]
    if journal is None:
        return True
    if (not _exact(journal, ("kind", "previous", "target", "entries"))
            or journal["kind"] not in ("apply", "restore")
            or not _snapshot(journal["previous"]) or not _snapshot(journal["target"])
            or type(journal["entries"]) is not list or not all(_entry(item) for item in journal["entries"])
            or not _unique(journal["entries"]) or not _equal(journal["previous"], value["committed"])
            or journal["target"]["revision"] != journal["previous"]["revision"] + 1):
        return False
    previous, target, entries = journal["previous"], journal["target"], journal["entries"]
    if journal["kind"] == "restore":
        return (previous["region"] is not None and target["region"] is None
                and _targets(entries, previous["resources"])
                and all(_equal(entry["before"], resource["desired"]) and _equal(entry["desired"], resource["original"])
                        for entry, resource in zip(entries, previous["resources"])))
    if (target["region"] is None or not _targets(entries, target["resources"])
            or not all(_equal(entry["desired"], resource["desired"]) for entry, resource in zip(entries, target["resources"]))):
        return False
    if previous["region"] is None:
        return True
    if value["version"] == 3:
        if not _subset_targets(previous["resources"], target["resources"]):
            return False
        entries_by_id = {entry["id"]: entry for entry in entries}
        target_by_id = {resource["id"]: resource for resource in target["resources"]}
        return all(_equal(entries_by_id[before["id"]]["before"], before["desired"])
                   and _equal(before["original"], target_by_id[before["id"]]["original"])
                   for before in previous["resources"])
    return (_targets(previous["resources"], target["resources"])
            and all(_equal(entry["before"], before["desired"]) and _equal(before["original"], after["original"])
                    for entry, before, after in zip(entries, previous["resources"], target["resources"])))


def _marker(value: Any) -> bool:
    return _exact(value, ("initialized",)) and value["initialized"] is True


class _UnsupportedWire(Exception):
    """A valid future envelope must never fall back to an older generation."""


def _generation(boundary: _Boundary, name: str) -> dict[str, Any]:
    try:
        text = boundary.read(name)
    except FileNotFoundError:
        return {"kind": "missing"}
    except UnicodeError:
        return {"kind": "corrupt"}
    except OSError:
        return {"kind": "corrupt"}
    try:
        envelope = _parse_json(text)
        schema = envelope.get("schema") if type(envelope) is dict else None
        if type(schema) is not str:
            return {"kind": "corrupt"}
        # Reuse the checked-state decoder before interpreting version metadata.
        # _Boundary retains the no-follow, dirfd and directory-identity checks.
        payload = _decode(text, schema, None)
        version = payload.get("version") if type(payload) is dict else None
        if schema not in SCHEMAS.values() or type(version) in (int, float) and version > max(SCHEMAS):
            raise _UnsupportedWire()
        if not valid_document(payload) or SCHEMAS[payload["version"]] != schema:
            return {"kind": "corrupt"}
        return {"kind": "valid", "payload": payload}
    except _UnsupportedWire:
        raise
    except Exception:
        return {"kind": "corrupt"}


def _read_document(root: Path) -> dict[str, Any]:
    with _Boundary(root, root / "local-transaction.json") as boundary:
        current = _generation(boundary, boundary.name)
        previous = _generation(boundary, boundary.name + ".prev")
        if current["kind"] == "valid":
            return {"kind": "ok", "payload": current["payload"], "degraded": False}
        if previous["kind"] == "valid":
            return {"kind": "ok", "payload": previous["payload"], "degraded": True}
        if current["kind"] == previous["kind"] == "missing":
            return {"kind": "missing"}
        raise _unsafe()


def _load(root: Path) -> tuple[dict[str, Any] | None, bool]:
    try:
        root.lstat()
    except FileNotFoundError:
        return None, False
    except Exception as exc:
        raise _unsafe() from exc
    try:
        marker = read_checked(root, root / "local-transaction.initialized", MARKER_SCHEMA, _marker)
        result = _read_document(root)
        if result["kind"] == "missing":
            if marker["kind"] != "missing":
                raise _unsafe()
            return None, False
        if marker["kind"] == "missing" or marker["degraded"]:
            raise _unsafe()
        return result["payload"], result["degraded"]
    except Exception as exc:
        raise _unsafe() from exc


def read_resource_bindings(root: str | Path) -> list[dict[str, str]]:
    document, _ = _load(checked_root(root))
    if document is None:
        return []
    bindings = document["journal"]["entries"] if document["journal"] is not None else document["committed"]["resources"]
    return [{"id": item["id"], "identity": item["identity"]} for item in bindings]


def read_committed_target(root: str | Path) -> dict[str, str] | None:
    """Read the unified committed preset without inspecting live resources.

    A nested Bash transaction may have committed a different preset while its
    outer transaction is unfinished. The outer checked document remains the
    authority here. Only the target is returned; saved baselines stay private.
    """
    root = checked_root(root)
    document, degraded = _load(root)
    if document is None:
        return None
    if degraded:
        raise _unsafe()

    def target(snapshot):
        if snapshot["region"] is None:
            return None
        binding = next((item for item in snapshot["resources"] if item["id"] == "bash-profile"), None)
        if binding is None or binding["identity"] != str(root.parent):
            raise _unsafe()
        desired = binding["desired"]
        if (type(desired) is not dict or set(desired) not in ({"region"}, {"region", "timezone"})
                or desired["region"] != snapshot["region"]):
            raise _unsafe()
        try:
            profile = get_target_region(snapshot["region"], desired.get("timezone"))
            if "timezone" in desired and (document["version"] < 2 or type(desired["timezone"]) is not str
                                           or profile["timezone"] != desired["timezone"]
                                           or profile["timezone"] == get_target_region(snapshot["region"])["timezone"]):
                raise _unsafe()
        except FuccError as exc:
            raise _unsafe() from exc
        return clone_json(desired)

    committed = target(document["committed"])
    journal = document["journal"]
    if journal is not None:
        # valid_document already binds entries to the corresponding snapshot;
        # validate the same trusted Bash authority on both transaction sides.
        target(journal["previous"])
        target(journal["target"])
        if committed is None:
            # During an interrupted initial apply, a nested Bash target is
            # uncommitted at the unified level. Refuse that fallback explicitly.
            raise _unsafe()
    return committed


def matches_resource_claim(root: str | Path, id: str, expected: Any) -> bool:
    if not _id(id) or not is_safe_json(expected):
        raise _unsafe()
    document, _ = _load(checked_root(root))
    if document is None:
        return False
    journal = document["journal"]
    snapshot = document["committed"] if journal is None else journal["target"] if journal["kind"] == "apply" else journal["previous"]
    return any(item["id"] == id and _equal(item["desired"], expected) for item in snapshot["resources"])


def _locale_key(id: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    # Node's default ICU collation for the permitted ASCII id alphabet:
    # '_' < '-' < '.' < digits < letters, then lowercase before uppercase.
    # The secondary case comparison happens only after the full primary string.
    punctuation = {"_": 0, "-": 1, ".": 2}
    primary = tuple(punctuation[char] if char in punctuation else ord(char) - ord("0") + 3
                    if char.isdigit() else ord(char.lower()) - ord("a") + 13 for char in id)
    return primary, tuple(1 if char.isupper() else 0 for char in id)


class LocalTransaction:
    def __init__(self, root: str | Path, resources: Iterable[Resource],
                 before_recover: Callable[[], None] | None = None, wire_version: int = 1):
        if type(wire_version) is not int or wire_version not in SCHEMAS:
            raise FuccError("Local transaction wire version must be 1, 2 or 3")
        self.wire_version = wire_version
        self._effective_wire_version = wire_version
        self.root = checked_root(root)
        self.resources = list(resources)
        if (not self.resources or any(not _id(item.id) or not _identity(item.identity)
                                     or not callable(item.read) or not callable(item.write) for item in self.resources)
                or len({item.id for item in self.resources}) != len(self.resources)):
            raise FuccError("Local transaction requires a nonempty set of uniquely identified resources")
        self.resources.sort(key=lambda resource: _locale_key(resource.id))
        self.before_recover = before_recover
        self.path = self.root / "local-transaction.json"
        self.lock_key = f"{self.root}\0local-transaction"

    def _save(self, document: dict[str, Any]) -> None:
        inactive = document["journal"] is None and document["committed"]["region"] is None
        version = 1 if inactive else self._effective_wire_version
        payload = {**document, "version": version}
        write_checked(self.root, self.path, SCHEMAS[version], payload, valid_document)
        if inactive and self._effective_wire_version >= 2:
            # A completed restore (or initial apply compensation) proves the
            # baseline is restored. Remove newer formats from both generations before an
            # older runtime is allowed to capture a fresh baseline again.
            write_checked(self.root, self.path, SCHEMA, payload, valid_document)

    def _load(self) -> tuple[dict[str, Any] | None, bool]:
        document, degraded = _load(self.root)
        if document is not None:
            self._effective_wire_version = max(self.wire_version, document["version"])
        return document, degraded

    def _initialize(self) -> dict[str, Any]:
        write_checked(self.root, self.root / "local-transaction.initialized", MARKER_SCHEMA, {"initialized": True}, _marker)
        document = {"version": 1, "committed": clone_json(EMPTY), "journal": None}
        self._save(document)
        return document

    def _bound(self, snapshot: dict[str, Any]) -> bool:
        return snapshot["region"] is None or _targets(snapshot["resources"], self.resources)

    def _journal_bound(self, journal: dict[str, Any]) -> bool:
        previous_bound = self._bound(journal["previous"])
        if not previous_bound and self._effective_wire_version == 3 and journal["kind"] == "apply":
            previous_bound = _subset_targets(journal["previous"]["resources"], journal["entries"])
        return (_targets(journal["entries"], self.resources)
                and previous_bound and self._bound(journal["target"]))

    def _document_bound(self, document: dict[str, Any]) -> bool:
        journal = document["journal"]
        return self._bound(document["committed"]) if journal is None else self._journal_bound(journal)

    @staticmethod
    def _read(resource: Resource) -> Any:
        return clone_json(resource.read())

    def _actual(self, snapshot: dict[str, Any], *, enroll: bool = False) -> list[Any]:
        if not self._bound(snapshot) and not (enroll and _subset_targets(snapshot["resources"], self.resources)):
            raise _unsafe()
        actual = [self._read(resource) for resource in self.resources]
        actual_by_id = {resource.id: value for resource, value in zip(self.resources, actual)}
        if snapshot["region"] is not None and any(not _equal(actual_by_id[expected["id"]], expected["desired"])
                                                  for expected in snapshot["resources"]):
            raise _unsafe()
        return actual

    def _move(self, entry: dict[str, Any], value: Any) -> None:
        resource = next((item for item in self.resources if item.id == entry["id"] and item.identity == entry["identity"]), None)
        if resource is None:
            raise _unsafe()
        actual = self._read(resource)
        if not _equal(actual, entry["before"]) and not _equal(actual, entry["desired"]):
            raise _unsafe()
        if not _equal(actual, value):
            resource.write(clone_json(value))
        if not _equal(self._read(resource), value):
            raise _unsafe()

    def _publish(self, journal: dict[str, Any]) -> None:
        document = {"version": 1, "committed": journal["previous"], "journal": journal}
        # Both generations must carry the same plan before touching resources.
        self._save(document)
        self._save(document)

    def _compensate(self, journal: dict[str, Any]) -> bool:
        if not self._journal_bound(journal):
            return False
        try:
            self._publish(journal)
        except Exception:
            return False
        complete = True
        for entry in reversed(journal["entries"]):
            try:
                self._move(entry, entry["before"])
            except Exception:
                complete = False
        if not complete:
            return False
        try:
            self._save({"version": 1, "committed": journal["previous"], "journal": None})
            return True
        except Exception:
            return False

    def _forward_restore(self, journal: dict[str, Any]) -> bool:
        if not self._journal_bound(journal):
            return False
        try:
            self._publish(journal)
            for entry in journal["entries"]:
                self._move(entry, entry["desired"])
            self._save({"version": 1, "committed": journal["target"], "journal": None})
            return True
        except Exception:
            return False

    def _locked(self, action: Callable[[], dict[str, str]], recover: bool = False) -> dict[str, str]:
        try:
            lock = MutationLock(self.root, self.lock_key, recover=recover).acquire()
        except FuccError as exc:
            if exc.code == "RECOVERY_REQUIRED":
                return {"kind": "recovery_required"}
            raise
        try:
            return action()
        finally:
            lock.release()

    def apply(self, region: str, desired: dict[str, Any], *, wire_version: int | None = None,
              enroll: bool = False) -> dict[str, str]:
        if type(enroll) is not bool:
            raise FuccError("Local transaction enrollment must be explicitly enabled or disabled")
        if wire_version is not None and (type(wire_version) is not int or wire_version not in SCHEMAS):
            raise FuccError("Local transaction wire version must be 1, 2 or 3")
        if (type(region) is not str or region not in REGIONS or not is_safe_json(desired)
                or not _exact(desired, (resource.id for resource in self.resources))):
            raise FuccError("Local environment target must contain one JSON value for every managed resource")
        target_values = clone_json(desired)

        def action() -> dict[str, str]:
            try:
                document, degraded = self._load()
                if degraded or document is not None and document["journal"] is not None:
                    return {"kind": "recovery_required"}
                self._effective_wire_version = max(self._effective_wire_version, wire_version or self.wire_version,
                                                   3 if enroll else 1)
                before = self._actual(document["committed"] if document is not None else EMPTY, enroll=enroll)
                if document is None:
                    document = self._initialize()
            except Exception:
                return {"kind": "recovery_required"}
            previous = document["committed"]
            if self._bound(previous) and previous["region"] == region and all(_equal(value, target_values[resource.id])
                                                      for resource, value in zip(self.resources, before)):
                return {"kind": "noop"}
            try:
                old_resources = {resource["id"]: resource for resource in previous["resources"]}
                target = {"region": region, "revision": previous["revision"] + 1, "resources": [
                    {"id": resource.id, "identity": resource.identity,
                     "original": clone_json(resource.baseline(clone_json(before[index])) if resource.baseline is not None else before[index])
                     if resource.id not in old_resources else clone_json(old_resources[resource.id]["original"]),
                     "desired": target_values[resource.id]}
                    for index, resource in enumerate(self.resources)]}
            except Exception:
                return {"kind": "recovery_required"}
            journal = {"kind": "apply", "previous": previous, "target": target,
                       "entries": [{"id": resource.id, "identity": resource.identity, "before": before[index], "desired": target_values[resource.id]}
                                   for index, resource in enumerate(self.resources)]}
            try:
                self._publish(journal)
                for entry in journal["entries"]:
                    self._move(entry, entry["desired"])
                self._save({"version": 1, "committed": target, "journal": None})
                return {"kind": "committed"}
            except Exception:
                return {"kind": "compensated" if self._compensate(journal) else "recovery_required"}
        return self._locked(action)

    def restore(self) -> dict[str, str]:
        def action() -> dict[str, str]:
            try:
                document, degraded = self._load()
                if degraded or document is not None and document["journal"] is not None:
                    return {"kind": "recovery_required"}
                previous = document["committed"] if document is not None else None
                if previous is None or previous["region"] is None:
                    return {"kind": "noop"}
                before = self._actual(previous)
            except Exception:
                return {"kind": "recovery_required"}
            journal = {"kind": "restore", "previous": previous,
                       "target": {"region": None, "revision": previous["revision"] + 1, "resources": []},
                       "entries": [{"id": resource["id"], "identity": resource["identity"], "before": before[index], "desired": resource["original"]}
                                   for index, resource in enumerate(previous["resources"])]}
            try:
                self._publish(journal)
            except Exception:
                return {"kind": "recovery_required"}
            return {"kind": "restored" if self._forward_restore(journal) else "recovery_required"}
        return self._locked(action)

    def recover(self) -> dict[str, str]:
        def action() -> dict[str, str]:
            try:
                document, degraded = self._load()
                if document is not None and not self._document_bound(document):
                    return {"kind": "recovery_required"}
                if self.before_recover is not None:
                    self.before_recover()
                document, degraded = self._load()
            except Exception:
                return {"kind": "recovery_required"}
            if document is None:
                return {"kind": "noop"}
            if not self._document_bound(document):
                return {"kind": "recovery_required"}
            if document["journal"] is None:
                if degraded:
                    return {"kind": "recovery_required"}
                try:
                    self._actual(document["committed"])
                    return {"kind": "noop"}
                except Exception:
                    return {"kind": "recovery_required"}
            journal = document["journal"]
            recovered = self._compensate(journal) if journal["kind"] == "apply" else self._forward_restore(journal)
            return {"kind": "recovered" if recovered else "recovery_required"}
        return self._locked(action, recover=True)

    def status(self) -> dict[str, Any]:
        try:
            document, degraded = self._load()
        except Exception:
            return {"region": None, "health": "recovery_required", "transaction": "none", "revision": 0, "active": False,
                    "resources": [{"id": item.id, "identity": item.identity, "aligned": False} for item in self.resources]}
        if document is None:
            return {"region": None, "health": "healthy", "transaction": "none", "revision": 0, "active": False,
                    "resources": [{"id": item.id, "identity": item.identity, "aligned": True} for item in self.resources]}
        committed, journal = document["committed"], document["journal"]
        binding_valid = self._document_bound(document)
        summaries = []
        for resource in self.resources:
            expected = next((item for item in committed["resources"] if item["id"] == resource.id and item["identity"] == resource.identity), None)
            aligned = committed["region"] is None and journal is None
            if not aligned and expected is not None:
                try:
                    aligned = _equal(self._read(resource), expected["desired"])
                except Exception:
                    aligned = False
            summaries.append({"id": resource.id, "identity": resource.identity, "aligned": aligned})
        return {"region": committed["region"], "active": committed["region"] is not None, "revision": committed["revision"],
                "transaction": journal["kind"] if journal is not None else "none", "resources": summaries,
                "health": "recovery_required" if degraded or journal is not None or not binding_valid else
                "healthy" if all(item["aligned"] for item in summaries) else "degraded"}

    def _committed_value(self, resource_id: str, field: str) -> Any | None:
        if not _id(resource_id):
            raise _unsafe()
        document, degraded = self._load()
        if document is None:
            return None
        committed = document["committed"]
        if degraded or not self._document_bound(document):
            raise _unsafe()
        return next((clone_json(item[field]) for item in committed["resources"] if item["id"] == resource_id), None)

    def committed_desired(self, resource_id: str) -> Any | None:
        """Return only a checked committed target, never its saved baseline."""
        return self._committed_value(resource_id, "desired")

    def committed_original(self, resource_id: str) -> Any | None:
        """Read a checked baseline for an internal adapter; never expose in CLI."""
        return self._committed_value(resource_id, "original")
