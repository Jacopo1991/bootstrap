#!/usr/bin/env python3
"""Backrest backup install: static shape of windows/install-backrest.ps1 and the expected
generated config (ci/fixtures/backrest-config.expected.json), tied to the script's own constants.

Generating the config from PowerShell is tested in ci/backrest-test.ps1 (needs PowerShell).
A real install, restore and the \\\\wsl.localhost reads can only be proven on Windows.
"""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "windows/install-backrest.ps1").read_text()
FIXTURE = json.loads((ROOT / "ci/fixtures/backrest-config.expected.json").read_text())


def constant(name: str) -> str:
    match = re.search(rf"\$script:{name} = '([^']*)'", SCRIPT)
    assert match, name
    return match.group(1)


class ScriptShape(unittest.TestCase):
    def test_same_pattern_as_other_windows_scripts(self):
        self.assertIn('. "$PSScriptRoot/common.ps1"', SCRIPT)
        self.assertIn("Assert-BootstrapAdministrator", SCRIPT)
        self.assertIn("if ($MyInvocation.InvocationName -ne '.') { Install-Backrest }", SCRIPT)

    def test_pinned_release_with_hash_check_before_extract(self):
        self.assertRegex(constant("BackrestSha256"), r"^[0-9a-f]{64}$")
        version = constant("BackrestVersion")
        self.assertRegex(version, r"^v\d+\.\d+\.\d+$")
        self.assertIn(f"/download/{version}/", constant("BackrestUrl"))
        self.assertNotIn("latest", constant("BackrestUrl"))
        self.assertLess(SCRIPT.index("Get-FileHash"), SCRIPT.index("Expand-Archive"))
        self.assertIn("hash mismatch", SCRIPT)

    def test_logon_start_for_the_founder_not_elevated(self):
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", SCRIPT)
        self.assertIn("-RunLevel Limited", SCRIPT)
        self.assertIn("127.0.0.1:9898", SCRIPT)  # web UI stays on loopback

    def test_password_never_written(self):
        self.assertNotRegex(SCRIPT, r"(?i)\bpassword\s*=")
        self.assertNotIn("RESTIC_PASSWORD", SCRIPT)

    def test_existing_config_needs_force_and_is_backed_up(self):
        self.assertIn("-Force", SCRIPT)
        self.assertIn(".bak", SCRIPT)

    def test_no_agent_side_or_host_changes(self):
        for forbidden in ("cmd.exe", "pwsh", "Set-ExecutionPolicy", "/mnt/c"):
            self.assertNotIn(forbidden, SCRIPT)


class ExpectedConfig(unittest.TestCase):
    plan = FIXTURE["plans"][0]
    repo = FIXTURE["repos"][0]

    def test_constants_match_script(self):
        self.assertEqual(self.plan["schedule"]["cron"], constant("BackrestBackupCron"))
        self.assertEqual(self.repo["prunePolicy"]["schedule"]["cron"], constant("BackrestPruneCron"))
        self.assertEqual(self.repo["checkPolicy"]["schedule"]["cron"], constant("BackrestCheckCron"))
        self.assertEqual(self.repo["uri"], constant("BackrestRepoPath"))
        block = re.search(r"BackrestExcludes = @\((.*?)\n\)", SCRIPT, re.S).group(1)
        self.assertEqual(self.plan["excludes"], re.findall(r"'([^']+)'", block))

    def test_destination_and_no_password(self):
        self.assertEqual(self.repo["uri"], "C:\\backups\\restic")
        self.assertNotIn("password", json.dumps(FIXTURE).lower())

    def test_daily_0230_schedule_local_clock(self):
        self.assertEqual(self.plan["schedule"], {"cron": "30 2 * * *", "clock": "CLOCK_LOCAL"})

    def test_retention(self):
        self.assertEqual(self.plan["retention"], {"policyTimeBucketed": {"daily": 7, "weekly": 4, "monthly": 6}})

    def test_weekly_prune_and_check(self):
        for policy in ("prunePolicy", "checkPolicy"):
            fields = self.repo[policy]["schedule"]["cron"].split()
            self.assertEqual(fields[2:4], ["*", "*"])
            self.assertRegex(fields[4], r"^[0-6]$")

    def test_sources(self):
        root = "\\\\wsl.localhost\\AgentDev\\home\\agent\\"
        paths = self.plan["paths"]
        self.assertTrue(all(p.startswith(root) for p in paths))
        names = [p[len(root):] for p in paths]
        self.assertEqual(names[:2], ["project-data", "cortex"])
        workspace = [n.split("\\", 1)[1] for n in names[2:]]
        for local in ("consultancy-website", "customer-harness", "typo3-dkm-plugin"):
            self.assertIn(local, workspace)
        self.assertNotIn("bootstrap", workspace)
        self.assertTrue(all(n.startswith("dev_workspace\\") for n in names[2:]))

    def test_excludes(self):
        text = " ".join(self.plan["excludes"])
        for needed in ("node_modules", ".venv", ".cache", "dist", "build", "qmd"):
            self.assertIn(needed, text)

    def test_distro_started_before_each_backup(self):
        hook = self.plan["hooks"][0]
        self.assertEqual(hook["conditions"], ["CONDITION_SNAPSHOT_START"])
        self.assertEqual(hook["onError"], "ON_ERROR_CANCEL")
        self.assertEqual(hook["actionCommand"]["command"], "wsl.exe -d AgentDev -u agent -- true")


if __name__ == "__main__":
    unittest.main()
