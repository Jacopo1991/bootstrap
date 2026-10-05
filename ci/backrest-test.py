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

    def test_restic_pinned_and_installed_next_to_backrest(self):
        self.assertRegex(constant("ResticSha256"), r"^[0-9a-f]{64}$")
        version = constant("ResticVersion")
        self.assertIn(f"/download/{version}/", constant("ResticUrl"))
        self.assertIn(version.lstrip("v"), constant("ResticZipEntry"))
        self.assertIn("'restic.exe'", SCRIPT)
        self.assertEqual(SCRIPT.count("Get-FileHash"), 1)  # one shared verify-then-extract helper
        self.assertEqual(SCRIPT.count("Expand-VerifiedZip -Url"), 2)

    def test_launcher_is_explicit_about_paths(self):
        for needed in ("Set-Location -LiteralPath", "BACKREST_RESTIC_COMMAND", "BACKREST_CONFIG", "BACKREST_DATA", "BACKREST_PORT"):
            self.assertIn(needed, SCRIPT)

    def test_discovery_skips_worktrees_and_prints_sources(self):
        self.assertIn("-PathType Container", SCRIPT)
        self.assertIn("Write-Output 'Backup sources:'", SCRIPT)

    def test_live_smoke_is_opt_in(self):
        live = (ROOT / "ci/backrest-test.ps1").read_text()
        self.assertTrue(live.lstrip().startswith("param([switch]$Live)"))
        self.assertIn("if ($Live)", live)
        self.assertIn("FATAL", live)
        self.assertIn("StatusCode", live)
        self.assertIn("New-ResticPasswordFile", live)  # temp password file, as the installer does
        self.assertIn("repo\\config", live)  # temp repository initialized

    def test_logon_start_for_the_founder_not_elevated(self):
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", SCRIPT)
        self.assertIn("-RunLevel Limited", SCRIPT)
        self.assertIn("127.0.0.1:9898", SCRIPT)  # web UI stays on loopback

    def test_password_prompted_once_and_kept_in_owner_only_file(self):
        self.assertNotRegex(SCRIPT, r"(?i)\bpassword\s*=")
        self.assertNotIn("RESTIC_PASSWORD=", SCRIPT.replace("RESTIC_PASSWORD_FILE=", ""))
        self.assertIn("Read-Host -AsSecureString", SCRIPT)
        self.assertEqual(SCRIPT.count("Read-Host"), 2)  # typed twice
        self.assertIn("restic-password.txt", SCRIPT)
        # skipped when the file exists, and created before the config is written and Backrest started
        self.assertIn("if (Test-Path -LiteralPath $passwordFile)", SCRIPT)
        install = SCRIPT[SCRIPT.index("function Install-Backrest {"):]
        self.assertLess(install.index("New-ResticPasswordFile"), install.index("Start-ScheduledTask"))
        self.assertLess(install.index("New-ResticPasswordFile"), install.index("WriteAllText($configFile"))
        # ACL: file created empty, access inherited rules removed, only then the secret is written
        helper = SCRIPT[SCRIPT.index("function New-ResticPasswordFile"):SCRIPT.index("function Expand-VerifiedZip")]
        self.assertIn("SetAccessRuleProtection($true, $false)", helper)
        self.assertLess(helper.index("Set-Acl"), helper.index("WriteAllText($Path"))
        self.assertEqual(helper.count("FileSystemAccessRule"), 1)
        # the secret is never printed
        self.assertNotRegex(SCRIPT, r"Write-(Output|Host|Warning)[^\n]*\$(a|b|first|second|Password)\b")

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
        self.assertNotRegex(json.dumps(FIXTURE).lower(), r'"password"\s*:')

    def test_repo_reads_password_from_file_only(self):
        self.assertEqual(len(self.repo["env"]), 1)
        self.assertRegex(self.repo["env"][0], r"^RESTIC_PASSWORD_FILE=.+\\backrest\\restic-password\.txt$")
        self.assertNotIn("RESTIC_PASSWORD=", json.dumps(FIXTURE))

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
