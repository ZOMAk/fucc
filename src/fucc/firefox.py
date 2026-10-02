"""Firefox policy values and read-only installation discovery.

The ICE preferences below are Firefox's own mapping for the WebExtension
``disable_non_proxied_udp`` setting, not Chrome policy names. Proxy-only ICE
is conditional on the document actually using a browser proxy; this does not
configure routing or guarantee WebRTC egress under a transparent proxy.

Sources:
https://firefox-admin-docs.mozilla.org/reference/policies/preferences/
https://github.com/mozilla-firefox/firefox/blob/main/toolkit/components/extensions/parent/ext-privacy.js
https://github.com/mozilla-firefox/firefox/blob/main/dom/media/webrtc/jsapi/PeerConnectionImpl.cpp
https://github.com/mozilla-firefox/firefox/blob/main/toolkit/components/enterprisepolicies/EnterprisePoliciesParent.sys.mjs
https://github.com/canonical/firefox-snap/blob/stable/snapcraft.yaml
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import stat
import subprocess

from .errors import FuccError
from .files import read_snapshot
from .regions import get_target_region
from .state import is_safe_json

POLICY_PATH = "/etc/firefox/policies/policies.json"
MANAGED_PREFERENCES = (
    "intl.accept_languages",
    "media.peerconnection.ice.default_address_only",
    "media.peerconnection.ice.no_host",
    "media.peerconnection.ice.proxy_only_if_behind_proxy",
)
LANGUAGES = {"us": "en-US,en", "eu": "en-GB,en", "jp": "ja-JP,ja", "sg": "en-SG,en"}
NATIVE_CANDIDATES = ("/usr/bin/firefox", "/usr/bin/firefox-esr", "/usr/lib/firefox/firefox",
                     "/usr/lib/firefox-esr/firefox-esr", "/opt/firefox/firefox")
FLATPAK_DIRS = ("/var/lib/flatpak/app/org.mozilla.firefox",)
NATIVE_DISTRIBUTION_POLICIES = (
    "/usr/lib/firefox/distribution/policies.json",
    "/usr/lib/firefox-esr/distribution/policies.json",
    "/opt/firefox/distribution/policies.json",
)
SNAP_DISTRIBUTION_POLICIES = ("/snap/firefox/current/usr/lib/firefox/distribution/policies.json",)


def _conflict():
    # Existing policy values may contain credentials or private URLs.
    return FuccError("Firefox policies.json 必须是无重复键的有效策略对象；请检查现有配置。", "CONFIG_CONFLICT", 24)


def _preference_valid(value):
    if type(value) in (str, bool, int, float):
        return True
    if type(value) is not dict or value.get("Status", "locked") not in ("default", "locked", "user", "clear"):
        return False
    if value.get("Status") != "clear" and "Value" not in value:
        return False
    if "Value" in value and type(value["Value"]) not in (str, bool, int, float):
        return False
    kind = value.get("Type")
    if kind is not None:
        if kind not in ("string", "boolean", "number"):
            return False
        if "Value" in value and not ((kind == "string" and type(value["Value"]) is str)
                                     or (kind == "boolean" and type(value["Value"]) is bool)
                                     or (kind == "number" and type(value["Value"]) in (int, float))):
            return False
    return True


def parse_policies(text):
    """Parse a complete policy file and preserve all unrelated JSON values."""
    if text is None:
        return {"policies": {}}
    if not isinstance(text, str):
        raise _conflict()

    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError("Duplicate policy key")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError("Non-finite policy value")

    try:
        if len(text.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Policy exceeds inspection size limit")
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
        if type(value) is not dict or type(value.get("policies")) is not dict or not is_safe_json(value):
            raise ValueError("Invalid policy object")
        preferences = value["policies"].get("Preferences", {})
        if type(preferences) is not dict or not all(_preference_valid(item) for item in preferences.values()):
            raise ValueError("Invalid Preferences object")
        for key in MANAGED_PREFERENCES:
            if key in preferences:
                preference = preferences[key]
                managed = preference.get("Value") if type(preference) is dict else preference
                if type(preference) is dict and preference.get("Status") == "clear" and "Value" not in preference:
                    continue
                if type(managed) is not (str if key == "intl.accept_languages" else bool):
                    raise ValueError("Invalid managed preference type")
        return value
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise _conflict() from exc


def managed_preferences(region):
    """Return four locked language/ICE preferences without changing WebRTC itself."""
    get_target_region(region)
    return {key: {"Value": LANGUAGES[region] if key == "intl.accept_languages" else True, "Status": "locked"}
            for key in MANAGED_PREFERENCES}


def merge_policies(text, region):
    result = deepcopy(parse_policies(text))
    preferences = result["policies"].setdefault("Preferences", {})
    preferences.update(managed_preferences(region))
    return result


def desired_snapshot(snapshot, region):
    """Describe the complete desired file; the transaction owns the original bytes."""
    if (type(snapshot) is not dict or set(snapshot) != {"text", "mode"}
            or (snapshot["text"] is None and snapshot["mode"] is not None)
            or (snapshot["text"] is not None and (type(snapshot["mode"]) is not int or not 0 <= snapshot["mode"] <= 0o777))):
        raise _conflict()
    return {"text": json.dumps(merge_policies(snapshot["text"], region), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            "mode": 0o644}


def policy_aligned(text, region):
    expected = managed_preferences(region)
    try:
        actual = parse_policies(text)["policies"].get("Preferences", {})
    except FuccError:
        return False
    return all(actual.get(key) == value for key, value in expected.items())


def read_policy_snapshot(path=POLICY_PATH):
    """Read a no-link snapshot and validate it without publishing policy values."""
    snapshot = read_snapshot(path)
    parse_policies(snapshot["text"])
    return snapshot


def _snap_connections():
    """Execute only the fixed system Snap client, never the browser or a wrapper."""
    try:
        client = Path("/usr/bin/snap").resolve(strict=True)
        info = client.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            return None
        result = subprocess.run(["/usr/bin/snap", "connections", "firefox"], text=True, capture_output=True,
                                check=True, timeout=5, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"})
        return result.stdout if len(result.stdout) <= 1024 * 1024 else None
    except (OSError, RuntimeError, UnicodeError, subprocess.SubprocessError):
        return None


def snap_policy_interface_connected(text):
    if not isinstance(text, str):
        return False
    return any(len(fields := line.split()) >= 3 and fields[0] == "system-files"
               and fields[1] == "firefox:etc-firefox" and fields[2] == ":system-files"
               for line in text.splitlines())


def _policy_presence(path):
    """Inspect only metadata; a link or unreadable policy is also worth preserving.

    Paths come from fixed installation locations, or explicit read-only test
    seams. No policy contents or browser-provided command is read or executed.
    The Snap ``current`` parent is intentionally allowed for this existence
    check, while all actual writes still target the guarded host policy path.
    """
    try:
        Path(path).lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def discover_firefox(*, candidates=None, snap_path="/snap/bin/firefox", flatpak_dirs=None,
                     snap_connections=None, trusted_uid=0, system_policy_path=POLICY_PATH,
                     distribution_paths=None):
    """Inspect trusted installation files and the Snap interface, without launching Firefox.

    Keyword arguments are read-only test seams; they are not command-line inputs.
    A Firefox Snap with a disconnected/unknown etc-firefox interface is reported
    as installed but unsupported for policy management.
    Linux Firefox selects an existing system policy file before the entire
    installation distribution file; these files are not merged. Avoid creating
    a system file that would hide unrelated installation policies.
    """
    kind = "not_installed"
    for candidate in NATIVE_CANDIDATES if candidates is None else candidates:
        try:
            path = Path(candidate).resolve(strict=True)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != trusted_uid or info.st_mode & 0o022:
                continue
            if str(path).startswith("/snap/"):
                kind = "snap"
                break
            if str(path).startswith("/var/lib/flatpak/"):
                kind = "flatpak"
                break
            if info.st_size < 128 * 1024:
                script = path.read_bytes().decode("utf-8", errors="replace")
                if re.search(r"/snap/bin/firefox\b|\bsnap\s+(?:run\s+)?firefox\b", script):
                    kind = "snap" if Path(snap_path).exists() else "not_installed"
                    break
                if re.search(r"\bflatpak\s+run\b[^\n]*\borg\.mozilla\.firefox\b", script):
                    kind = "flatpak"
                    break
            kind = "native"
            break
        except (OSError, RuntimeError):
            continue
    if kind == "not_installed" and Path(snap_path).exists():
        kind = "snap"
    paths = (*FLATPAK_DIRS, Path.home() / ".local/share/flatpak/app/org.mozilla.firefox") if flatpak_dirs is None else flatpak_dirs
    if kind == "not_installed" and any(Path(path).exists() for path in paths):
        kind = "flatpak"
    connected = snap_policy_interface_connected((snap_connections or _snap_connections)()) if kind == "snap" else None
    supported = kind == "native" or kind == "snap" and connected
    message = ("Firefox 原生策略文件可管理；完全退出后重开，并在 about:policies 验证。" if kind == "native" else
               "Firefox Snap 的 etc-firefox 接口已连接，可读取宿主策略；完全退出后重开并在 about:policies 验证。" if connected else
               "Firefox Snap 的 etc-firefox 接口未连接或无法核实；此安装暂不管理策略。" if kind == "snap" else
               "Firefox Flatpak 策略路径隔离；此版本不支持。" if kind == "flatpak" else "未发现 Firefox 安装。")
    paths = (NATIVE_DISTRIBUTION_POLICIES if kind == "native" else SNAP_DISTRIBUTION_POLICIES
             if kind == "snap" else ()) if distribution_paths is None else distribution_paths
    present = [str(path) for path in paths if _policy_presence(path) is not False]
    system_present = _policy_presence(system_policy_path) if kind in ("native", "snap") else False
    conflict = bool(present) and system_present is not True
    if supported and conflict:
        supported = False
        message = ("发现 Firefox 安装目录的 distribution/policies.json；宿主策略尚不存在或无法核实。"
                   "为避免遮蔽原有策略，此安装暂不管理策略；请先确认并合并现有策略。")
    elif supported and system_present is None:
        supported = False
        message = "无法核实 Firefox 宿主策略路径；此安装暂不管理策略。"
    elif supported and present:
        message += " 现有宿主策略优先于安装目录的 distribution/policies.json。"
    return {"id": "firefox", "label": "Firefox", "kind": kind, "directory": str(Path(POLICY_PATH).parent),
            "policyPath": POLICY_PATH, "policySupported": bool(supported), "snapInterfaceConnected": connected,
            "distributionPolicyPaths": present, "distributionPolicyConflict": conflict,
            "message": message}
