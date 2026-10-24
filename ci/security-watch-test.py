#!/usr/bin/env python3
"""Fixture-only installed-environment probe and security-watch checks."""
import json
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "home/dot_local/bin/executable_security-watch"


def fixture(root):
    for ecosystem, name, version in [("PyPI", "probe-python", "1.0"),
                                      ("npm", "probe-node", "1.0.0")]:
        db = root / "cache/osv-scalibr" / ecosystem / "all.zip"
        db.parent.mkdir(parents=True)
        advisory = {"id": "PROBE-2026-1", "modified": "2026-01-01T00:00:00Z",
                    "affected": [{"package": {"name": name, "ecosystem": ecosystem},
                                  "versions": [version]}]}
        with zipfile.ZipFile(db, "w") as archive:
            archive.writestr("PROBE-2026-1.json", json.dumps(advisory))


def python_package(root, name="probe-python"):
    metadata = root / f"lib/python3.12/site-packages/{name.replace('-', '_')}-1.0.dist-info/METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")


def node_package(root, name="probe-node"):
    package = root / "node_modules" / name / "package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({"name": name, "version": "1.0.0"}))


class InstalledProbe(unittest.TestCase):
    def test_installed_packages_offline(self):
        scanner = shutil.which("osv-scanner")
        self.assertIsNotNone(scanner, "osv-scanner must already be installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(root),
                   "XDG_CACHE_HOME": str(root / "cache"), "TMPDIR": str(root)}
            version = subprocess.run([scanner, "--version"], env=env, capture_output=True,
                                     text=True, timeout=30)
            self.assertIn("osv-scanner version: 2.6.0", version.stdout)
            fixture(root)
            python_package(root / "venv")
            python_package(root / "venv", "pre_commit")
            python_package(root / "venv", "inspect_ai")
            node_package(root / "npm")
            config = root / "empty.toml"
            config.write_text("")
            for ecosystem, name, version in [("PyPI", "probe-python", "1.0"),
                                              ("npm", "probe-node", "1.0.0")]:
                target = root / ("venv" if ecosystem == "PyPI" else "npm")
                result = subprocess.run([scanner, "scan", "source", "--offline",
                                         "--offline-vulnerabilities", "--no-ignore", "-r",
                                         "--experimental-plugins=python/wheelegg,javascript/packagejson",
                                         "--config", str(config),
                                         "--format=json", "--all-packages", str(target)],
                                        cwd=root, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                data = json.loads(result.stdout)
                packages = [p for source in data["results"] for p in source["packages"]]
                if ecosystem == "PyPI":
                    self.assertTrue({"pre_commit", "inspect_ai"} <=
                                    {p["package"]["name"] for p in packages})
                self.assertTrue(any(p["package"]["name"] == name and
                                    p["package"]["version"] == version and
                                    any(v["id"] == "PROBE-2026-1" for v in p["vulnerabilities"])
                                    for p in packages), data)


class DailyJob(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fixture(self.home)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.tool("systemctl", 'echo "$@" >> "$HOME/refresh"\nexit "${REFRESH_EXIT:-0}"\n')
        self.tool("job-ping", 'echo "$@" >> "$HOME/pings"\n')
        scanner = shutil.which("osv-scanner")
        self.assertIsNotNone(scanner)
        (self.bin / "osv-scanner").symlink_to(scanner)
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home),
                    "XDG_CACHE_HOME": str(self.home / "cache"), "TMPDIR": str(self.home)}
        self.npm = self.home / ".local/share/bootstrap/npm"
        node_package(self.npm)
        node_package(self.npm, "@openai/codex")
        for relative in (".local/share/bootstrap/pre-commit/venv",
                         ".local/share/bootstrap/inspect/venv", "dev_workspace/public/.venv"):
            python_package(self.home / relative)
        python_package(self.home / ".local/share/bootstrap/pre-commit/venv", "pre-commit")
        python_package(self.home / ".local/share/bootstrap/inspect/venv", "inspect-ai")
        status = self.home / "cortex/cortex-core/STATUS.md"
        status.parent.mkdir(parents=True)
        status.write_text("# Status\n\n## Security\nold\n\n## Keep\nunchanged\n")

    def tool(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def scan(self, **extra):
        result = subprocess.run(["/usr/bin/python3", str(SCRIPT)], env=dict(self.env, **extra),
                                capture_output=True, text=True, timeout=60)
        self.report = json.loads((self.home / ".local/state/security/latest.json").read_text())
        return result

    def ignore(self, expiry):
        content = ('[[IgnoredVulns]]\nid = "PROBE-2026-1"\n'
                   f'ignoreUntil = {expiry}\nreason = "Fixture approved risk"\n')
        (self.npm / "osv-scanner.toml").write_text(content)

    def approve(self):
        self.ignore((datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat())

    def test_findings_per_root_and_failure_ping(self):
        self.assertEqual(self.scan().returncode, 1)
        self.assertEqual([r["status"] for r in self.report["roots"]],
                         ["findings", "findings", "findings", "not installed", "findings"])
        self.assertEqual(self.report["errors"], [])
        self.assertEqual(self.report["last_clean_counts"],
                         {r["root"]: r["package_count"] for r in self.report["roots"]
                          if r["status"] == "findings"})
        self.assertEqual((self.home / "pings").read_text(), "security-watch 1\n")

    def test_native_expiry_and_success_status(self):
        self.approve()
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr + str(self.report))
        self.assertEqual((self.home / "pings").read_text(), "security-watch 0\n")
        status = (self.home / "cortex/cortex-core/STATUS.md").read_text()
        self.assertIn("## Keep\nunchanged", status)
        self.assertEqual(status.count("## Security"), 1)
        self.assertNotIn("\nold\n", status)
        self.assertEqual(self.report["exemptions"][0]["id"], "PROBE-2026-1")
        self.assertTrue(self.report["exemptions"][0]["effective"])
        self.assertIn("Fixture approved risk", status)
        self.ignore("2000-01-01")
        self.assertEqual(self.scan().returncode, 1)
        self.assertFalse(self.report["exemptions"][0]["effective"])

    def test_refresh_failure_and_missing_database_never_clean(self):
        self.approve()
        self.assertEqual(self.scan(REFRESH_EXIT="1").returncode, 1)
        self.assertIn("refresh.service failed", self.report["errors"][0])
        (self.home / "cache/osv-scalibr/npm/all.zip").unlink()
        self.assertEqual(self.scan().returncode, 1)
        self.assertEqual(self.report["roots"][0]["status"], "error")
        self.assertEqual(self.report["roots"][1]["status"], "clean")

    def test_refresh_precedes_scanner(self):
        scanner = str((self.bin / "osv-scanner").resolve())
        (self.bin / "osv-scanner").unlink()
        self.tool("osv-scanner", 'test -f "$HOME/refresh" || exit 99\n'
                  'exec "$FIXTURE_SCANNER" "$@"\n')
        self.approve()
        result = self.scan(FIXTURE_SCANNER=scanner)
        self.assertEqual(result.returncode, 0, result.stderr + str(self.report))
        self.assertEqual((self.home / "refresh").read_text(),
                         "--user start osv-db-refresh.service\n")

    def test_private_roots_and_symlink_targets_are_not_scanned(self):
        self.approve()
        names = ("customer-harness", "CustomerHarness", "typo3-dkm-plugin", "typo3-dkm",
                 "Typo3", "DKM", "photo-organizer", "secrets", "project-data", ".git")
        for name in names:
            for variant in {name, name.lower(), name.upper(), name.swapcase()}:
                private = self.home / "dev_workspace" / variant
                node_package(private)
                python_package(private / ".venv")
                (private / "package-lock.json").write_text(json.dumps({
                    "name": "private-canary", "lockfileVersion": 3,
                    "packages": {"node_modules/private-canary": {"version": "1.0.0"}}}))
                public = self.home / "dev_workspace/public" / ("links-" + variant)
                public.mkdir()
                (public / "package-lock.json").symlink_to(private / "package-lock.json")
                linked_node = public / "node_modules/canary"
                linked_node.mkdir(parents=True)
                (linked_node / "package.json").symlink_to(private / "node_modules/probe-node/package.json")
                linked_python = public / "canary.dist-info"
                linked_python.mkdir()
                (linked_python / "METADATA").symlink_to(
                    private / ".venv/lib/python3.12/site-packages/probe_python-1.0.dist-info/METADATA")
        node_package(self.home / "outside")
        (self.home / "dev_workspace/public/linked").symlink_to(self.home / "outside")
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr + str(self.report))
        sources = json.dumps(self.report["roots"][-1])
        self.assertNotIn("probe-node", sources)
        self.assertNotIn("canary", sources)
        self.assertIn("public/.venv", sources)
        for source in self.report["roots"][-1]["results"]["results"]:
            self.assertIn("public/.venv", source["source"]["path"])

    def test_photo_organizer_venv_is_never_read(self):
        self.approve()
        python_package(self.home / "dev_workspace/photo-organizer/.venv")
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr + str(self.report))
        sources = json.dumps(self.report["roots"][-1])
        self.assertIn("public/.venv", sources)
        self.assertNotIn("photo-organizer", sources)

    def test_missing_root_and_status_write_failure(self):
        self.approve()
        (self.home / "cortex/cortex-core/STATUS.md").unlink()
        self.assertEqual(self.scan().returncode, 1)
        self.assertIn("security update failed", self.report["errors"][-1])
        self.assertEqual(self.report["status"], "failed")
        directory = self.home / ".local/state/security"
        self.assertIn(": failed.", next(directory.glob("*.md")).read_text())
        self.assertEqual(next(directory.glob("*.md")).stat().st_mode & 0o777, 0o600)

    def test_project_lockfiles_and_missing_root(self):
        self.approve()
        project = self.home / "dev_workspace/lock-only"
        project.mkdir()
        (project / "package-lock.json").write_text(json.dumps({
            "name": "fixture", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {"node_modules/probe-node": {"version": "1.0.0"}}}))
        self.assertEqual(self.scan().returncode, 0)
        self.assertIn("package-lock.json", json.dumps(self.report["roots"][-1]))
        (self.npm / "osv-scanner.toml").unlink()
        (self.npm / "osv-scanner.toml").symlink_to(self.home / "outside-config")
        self.assertEqual(self.scan().returncode, 1)
        self.assertIn("approved config is symlinked", self.report["errors"][-1])

    def test_invalid_exemptions_fail_closed(self):
        for content in (
                '[[IgnoredVulns]]\nid="PROBE-2026-1"\nreason="missing expiry"\n',
                '[[IgnoredVulns]]\nid="PROBE-2026-1"\nignoreUntil=2026-11-01\n',
                '[[PackageOverrides]]\nname="probe-python"\nignore=true\n',
                '[[IgnoredVulns]]\nid="PROBE-2026-1"\nreason="forever"\nignoreUntil=2099-01-01\n',
                '[[IgnoredVulns]]\nid=""\nreason="empty ID"\nignoreUntil=2026-11-01\n'):
            with self.subTest(config=content):
                (self.npm / "osv-scanner.toml").write_text(content)
                self.assertEqual(self.scan().returncode, 1)
                self.assertEqual(self.report["status"], "failed")
                self.assertTrue(self.report["errors"])
                self.assertFalse((self.home / "refresh").exists())

    def test_project_and_nested_configs_cannot_suppress_findings(self):
        content = '[[IgnoredVulns]]\nid="PROBE-2026-1"\nreason="planted"\n'
        for relative in ("dev_workspace", "dev_workspace/public",
                         "dev_workspace/public/.venv", ".local/share/bootstrap/npm/node_modules"):
            (self.home / relative / "osv-scanner.toml").write_text(content)
        project = self.home / "dev_workspace/public"
        (project / "package-lock.json").write_text(json.dumps({
            "name": "fixture", "lockfileVersion": 3,
            "packages": {"node_modules/probe-node": {"version": "1.0.0"}}}))
        self.assertEqual(self.scan().returncode, 1)
        self.assertEqual(self.report["exemptions"], [])
        self.assertEqual(self.report["roots"][-1]["status"], "findings")
        self.assertIn("PROBE-2026-1", json.dumps(self.report["roots"][-1]))
        # With the approved file present, it alone governs all scanned roots.
        self.approve()
        self.assertEqual(self.scan().returncode, 0)
        self.assertNotIn("planted", json.dumps(self.report["exemptions"]))

    def fake_scanner(self):
        (self.bin / "osv-scanner").unlink()
        path = self.bin / "osv-scanner"
        path.write_text('''#!/usr/bin/python3
import json, os, signal, sys
from pathlib import Path
if "--version" in sys.argv:
    print("osv-scanner version: 2.6.0")
    sys.exit(0)
assert sys.argv.count("--config") == 1
assert Path(sys.argv[sys.argv.index("--config") + 1]).is_file()
root = sys.argv[-1]
with (Path.home() / "scans").open("a") as log:
    log.write(root + "\\n")
mode = os.environ.get("SCAN_MODE", "empty")
if mode == "terminate":
    os.kill(os.getppid(), signal.SIGTERM)
    sys.exit(0)
names = ["@openai/codex", "pre-commit", "inspect-ai", "probe-python"]
if mode == "missing-sentinel":
    names = ["unrelated"]
if mode == "diagnostic":
    print("ERROR extractor: unreadable directory", file=sys.stderr)
packages = [{"package": {"name": name, "version": "1.0", "ecosystem": "PyPI"}}
            for name in names]
if "SCAN_COUNT" in os.environ:
    packages = [packages[i % len(packages)] for i in range(int(os.environ["SCAN_COUNT"]))]
print(json.dumps({"results": [] if mode == "empty" else [{"packages": packages}]}))
''')
        path.chmod(0o755)

    def test_empty_partial_and_diagnostic_errors_never_clean(self):
        self.fake_scanner()
        for mode, message in (("empty", "empty package results"),
                              ("missing-sentinel", "missing sentinel package"),
                              ("diagnostic", "diagnostics contain errors")):
            with self.subTest(mode=mode):
                self.assertEqual(self.scan(SCAN_MODE=mode).returncode, 1)
                self.assertEqual(self.report["status"], "failed")
                self.assertIn(message, self.report["roots"][0]["error"])
                if mode == "missing-sentinel":
                    for entry in self.report["roots"][:3]:
                        self.assertIn("missing sentinel", entry["error"])
                self.assertEqual(len(self.report["roots"]), 5)

    def test_empty_workspace_is_failure_with_real_scanner(self):
        self.approve()
        (self.home / "dev_workspace/public/.venv/lib/python3.12/site-packages/"
         "probe_python-1.0.dist-info/METADATA").unlink()
        self.assertEqual(self.scan().returncode, 1)
        self.assertEqual(self.report["roots"][-1]["status"], "error")

    def test_package_count_drop_against_previous_latest(self):
        self.approve()
        for index in range(8):
            node_package(self.npm, f"extra-{index}")
        self.assertEqual(self.scan().returncode, 0)
        self.assertEqual(self.report["roots"][0]["package_count"], 10)
        # Upgrade an older report: only its accepted root counts seed the map.
        del self.report["last_clean_counts"]
        (self.home / ".local/state/security/latest.json").write_text(json.dumps(self.report))
        for index in range(8):
            (self.npm / f"node_modules/extra-{index}/package.json").unlink()
        self.assertEqual(self.scan().returncode, 1)
        self.assertIn("sharp package count drop: 10 to 2", self.report["roots"][0]["error"])
        self.assertEqual(self.report["roots"][1]["status"], "clean")

    def test_package_count_drop_keeps_last_clean_baseline(self):
        self.fake_scanner()
        self.assertEqual(self.scan(SCAN_MODE="healthy", SCAN_COUNT="500").returncode, 0)
        self.assertEqual(self.report["roots"][-1]["package_count"], 500)
        baseline = self.report["last_clean_counts"]
        for day in (2, 3):
            with self.subTest(day=day):
                self.assertEqual(self.scan(SCAN_MODE="healthy", SCAN_COUNT="10").returncode, 1)
                entry = self.report["roots"][-1]
                self.assertEqual(entry["package_count"], 10)
                self.assertEqual(entry["status"], "error")
                self.assertIn("sharp package count drop: 500 to 10", entry["error"])
                self.assertEqual(self.report["last_clean_counts"], baseline)
                self.assertEqual((self.home / "pings").read_text().splitlines()[-1],
                                 "security-watch 1")
        self.assertEqual(self.scan(SCAN_MODE="healthy", SCAN_COUNT="600").returncode, 0)
        self.assertTrue(all(count == 600 for count in self.report["last_clean_counts"].values()))

    def test_rejected_and_interrupted_scans_preserve_baseline(self):
        self.fake_scanner()
        self.assertEqual(self.scan(SCAN_MODE="healthy", SCAN_COUNT="500").returncode, 0)
        root = ".local/share/bootstrap/npm"
        for mode in ("empty", "diagnostic", "missing-sentinel", "terminate"):
            with self.subTest(mode=mode):
                self.assertNotEqual(self.scan(SCAN_MODE=mode, SCAN_COUNT="5000").returncode, 0)
                self.assertEqual(self.report["last_clean_counts"][root], 500)
                self.assertEqual(self.scan(SCAN_MODE="healthy", SCAN_COUNT="10").returncode, 1)
                self.assertIn("sharp package count drop: 500 to 10",
                              self.report["roots"][0]["error"])

    def test_missing_real_sentinels(self):
        self.approve()
        for relative in ("node_modules/@openai/codex/package.json",):
            (self.npm / relative).unlink()
        for tool, name in (("pre-commit", "pre_commit"), ("inspect", "inspect_ai")):
            (self.home / f".local/share/bootstrap/{tool}/venv/lib/python3.12/"
             f"site-packages/{name}-1.0.dist-info/METADATA").unlink()
        self.assertEqual(self.scan().returncode, 1)
        for entry in self.report["roots"][:3]:
            self.assertIn("missing sentinel", entry["error"])

    def test_sigterm_overwrites_old_clean_and_stops(self):
        self.approve()
        self.assertEqual(self.scan().returncode, 0)
        self.fake_scanner()
        self.assertNotEqual(self.scan(SCAN_MODE="terminate").returncode, 0)
        self.assertEqual(self.report["status"], "incomplete")
        self.assertEqual(len((self.home / "scans").read_text().splitlines()), 1)
        self.assertEqual((self.home / "pings").read_text().splitlines()[-1], "security-watch 1")
        status = (self.home / "cortex/cortex-core/STATUS.md").read_text()
        self.assertIn(": incomplete.", status)
        directory = self.home / ".local/state/security"
        self.assertTrue(any(json.loads(p.read_text())["status"] == "incomplete"
                            for p in directory.glob("*.json")))
        self.assertTrue(any(": incomplete." in p.read_text() for p in directory.glob("*.md")))

    def test_initial_latest_is_incomplete_before_refresh_and_termination(self):
        self.approve()
        self.assertEqual(self.scan().returncode, 0)
        baseline = self.report["last_clean_counts"]
        path = self.bin / "systemctl"
        path.write_text('''#!/usr/bin/python3
import json, os, signal
from pathlib import Path
latest = Path.home() / ".local/state/security/latest.json"
assert json.loads(latest.read_text())["status"] == "incomplete"
(Path.home() / "saw-incomplete").touch()
os.kill(os.getppid(), signal.SIGTERM)
''')
        self.assertNotEqual(self.scan().returncode, 0)
        self.assertTrue((self.home / "saw-incomplete").exists())
        self.assertEqual(self.report["status"], "incomplete")
        self.assertEqual(self.report["roots"], [])
        self.assertEqual(self.report["last_clean_counts"], baseline)
        self.assertEqual((self.home / "pings").read_text().splitlines()[-1], "security-watch 1")

    def test_reports_precede_status_and_survive_decode_error(self):
        self.approve()
        status = self.home / "cortex/cortex-core/STATUS.md"
        status.write_bytes(b"\xff")
        original = Path.read_text
        observed = []

        def read(path, *args, **kwargs):
            if path == status:
                directory = self.home / ".local/state/security"
                latest = json.loads(original(directory / "latest.json"))
                self.assertEqual(latest["status"], "clean")
                self.assertTrue(list(directory.glob("*.md")))
                observed.append(True)
            return original(path, *args, **kwargs)

        namespace = runpy.run_path(str(SCRIPT))
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)
        with patch.dict(os.environ, self.env, clear=True), patch.object(Path, "read_text", read):
            self.assertEqual(namespace["main"](), 1)
        self.assertEqual(observed, [True])
        latest = json.loads((self.home / ".local/state/security/latest.json").read_text())
        self.assertEqual(latest["status"], "failed")
        self.assertIn("security update failed", latest["errors"][-1])
        self.assertTrue(any(": failed." in p.read_text()
                            for p in (self.home / ".local/state/security").glob("*.md")))


class Wiring(unittest.TestCase):
    def test_daily_units_and_gate(self):
        units = ROOT / "home/dot_config/systemd/user"
        timer = (units / "osv-db-refresh.timer").read_text()
        self.assertIn("OnCalendar=daily", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=security-watch.service", timer)
        self.assertFalse((units / "security-watch.timer").exists())
        self.assertFalse((units / "timers.target.wants/symlink_security-watch.timer").exists())
        self.assertIn("TimeoutStartSec=45min", (units / "security-watch.service").read_text())
        setup = (ROOT / "home/dot_local/share/bootstrap/executable_qmd-setup.sh").read_text()
        self.assertIn("enable --now osv-db-refresh.timer", setup)
        self.assertNotIn("security-watch.timer", setup)
        self.assertIn("run python3 ci/security-watch-test.py", (ROOT / "ci/lint.sh").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
