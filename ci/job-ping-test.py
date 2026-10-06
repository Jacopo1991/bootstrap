#!/usr/bin/env python3
"""job-ping (Healthchecks.io pings for the scheduled jobs): which URL is pinged, when it is
skipped silently, that a ping can never fail or delay a job, and that the URL never appears in
a command line. A fake curl records what it is given; one test uses the real curl against a
port nothing listens on to prove a dead network costs next to nothing.

The Windows twin (windows/job-ping.ps1) is tested in ci/founder-tasks-test.ps1 (needs PowerShell 5.1).
"""
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
JOB_PING = ROOT / "home/dot_local/bin/executable_job-ping"
URL = "https://hc-ping.example/11111111-2222-3333-4444-555555555555"

# Records argv and the --config text read from stdin; exits like FAKE_CURL_EXIT.
FAKE_CURL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_ARGS"
cat >> "$FAKE_CONFIG"
exit "${FAKE_CURL_EXIT:-0}"
"""


class JobPing(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_dir = self.temp / "bin"
        bin_dir.mkdir()
        curl = bin_dir / "curl"
        curl.write_text(FAKE_CURL)
        curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
        self.args, self.config, self.file = self.temp / "args", self.temp / "config", self.temp / "pings.env"
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_ARGS=str(self.args),
                        FAKE_CONFIG=str(self.config), JOB_PINGS_FILE=str(self.file))

    def ping(self, *args: str, env: dict | None = None):
        return subprocess.run(["bash", str(JOB_PING), *args], env=env or self.env, capture_output=True,
                              text=True, timeout=30)

    def urls(self) -> list[str]:
        return self.config.read_text().splitlines() if self.config.exists() else []

    def called(self) -> bool:
        return self.args.exists()

    def test_success_pings_the_check_url(self):
        self.file.write_text(f"QMD_INDEX_URL={URL}\n")
        for result in ("0", "ok", "success"):
            self.config.unlink(missing_ok=True)
            self.assertEqual(self.ping("qmd-index", result).returncode, 0)
            self.assertEqual(self.urls(), [f'url = "{URL}"'], result)

    def test_failure_pings_the_fail_url(self):
        self.file.write_text(f"QMD_INDEX_URL={URL}\n")
        for result in ("1", "2", "fail", "exit-code", "timeout", "00", "oK"):
            self.config.unlink(missing_ok=True)
            self.assertEqual(self.ping("qmd-index", result).returncode, 0)
            self.assertEqual(self.urls(), [f'url = "{URL}/fail"'], repr(result))

    def test_no_result_argument_means_success(self):
        self.file.write_text(f"OSV_DB_REFRESH_URL={URL}\n")
        self.ping("osv-db-refresh")
        self.assertEqual(self.urls(), [f'url = "{URL}"'])

    def test_each_check_uses_its_own_key(self):
        self.file.write_text("".join(f"{key}_URL=https://hc.example/{key.lower()}\n" for key in
                                     ("QMD_INDEX", "OSV_DB_REFRESH", "GH_SKILL_UPDATE", "QMD_INDEX_X")))
        for check, expected in (("qmd-index", "qmd_index"), ("osv-db-refresh", "osv_db_refresh"),
                                ("gh-skill-update", "gh_skill_update")):
            self.config.unlink(missing_ok=True)
            self.ping(check, "0")
            self.assertEqual(self.urls(), [f'url = "https://hc.example/{expected}"'])

    def test_value_cleanup_comments_quotes_crlf_and_last_key_wins(self):
        self.file.write_text('# a comment\nQMD_INDEX_URL=https://old.example/x\r\n'
                             'OTHER_URL=https://other.example/y\r\nQMD_INDEX_URL="https://hc.example/z/"\r\n')
        self.ping("qmd-index", "0")
        self.assertEqual(self.urls(), ['url = "https://hc.example/z"'])

    def test_last_line_without_newline(self):
        self.file.write_text(f"QMD_INDEX_URL={URL}")
        self.ping("qmd-index", "0")
        self.assertEqual(self.urls(), [f'url = "{URL}"'])

    def test_skipped_silently_when_nothing_to_ping(self):
        cases = {
            "no file": None,
            "other key only": "GH_SKILL_UPDATE_URL=https://hc.example/a\n",
            "key prefix is not the key": "QMD_INDEX_URL_EXTRA=https://hc.example/a\n",
            "empty value": "QMD_INDEX_URL=\n",
            "plain http": "QMD_INDEX_URL=http://hc.example/a\n",
            "not a URL": "QMD_INDEX_URL=hello\n",
            "ftp": "QMD_INDEX_URL=ftp://hc.example/a\n",
            "space in URL": "QMD_INDEX_URL=https://hc.example/a b\n",
            "quote in URL": 'QMD_INDEX_URL=https://hc.example/a"b\n',
            "backslash in URL": "QMD_INDEX_URL=https://hc.example/a\\b\n",
            "commented out": "#QMD_INDEX_URL=https://hc.example/a\n",
        }
        for label, text in cases.items():
            self.args.unlink(missing_ok=True)
            self.file.unlink(missing_ok=True)
            if text is not None:
                self.file.write_text(text)
            result = self.ping("qmd-index", "0")
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""), label)
            self.assertFalse(self.called(), label)

    def test_bad_check_names_are_ignored(self):
        self.file.write_text(f"QMD_INDEX_URL={URL}\n")
        for check in ("", "Qmd-Index", "-qmd", "qmd index", "qmd;id", "../x", "qmd_index"):
            result = self.ping(check, "0") if check else self.ping()
            self.assertEqual(result.returncode, 0, check)
            self.assertFalse(self.called(), check)

    def test_unreadable_file_is_skipped(self):
        if os.geteuid() == 0:
            self.skipTest("root reads everything")
        self.file.write_text(f"QMD_INDEX_URL={URL}\n")
        self.file.chmod(0)
        self.assertEqual(self.ping("qmd-index", "0").returncode, 0)
        self.assertFalse(self.called())

    def test_default_file_is_under_project_data(self):
        text = JOB_PING.read_text()
        self.assertIn("$HOME/project-data/healthchecks/pings.env", text)
        home = self.temp / "home"
        (home / "project-data/healthchecks").mkdir(parents=True)
        (home / "project-data/healthchecks/pings.env").write_text(f"QMD_INDEX_URL={URL}\n")
        env = {k: v for k, v in self.env.items() if k != "JOB_PINGS_FILE"} | {"HOME": str(home)}
        self.ping("qmd-index", "0", env=env)
        self.assertEqual(self.urls(), [f'url = "{URL}"'])

    def test_a_failing_curl_never_fails_the_job(self):
        self.file.write_text(f"QMD_INDEX_URL={URL}\n")
        for code in ("6", "7", "28", "35", "56"):
            result = self.ping("qmd-index", "1", env=dict(self.env, FAKE_CURL_EXIT=code))
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""), code)

    def test_missing_curl_never_fails_the_job(self):
        self.file.write_text(f"QMD_INDEX_URL={URL}\n")
        bash = shutil.which("bash")
        env = dict(self.env, PATH=str(self.temp / "empty"))
        result = subprocess.run([bash, str(JOB_PING), "qmd-index", "0"], env=env, capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0)

    def test_url_is_never_in_a_command_line_or_output(self):
        self.file.write_text(f"QMD_INDEX_URL={URL}\n")
        result = self.ping("qmd-index", "1")
        self.assertNotIn("hc-ping", self.args.read_text())
        self.assertNotIn("11111111", self.args.read_text())
        self.assertIn("--config -", self.args.read_text())
        self.assertNotIn("11111111", result.stdout + result.stderr)

    def test_timeouts_are_short_and_there_are_no_retries(self):
        self.file.write_text(f"QMD_INDEX_URL={URL}\n")
        self.ping("qmd-index", "0")
        line = self.args.read_text()
        self.assertIn("--max-time 6", line)
        self.assertIn("--connect-timeout 3", line)
        self.assertIn("--retry 0", line)
        self.assertIn("--silent", line)

    @unittest.skipUnless(shutil.which("curl"), "curl is not installed")
    def test_dead_network_costs_next_to_nothing(self):
        self.file.write_text("QMD_INDEX_URL=https://127.0.0.1:1/ping\n")
        env = dict(self.env, PATH=os.environ["PATH"])  # real curl
        started = time.monotonic()
        result = self.ping("qmd-index", "0", env=env)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertLess(time.monotonic() - started, 8)


class Wiring(unittest.TestCase):
    """Each job calls job-ping with its own check name; the docs name every check."""

    def read(self, path: str) -> str:
        return (ROOT / path).read_text()

    def test_every_linux_job_pings_its_own_check(self):
        for path, check in (("home/dot_local/bin/executable_qmd-refresh", "qmd-index"),
                            ("home/dot_local/bin/executable_osv-db-refresh", "osv-db-refresh"),
                            ("home/dot_local/bin/executable_gh-skill-update", "gh-skill-update")):
            self.assertIn(f"job-ping {check} ", self.read(path), path)

    def test_job_ping_is_an_expected_binary(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("agent_drift", ROOT / "checks/agent-drift.py")
        drift = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(drift)
        self.assertIn("job-ping", drift.EXPECTED_BINARIES)

    def test_readme_names_every_check_and_key(self):
        readme = self.read("README.md")
        for check in ("qmd-index", "osv-db-refresh", "gh-skill-update", "windows-skills-update",
                      "windows-minutes-watchdog", "backrest-backup"):
            self.assertIn(f"`{check}`", readme, check)
            self.assertIn(check.upper().replace("-", "_") + "_URL", readme, check)
        self.assertIn("project-data/healthchecks/pings.env", readme)
        self.assertIn("machine-bootstrap\\pings.env", readme)

    def test_no_ping_url_is_committed(self):
        for path in ("home/dot_local/bin/executable_job-ping", "windows/job-ping.ps1", "README.md"):
            self.assertNotRegex(self.read(path), r"hc-ping\.com/[0-9a-f-]{8,}", path)


if __name__ == "__main__":
    unittest.main()
