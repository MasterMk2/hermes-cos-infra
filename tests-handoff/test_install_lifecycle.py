"""Execute install -> health -> rollback with filesystem operations intact.

Only host roots and privileged command adapters are substituted. The real
scripts produce the backups, service snapshots, configurations and units.
Broker payloads are real; two small gateway payloads isolate deployment from
the separate gateway import/integration suite. No real service is started.
"""
import json
import os
from pathlib import Path

from test_installer_harness import InstallerHarness, ROOT


class FullInstall(InstallerHarness):
    def setUp(self):
        super().setUp()
        self.broker = self.root / "broker"
        self.units = self.root / "units"
        self.units.mkdir()
        (self.root / "backups").mkdir()
        self.gateway_config = self.targets / "channel-gateway/config/config.yaml"
        self.gateway_config.parent.mkdir()
        self.gateway_config.write_text("proactive:\n  enabled: false\n", encoding="utf-8")
        replacements = {
            "/srv/hermes": str(self.targets),
            "/opt/hermes-coding-handoff": str(self.broker),
            "/etc/systemd/system": str(self.units),
            "/etc/hermes-secrets": str(self.secret_dir),
            "/var/backups": str(self.root / "backups"),
        }
        for path in (self.pkg / "src/coding-handoff").rglob("*"):
            if path.is_file() and path.suffix in (".sh", ".py", ".json", ".in"):
                content = path.read_text(encoding="utf-8")
                for old, new in replacements.items():
                    content = content.replace(old, new)
                path.write_text(content, encoding="utf-8", newline="\n")
        manifest = json.loads(self.manifest.read_text())
        original = json.loads((ROOT / "src/coding-handoff/deploy/manifest.json").read_text())
        for name, component in original["components"].items():
            if name == "gateway":
                continue
            entries = []
            for item in component["files"]:
                item = dict(item)
                for old, new in replacements.items():
                    item["target"] = item["target"].replace(old, new)
                item["payload_sha256"] = self._sha(self.pkg / item["source"])
                entries.append(item)
            manifest["components"][name] = dict(component, files=entries)
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.state.write_text(json.dumps({
            "channel-gateway.service": ["disabled", "active"],
            "hermes-coding-handoff.service": ["disabled", "inactive"],
        }))
        self.extra_env = {"BROKER_CONFIG": str(self.broker / "config/handoff.json")}
        self.fake("stat", '''
import subprocess,sys
if sys.argv[1:3] == ['-c', '%U']: print('fixture'); sys.exit(0)
if sys.argv[1:3] == ['-c', '%G']: print('fixture'); sys.exit(0)
sys.exit(subprocess.call(['/usr/bin/stat', *sys.argv[1:]]))
''')
        self.fake("install", '''
import subprocess,sys
args=iter(sys.argv[1:]); kept=[]
for arg in args:
    if arg in ('-o', '-g'): next(args)
    else: kept.append(arg)
sys.exit(subprocess.call(['/usr/bin/install', *kept]))
''')
        self.fake("chown", "pass\n")
        self.fake("cp", '''
import os,subprocess,sys
if os.environ.get('FAIL_COPY_SOURCE') in sys.argv[1:]:
    open(sys.argv[-1], 'w').write('partial backup')
    sys.exit(1)
sys.exit(subprocess.call(['/usr/bin/cp', *sys.argv[1:]]))
''')
        self.fake("systemctl", '''
import json,os,sys
from pathlib import Path
path=Path(os.environ['FAKE_STATE']); state=json.loads(path.read_text())
args=sys.argv[1:]; op=args[0]; units=[a for a in args if a.endswith('.service')]
with open(os.environ['FAKE_LOG'],'a') as log: log.write('systemctl '+' '.join(args)+'\\n')
if op in ('is-active', 'is-enabled'):
    value=state.get(units[0], ['disabled','inactive'])[op=='is-active']
    if '--quiet' not in args: print(value)
    sys.exit(0 if value in ('active','enabled') else 3)
if op=='list-units': print('hermes-coding-handoff.service')
if op=='cat': sys.exit(0 if units[0]=='channel-gateway.service' else 1)
for unit in units:
    value=state.setdefault(unit,['disabled','inactive'])
    if op in ('enable','disable'): value[0]='enabled' if op=='enable' else 'disabled'
    if op in ('start','restart') or (op=='enable' and '--now' in args): value[1]='active'
    if op=='stop' or (op=='disable' and '--now' in args): value[1]='inactive'
path.write_text(json.dumps(state))
''')
        self.fake("curl", '''
import json,os,sys
if os.environ.get('FAIL_HEALTH'): sys.exit(7)
with open(os.environ['BROKER_CONFIG']) as f: config=json.load(f)
print(json.dumps({'status':'ok','dispatch_enabled':config['dispatch_enabled']}))
''')

    def fake(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def env(self):
        return {**super().env(), **self.extra_env}

    def install(self, *flags):
        return self.run_script("install.sh", "--bridge-config", str(self.bridge_config),
                               "--bridge-socket", str(self.socket),
                               "--jobs-dir", str(self.jobs), *flags)

    def test_inert_then_enabled_install_passes_real_health(self):
        first = self.install()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        first_backup = self.backups()[0]
        snapshot = (first_backup / ".service-state").read_text()
        self.assertIn("channel-gateway.service\tdisabled\tactive", snapshot)
        second = self.install("--enable-route", "--enable-dispatch")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(len(self.backups()), 2)
        self.assertIn("health passed", second.stdout)
        import yaml
        config = yaml.safe_load(self.gateway_config.read_text())
        self.assertTrue(config["coding_handoff"]["enabled"])
        self.assertFalse(config["proactive"]["enabled"])

    def test_missing_gateway_config_fails_before_mutation(self):
        self.gateway_config.unlink()
        originals = [p.read_bytes() for p in self.gateway_files]
        result = self.install("--enable-route")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.backups(), [])
        self.assertEqual(originals, [p.read_bytes() for p in self.gateway_files])

    def test_health_failure_restores_actual_preinstall_files_and_state(self):
        self.assertEqual(self.install().returncode, 0)
        # Running but disabled is a supported pre-install state.
        state = json.loads(self.state.read_text())
        state["hermes-coding-handoff.service"] = ["disabled", "active"]
        self.state.write_text(json.dumps(state))
        originals = {p: p.read_bytes() for p in [self.gateway_config,
                     self.broker / "config/handoff.json", self.units / "hermes-coding-handoff.service"]}
        self.extra_env["FAIL_HEALTH"] = "1"
        result = self.install("--enable-route", "--enable-dispatch")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback complete", result.stdout, result.stdout + result.stderr)
        self.assertEqual(json.loads(self.state.read_text()), state)
        for path, before in originals.items():
            self.assertEqual(path.read_bytes(), before)

    def test_partial_config_backup_failure_does_not_restore_garbage(self):
        originals = [p.read_bytes() for p in self.gateway_files]
        config = self.gateway_config.read_bytes()
        self.extra_env["FAIL_COPY_SOURCE"] = str(self.gateway_config)
        result = self.install("--enable-route")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback complete", result.stdout, result.stdout + result.stderr)
        self.assertEqual(self.gateway_config.read_bytes(), config)
        self.assertEqual(originals, [p.read_bytes() for p in self.gateway_files])
        self.assertFalse((self.backups()[0] / "config.yaml").exists())

    def test_partial_unit_backup_failure_preserves_original_unit(self):
        unit = self.units / "hermes-coding-handoff.service"
        unit.write_text("[Service]\nExecStart=/bin/true\n")
        self.extra_env["FAIL_COPY_SOURCE"] = str(unit)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback complete", result.stdout, result.stdout + result.stderr)
        self.assertEqual(unit.read_text(), "[Service]\nExecStart=/bin/true\n")
