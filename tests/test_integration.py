"""Restore predecessor baselines and reject incompatible native resource sets."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest

from fucc.jsonc import edit, parse
from fucc.paths import persist_root
from fucc.runtime import LocalRuntime
from fucc.transaction import Resource
from test_state import build_old_bundle, node

LOCALES = ["en_US.utf8", "en_GB.utf8", "ja_JP.utf8", "en_SG.utf8"]


class DiskMachine:
    """Shares a fixture resource across languages; never invokes the helper."""
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.exists():
            self.path.write_text(json.dumps({"active": False, "region": None, "ownerUid": None}))
        self.resource = Resource("machine", "fucc-machine:v1:/var/lib/fucc", self.read, self.write)

    def read(self):
        return json.loads(self.path.read_text())

    def write(self, value):
        self.path.write_text(json.dumps(value))

    def desired(self, region, timezone=None):
        return {"active": True, "region": region, "ownerUid": 1000, **({"timezone": timezone} if timezone else {})}

    def inspect(self):
        return {**self.read(), "scope": "machine", "health": "healthy", "recoveryRequired": False,
                "transaction": "none", "revision": 0, "actualTimezone": "UTC", "timezoneAligned": None,
                "requiresAdministrator": True, "browsers": []}

    def recover(self):
        return None


@pytest.fixture
def legacy_runtime_bundle(tmp_path):
    bundle = build_old_bundle(tmp_path)
    old = Path(__file__).resolve().parents[2] / "fucc"
    entry = tmp_path / "legacy-entry.ts"
    with entry.open("a") as stream:
        stream.write(f'\nexport * from {json.dumps(str(old / "src/local/runtime.ts"))};\n')
    esbuild = next((old / "node_modules/.pnpm").glob("esbuild@*/node_modules/esbuild/bin/esbuild"))
    # jsonc-parser's UMD entry uses indirect requires, so point the temporary
    # validation bundle at its equivalent ESM implementation.
    jsonc_esm = next((old / "node_modules/.pnpm").glob("jsonc-parser@*/node_modules/jsonc-parser/lib/esm/main.js"))
    subprocess.run([str(esbuild), str(entry), "--bundle", "--platform=node", "--format=esm", f"--alias:jsonc-parser={jsonc_esm}", f"--outfile={bundle}"],
                   check=True, capture_output=True, text=True)
    return bundle


LEGACY = """
import { readFile, writeFile } from 'node:fs/promises';
const [environment, applications, machineFile, action, region] = process.argv.slice(1).map(value=>JSON.parse(value));
const read = async () => JSON.parse(await readFile(machineFile,'utf8'));
const machine = {
  resource: { id:'machine', identity:'fucc-machine:v1:/var/lib/fucc', read,
    write: async value => writeFile(machineFile,JSON.stringify(value)) },
  desired: region => ({active:true,region,ownerUid:1000}),
  inspect: async () => ({...await read(),scope:'machine',health:'healthy',recoveryRequired:false,
    transaction:'none',revision:0,actualTimezone:'UTC',timezoneAligned:null,
    requiresAdministrator:true,browsers:[]}),
  recover: async () => undefined,
};
const runtime = await old.createLocalRuntime({environment,applicationDirs:[applications],machine,
  listLocales: async () => ['en_US.utf8','en_GB.utf8','ja_JP.utf8','en_SG.utf8']});
const result = action === 'status' ? null : await runtime[action](...(action==='on'?[region,false]:[]));
const status=await runtime.status();
console.log(JSON.stringify({result,status}));
"""


def legacy(bundle, environment, applications, machine_path, action, region=None):
    return json.loads(node(bundle, LEGACY, *[json.dumps(value) for value in (environment, str(applications), str(machine_path), action, region)]))


def fixture(tmp_path):
    home = tmp_path / "home folder'quoted"
    home.mkdir()
    bashrc = home / ".bashrc"
    bashrc.write_text("# original\nexport KEEP='daily'\n")
    claude = home / ".claude" / "settings.json"
    claude.parent.mkdir()
    claude.write_text('{"env":{"TZ":null,"TOKEN":"retain-private"},"model":"original"}\n')
    vscode = home / ".config" / "Code" / "User" / "settings.json"
    vscode.parent.mkdir(parents=True)
    vscode.write_text('''{
// keep this comment
"number":1.2300e+06,
"claudeCode.environmentVariables":[
  {"name":"TZ","value":null,"extra":"keep"},
  {"name":"KEEP","value":"original"},
  {"name":"TZ","value":42}
]
}
''')
    applications = tmp_path / "applications"
    applications.mkdir()
    (applications / "google-chrome.desktop").write_text('[Desktop Entry]\nName=Google Chrome\nExec=/usr/bin/google-chrome %U\nDBusActivatable=true\n[Desktop Action new-window]\nExec=/usr/bin/google-chrome --new-window %U\n')
    override = home / ".local" / "share" / "applications" / "google-chrome.desktop"
    override.parent.mkdir(parents=True)
    override.write_text('[Desktop Entry]\nName=Custom Google Chrome\nExec=/usr/bin/google-chrome --custom %U\nIcon=custom\n')
    environment = {"HOME": str(home), "SHELL": "/bin/bash", "PATH": "/usr/bin:/bin", "XDG_DATA_DIRS": str(tmp_path / "empty")}
    machine_path = tmp_path / "fixture-machine.json"
    machine = DiskMachine(machine_path)
    originals = {str(path): path.read_text() for path in (bashrc, override)}
    return environment, applications, machine_path, machine, claude, vscode, originals


def test_legacy_runtime_migrates_to_native_and_restores_initial_baselines(tmp_path, legacy_runtime_bundle):
    environment, applications, machine_path, machine, claude, vscode, originals = fixture(tmp_path)
    assert legacy(legacy_runtime_bundle, environment, applications, machine_path, "on", "us")["result"]["kind"] == "committed"
    runtime = LocalRuntime(environment, [str(applications)], machine, lambda: LOCALES)
    status = runtime.status()
    old_status = legacy(legacy_runtime_bundle, environment, applications, machine_path, "status")["status"]
    for key in ("region", "revision", "active", "health", "transaction", "configured", "resources", "paths"):
        assert status[key] == old_status[key]
    launcher = Path(status["paths"]["launcher"])
    assert '# FUCC application environment launcher' in launcher.read_text()
    assert 'exec "$@"' in launcher.read_text()
    ledger_path = persist_root(environment) / "local" / "local-transaction.json"
    originals_in_ledger = {item["id"]: deepcopy(item["original"]) for item in json.loads(ledger_path.read_text())["payload"]["committed"]["resources"]}
    # User additions remain outside the managed projection during migration.
    claude.write_text(edit(claude.read_text(), ["env", "TOKEN"], "user-updated"))
    vscode.write_text(edit(vscode.read_text(), ["claudeCode.environmentVariables", -1], {"name": "USER_ADDED", "value": "later"}))
    assert runtime.on("jp")["kind"] == "committed"
    switched = json.loads(ledger_path.read_text())["payload"]["committed"]
    assert {item["id"]: item["original"] for item in switched["resources"]} == originals_in_ledger
    native_status = runtime.status()
    assert native_status["health"] == "healthy" and native_status["configurationModel"] == "native-first"
    retired = set(native_status["retiredResources"])
    assert {"session-environment", "app-launcher", "desktop.google-chrome.desktop"} <= retired
    assert all(item["desired"] == item["original"] for item in switched["resources"] if item["id"] in retired)
    assert not launcher.exists() and not Path(status["paths"]["sessionFile"]).exists()
    assert Path(next(path for path in originals if path.endswith("google-chrome.desktop"))).read_text() == next(
        contents for path, contents in originals.items() if path.endswith("google-chrome.desktop"))
    assert LocalRuntime(environment, [str(applications)], machine, lambda: LOCALES).off()["kind"] == "restored"
    assert all(Path(path).read_text() == contents for path, contents in originals.items())
    assert json.loads(claude.read_text()) == {"env": {"TZ": None, "TOKEN": "user-updated"}, "model": "original"}
    rows = parse(vscode.read_text())["claudeCode.environmentVariables"]
    assert [row["value"] for row in rows if row["name"] == "TZ"] == [None, 42]
    assert any(row.get("extra") == "keep" for row in rows)
    assert {"name": "USER_ADDED", "value": "later"} in rows
    assert '// keep this comment' in vscode.read_text()
    assert '1.2300e+06' in vscode.read_text()
    assert not launcher.exists()
    assert machine.read() == {"active": False, "region": None, "ownerUid": None}
    assert not Path(status["paths"]["sessionFile"]).exists()


def test_fresh_native_runtime_is_rejected_by_legacy_without_writes(tmp_path, legacy_runtime_bundle):
    environment, applications, machine_path, machine, _, _, originals = fixture(tmp_path)
    runtime = LocalRuntime(environment, [str(applications)], machine, lambda: LOCALES)
    assert runtime.on("us")["kind"] == "committed"
    status = runtime.status()
    assert status["health"] == "healthy"
    ids = {item["id"] for item in status["resources"]}
    assert ids == {"bash-profile", "claude-settings", "vscode-settings", "machine"}
    assert not Path(status["paths"]["launcher"]).exists()
    assert not Path(status["paths"]["sessionFile"]).exists()
    before = {str(path): (path.read_bytes(), path.stat().st_mode & 0o777) for path in tmp_path.rglob("*") if path.is_file()}
    for verb in ("on", "off", "recover"):
        rejected = legacy(legacy_runtime_bundle, environment, applications, machine_path, verb, "jp")
        assert rejected["result"]["kind"] == "recovery_required"
        assert rejected["status"]["health"] == "recovery_required"
        assert {str(path): (path.read_bytes(), path.stat().st_mode & 0o777) for path in tmp_path.rglob("*") if path.is_file()} == before
    assert runtime.status()["health"] == "healthy"
    assert runtime.off()["kind"] == "restored"
    assert all(Path(path).read_text() == contents for path, contents in originals.items())


def test_inactive_local_status_reports_standalone_bash_conflict(tmp_path):
    environment, applications, _, machine, _, _, _ = fixture(tmp_path)
    runtime = LocalRuntime(environment, [str(applications)], machine, lambda: LOCALES)
    runtime.bash.protect("us")
    env_script = Path(environment["HOME"]) / ".config" / "fucc" / "env.sh"
    env_script.write_text(env_script.read_text().replace("America/New_York", "Europe/Paris"))
    assert runtime.bash.status()["health"] == "degraded"
    assert runtime.status()["health"] == "degraded"


def test_legacy_baseline_upgrades_to_dynamic_and_old_runtime_cannot_write(tmp_path, legacy_runtime_bundle):
    environment, applications, machine_path, machine, claude, vscode, originals = fixture(tmp_path)
    assert legacy(legacy_runtime_bundle, environment, applications, machine_path, "on", "us")["result"]["kind"] == "committed"
    runtime = LocalRuntime(environment, [str(applications)], machine, lambda: LOCALES)
    ledger_path = persist_root(environment) / "local" / "local-transaction.json"
    original_snapshots = {item["id"]: item["original"] for item in json.loads(ledger_path.read_text())["payload"]["committed"]["resources"]}
    assert runtime.on("us", timezone="America/Los_Angeles")["kind"] == "committed"
    assert runtime.status()["configured"]["TZ"] == "America/Los_Angeles"
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    for verb in ("on", "off", "recover"):
        # The old executable rejects both generations before accessing resources.
        result = subprocess.run(["node", "--input-type=module", "-e",
                                 f"const old=await import({json.dumps(str(legacy_runtime_bundle))});" + LEGACY,
                                 *[json.dumps(value) for value in (environment, str(applications), str(machine_path), verb, "us")]],
                                capture_output=True, text=True)
        assert result.returncode != 0
        assert {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    assert {item["id"]: item["original"] for item in json.loads(ledger_path.read_text())["payload"]["committed"]["resources"]} == original_snapshots
    assert LocalRuntime(environment, [str(applications)], machine, lambda: LOCALES).off()["kind"] == "restored"
    assert all(Path(path).read_text() == contents for path, contents in originals.items())
    assert json.loads(claude.read_text())["env"] == {"TZ": None, "TOKEN": "retain-private"}
    assert [row["value"] for row in parse(vscode.read_text())["claudeCode.environmentVariables"] if row["name"] == "TZ"] == [None, 42]
