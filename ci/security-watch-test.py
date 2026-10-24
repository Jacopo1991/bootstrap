#!/usr/bin/env python3
"""Fixture-only installed-environment probe and security-watch checks."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
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


def python_package(root):
    metadata = root / "lib/python3.12/site-packages/probe_python-1.0.dist-info/METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("Metadata-Version: 2.1\nName: probe-python\nVersion: 1.0\n")


def node_package(root):
    package = root / "node_modules/probe-node/package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({"name": "probe-node", "version": "1.0.0"}))


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
            metadata = root / "venv/lib/python3.12/site-packages/probe_python-1.0.dist-info/METADATA"
            metadata.parent.mkdir(parents=True)
            metadata.write_text("Metadata-Version: 2.1\nName: probe-python\nVersion: 1.0\n")
            package = root / "npm/node_modules/probe-node/package.json"
            package.parent.mkdir(parents=True)
            package.write_text(json.dumps({"name": "probe-node", "version": "1.0.0"}))
            for ecosystem, name, version in [("PyPI", "probe-python", "1.0"),
                                              ("npm", "probe-node", "1.0.0")]:
                target = root / ("venv" if ecosystem == "PyPI" else "npm")
                result = subprocess.run([scanner, "scan", "source", "--offline",
                                         "--offline-vulnerabilities", "--no-ignore", "-r",
                                         "--experimental-plugins=python/wheelegg,javascript/packagejson",
                                         "--format=json", "--all-packages", str(target)],
                                        cwd=root, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                data = json.loads(result.stdout)
                packages = [p for source in data["results"] for p in source["packages"]]
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
        for relative in (".local/share/bootstrap/pre-commit/venv",
                         ".local/share/bootstrap/inspect/venv", "dev_workspace/public/.venv"):
            python_package(self.home / relative)
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
        for relative in (".local/share/bootstrap/npm", ".local/share/bootstrap/pre-commit/venv",
                         ".local/share/bootstrap/inspect/venv", "dev_workspace"):
            (self.home / relative / "osv-scanner.toml").write_text(content)

    def test_findings_per_root_and_failure_ping(self):
        self.assertEqual(self.scan().returncode, 1)
        self.assertEqual([r["status"] for r in self.report["roots"]],
                         ["findings", "findings", "findings", "not installed", "findings"])
        self.assertEqual(self.report["errors"], [])
        self.assertEqual((self.home / "pings").read_text(), "security-watch 1\n")

    def test_native_expiry_and_success_status(self):
        self.ignore("2099-01-01")
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr + str(self.report))
        self.assertEqual((self.home / "pings").read_text(), "security-watch 0\n")
        status = (self.home / "cortex/cortex-core/STATUS.md").read_text()
        self.assertIn("## Keep\nunchanged", status)
        self.assertEqual(status.count("## Security"), 1)
        self.assertNotIn("\nold\n", status)
        self.ignore("2000-01-01")
        self.assertEqual(self.scan().returncode, 1)

    def test_refresh_failure_and_missing_database_never_clean(self):
        self.ignore("2099-01-01")
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
        self.ignore("2099-01-01")
        result = self.scan(FIXTURE_SCANNER=scanner)
        self.assertEqual(result.returncode, 0, result.stderr + str(self.report))
        self.assertEqual((self.home / "refresh").read_text(),
                         "--user start osv-db-refresh.service\n")

    def test_private_roots_and_symlink_targets_are_not_scanned(self):
        self.ignore("2099-01-01")
        for name in ("customer-harness", "typo3-dkm-plugin", "secrets"):
            node_package(self.home / "dev_workspace" / name)
        node_package(self.home / "outside")
        (self.home / "dev_workspace/public/linked").symlink_to(self.home / "outside")
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr + str(self.report))
        sources = json.dumps(self.report["roots"][-1])
        self.assertNotIn("probe-node", sources)

    def test_photo_organizer_venv_is_never_read(self):
        self.ignore("2099-01-01")
        python_package(self.home / "dev_workspace/photo-organizer/.venv")
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr + str(self.report))
        sources = json.dumps(self.report["roots"][-1])
        self.assertIn("public/.venv", sources)
        self.assertNotIn("photo-organizer", sources)

    def test_missing_root_and_status_write_failure(self):
        self.ignore("2099-01-01")
        (self.home / "cortex/cortex-core/STATUS.md").unlink()
        self.assertEqual(self.scan().returncode, 1)
        self.assertIn("security update failed", self.report["errors"][-1])
        self.assertEqual(self.report["status"], "failed")
        directory = self.home / ".local/state/security"
        self.assertIn(": failed.", next(directory.glob("*.md")).read_text())
        self.assertEqual(next(directory.glob("*.md")).stat().st_mode & 0o777, 0o600)

    def test_project_lockfiles_and_missing_root(self):
        self.ignore("2099-01-01")
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
        self.assertEqual(self.report["roots"][0]["status"], "error")
        self.assertEqual(self.report["roots"][1]["status"], "clean")


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
