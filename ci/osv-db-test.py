#!/usr/bin/env python3
"""Check the pre-commit gate's OSV data: the npm lock's accepted-risk exception and its
expiry, the weekly osv-db-refresh units and their enablement, and that osv-db-refresh
reports every failure, keeps the previous data and stamps only a full success.

The real-scanner checks run when osv-scanner and an offline npm database are present
(after install); everything else runs anywhere.
"""
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
NPM = ROOT / "home/dot_local/share/bootstrap/npm"
UNITS = ROOT / "home/dot_config/systemd/user"
REFRESH = ROOT / "home/dot_local/bin/executable_osv-db-refresh"
ADVISORY = "GHSA-vfj7-8cjw-p6xm"
EXPIRY = dt.date(2027, 1, 5)
ECOSYSTEMS = ["PyPI", "npm", "crates.io", "Go", "Packagist", "RubyGems"]
REAL_DB = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "osv-scalibr"


def units(name: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current = None
    for line in (UNITS / name).read_text().splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
        elif "=" in line and not line.startswith("#") and current is not None:
            key, value = line.split("=", 1)
            current[key] = value
    return sections


class IgnoreEntry(unittest.TestCase):
    def setUp(self):
        self.config = tomllib.loads((NPM / "osv-scanner.toml").read_text())

    def test_exactly_one_expiring_exception_with_a_reason(self):
        entries = self.config["IgnoredVulns"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["id"], ADVISORY)
        self.assertEqual(entry["ignoreUntil"], EXPIRY)
        self.assertIn("braces 3.0.3", entry["reason"])
        self.assertEqual(set(self.config), {"IgnoredVulns"})

    def test_exception_still_matches_the_lock(self):
        # When braces moves past 3.0.3 the exception is obsolete: remove it.
        lock = json.loads((NPM / "package-lock.json").read_text())
        self.assertEqual(lock["packages"]["node_modules/braces"]["version"], "3.0.3")


@unittest.skipUnless(shutil.which("osv-scanner") and (REAL_DB / "npm/all.zip").is_file(),
                     "osv-scanner or the offline npm database is not installed")
class IgnoreWithScanner(unittest.TestCase):
    """The hook's exact command, with the config found next to the lockfile."""

    def scan(self, config: str | None) -> subprocess.CompletedProcess:
        temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        shutil.copy(NPM / "package-lock.json", temp / "package-lock.json")
        if config is not None:
            (temp / "osv-scanner.toml").write_text(config)
        env = dict(os.environ, XDG_CACHE_HOME=str(REAL_DB.parent))
        return subprocess.run(["osv-scanner", "scan", "source", "--offline", "package-lock.json"],
                              cwd=temp, env=env, capture_output=True, text=True, timeout=120)

    def test_committed_exception_passes_until_it_expires(self):
        result = self.scan((NPM / "osv-scanner.toml").read_text())
        if dt.date.today() < EXPIRY:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(ADVISORY, result.stdout + result.stderr)  # reported as filtered
        else:
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_expired_exception_blocks_again(self):
        expired = (NPM / "osv-scanner.toml").read_text().replace(
            f"ignoreUntil = {EXPIRY.isoformat()}",
            f"ignoreUntil = {(dt.date.today() - dt.timedelta(days=1)).isoformat()}")
        self.assertNotEqual(expired, (NPM / "osv-scanner.toml").read_text())
        result = self.scan(expired)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(ADVISORY, result.stdout)

    def test_without_the_exception_the_lock_is_blocked(self):
        result = self.scan(None)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(ADVISORY, result.stdout)


class Units(unittest.TestCase):
    def test_service_only_downloads(self):
        service = units("osv-db-refresh.service")
        self.assertEqual(service["Service"]["Type"], "oneshot")
        self.assertEqual(service["Service"]["ExecStart"], "%h/.local/bin/osv-db-refresh")
        exec_lines = [line for line in (UNITS / "osv-db-refresh.service").read_text().splitlines()
                      if line.startswith("Exec")]
        self.assertEqual(len(exec_lines), 1)
        self.assertNotIn("claude", (UNITS / "osv-db-refresh.service").read_text())
        self.assertNotIn("codex", (UNITS / "osv-db-refresh.service").read_text())

    def test_timer_weekly_persistent_and_enabled(self):
        timer = units("osv-db-refresh.timer")
        self.assertEqual(timer["Timer"]["OnCalendar"], "weekly")
        self.assertEqual(timer["Timer"]["Persistent"], "true")
        self.assertEqual(timer["Install"]["WantedBy"], "timers.target")
        link = UNITS / "timers.target.wants/symlink_osv-db-refresh.timer"
        self.assertEqual(link.read_text().strip(), "../osv-db-refresh.timer")

    def test_enabled_by_the_qmd_timer_install_path(self):
        setup = (ROOT / "home/dot_local/share/bootstrap/executable_qmd-setup.sh").read_text()
        steps = ["enable --now qmd-index.timer", "enable --now osv-db-refresh.timer",
                 "is-active --quiet osv-db-refresh.timer"]
        positions = [setup.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("qmd-setup.sh", (ROOT / "install.sh").read_text())

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not available")
    def test_systemd_accepts_the_timer_calendar(self):
        result = subprocess.run(["systemd-analyze", "calendar", "weekly"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


FAKE_CURL = """#!/usr/bin/env python3
# Stand-in for curl: writes a small zip for the requested ecosystem, or fails.
import os, sys, zipfile
args = sys.argv[1:]
url = next(a for a in args if a.startswith("https://"))
out = args[args.index("-o") + 1]
ecosystem = url.split("/")[-2]
if ecosystem == os.environ.get("FAKE_FAIL"):
    sys.exit(22)
if ecosystem == os.environ.get("FAKE_CORRUPT"):
    open(out, "wb").write(b"not a zip")
    sys.exit(0)
with zipfile.ZipFile(out, "w") as z:
    z.writestr("ADVISORY.json", ecosystem + os.environ.get("FAKE_TAG", ""))
"""


class Refresh(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_dir = self.temp / "bin"
        bin_dir.mkdir()
        curl = bin_dir / "curl"
        curl.write_text(FAKE_CURL)
        curl.chmod(0o755)
        # Record job-ping calls instead of contacting Healthchecks.io.
        self.pings = self.temp / "pings"
        ping = bin_dir / "job-ping"
        ping.write_text(f'#!/bin/sh\necho "$@" >> {self.pings}\n')
        ping.chmod(0o755)
        self.cache = self.temp / "cache" / "osv-scalibr"
        self.env = dict(os.environ, XDG_CACHE_HOME=str(self.temp / "cache"),
                        JOB_PINGS_FILE=str(self.temp / "no-pings.env"),  # never ping for real from a test
                        PATH=f"{bin_dir}:{os.environ['PATH']}")

    def run_refresh(self, **fake: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(REFRESH)], env=dict(self.env, **fake),
                              capture_output=True, text=True, timeout=60)

    def zip_tag(self, ecosystem: str) -> str:
        import zipfile
        with zipfile.ZipFile(self.cache / ecosystem / "all.zip") as z:
            return z.read("ADVISORY.json").decode()

    def test_success_fetches_every_ecosystem_and_stamps(self):
        result = self.run_refresh()
        self.assertEqual(result.returncode, 0, result.stderr)
        for ecosystem in ECOSYSTEMS:
            self.assertEqual(self.zip_tag(ecosystem), ecosystem)
        self.assertTrue((self.cache / ".last-refresh").is_file())
        self.assertEqual(self.pings.read_text().splitlines(), ["osv-db-refresh 0"])

    def test_failures_are_reported_others_still_refresh_and_no_stamp(self):
        self.assertEqual(self.run_refresh().returncode, 0)
        (self.cache / ".last-refresh").unlink()
        result = self.run_refresh(FAKE_FAIL="npm", FAKE_CORRUPT="Go", FAKE_TAG="-new")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR npm", result.stderr)
        self.assertIn("ERROR Go", result.stderr)
        self.assertIn("NOT fully refreshed", result.stderr)
        # Failed ecosystems keep their previous database; the others are updated.
        self.assertEqual(self.zip_tag("npm"), "npm")
        self.assertEqual(self.zip_tag("Go"), "Go")
        self.assertEqual(self.zip_tag("PyPI"), "PyPI-new")
        self.assertEqual(list(self.cache.glob("*/*.part")), [])
        self.assertFalse((self.cache / ".last-refresh").exists())
        self.assertEqual(self.pings.read_text().splitlines(), ["osv-db-refresh 0", "osv-db-refresh 1"])

    def test_works_without_job_ping(self):
        (self.temp / "bin" / "job-ping").unlink()
        self.assertEqual(self.run_refresh().returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
