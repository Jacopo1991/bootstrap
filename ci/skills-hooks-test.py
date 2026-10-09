#!/usr/bin/env python3
"""Skills distribution (gh skill install and the daily update timer), the per-prompt
reminder hook, and the static shape of the Windows skills task and minutes watchdog.

The Windows threshold logic itself is tested in ci/founder-tasks-test.ps1 (Windows PowerShell 5.1).
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "home/dot_config/systemd/user"
UPDATE = ROOT / "home/dot_local/bin/executable_gh-skill-update"
SETUP = ROOT / "home/dot_local/share/bootstrap/executable_skills-setup.sh"
HOOK = ROOT / "home/dot_local/share/bootstrap/agent-policy/context_reminder.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unit(name: str) -> dict[str, dict[str, str]]:
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


class Units(unittest.TestCase):
    def test_service_only_updates(self):
        service = unit("gh-skill-update.service")
        self.assertEqual(service["Service"]["Type"], "oneshot")
        self.assertEqual(service["Service"]["ExecStart"], "%h/.local/bin/gh-skill-update")
        text = (UNITS / "gh-skill-update.service").read_text()
        self.assertEqual(len([l for l in text.splitlines() if l.startswith("Exec")]), 1)
        self.assertNotIn("claude", text)
        self.assertNotIn("codex", text)

    def test_timer_daily_persistent_and_enabled(self):
        timer = unit("gh-skill-update.timer")
        self.assertEqual(timer["Timer"]["OnCalendar"], "daily")
        self.assertEqual(timer["Timer"]["Persistent"], "true")
        self.assertEqual(timer["Install"]["WantedBy"], "timers.target")
        link = UNITS / "timers.target.wants/symlink_gh-skill-update.timer"
        self.assertEqual(link.read_text().strip(), "../gh-skill-update.timer")

    def test_systemd_accepts_the_calendar(self):
        result = subprocess.run(["systemd-analyze", "calendar", "daily"], capture_output=True, text=True)
        if result.returncode == 127:
            self.skipTest("systemd-analyze not available")
        self.assertEqual(result.returncode, 0, result.stderr)


class Install(unittest.TestCase):
    def test_install_runs_skills_setup_as_agent_after_chezmoi(self):
        install = (ROOT / "install.sh").read_text()
        self.assertLess(install.index("chezmoi --source"), install.index("skills-setup.sh"))
        line = install[install.index("skills-setup.sh") - 400:install.index("skills-setup.sh")]
        self.assertIn("sudo -H -u agent", line)

    def test_setup_installs_all_skills_for_both_agents_with_force_once(self):
        setup = SETUP.read_text()
        self.assertIn("gh skill install \"$repository\" --all --agent \"$agent\" --scope user --force", setup)
        self.assertIn("for agent in claude-code codex", setup)
        self.assertIn("repository=Jacopo1991/cortex-core", setup)
        self.assertEqual(len(re.findall(r"^\s*gh skill install .*--force", setup, re.M)), 1)
        self.assertIn('if [[ ! -e $stamp ]]', setup)  # --force only on the first install
        steps = ["enable --now gh-skill-update.timer", "is-active --quiet gh-skill-update.timer"]
        positions = [setup.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))

    def test_drift_allowlist_knows_the_units_command_and_the_skills(self):
        drift = load(ROOT / "checks/agent-drift.py", "agent_drift")
        self.assertIn("gh-skill-update", drift.EXPECTED_BINARIES)
        with tempfile.TemporaryDirectory() as temp:
            skills = Path(temp)
            for name, header in (("github-ci", "metadata:\n  github-repo: https://github.com/Jacopo1991/cortex-core\n"),
                                 ("hand-copied", "name: hand-copied\n"),
                                 ("other-source", "metadata:\n  github-repo: https://github.com/someone/else\n")):
                (skills / name).mkdir()
                (skills / name / "SKILL.md").write_text(f"---\nname: {name}\n{header}---\nbody Jacopo1991/cortex-core\n")
            (skills / "no-file").mkdir()
            self.assertEqual(drift.unexpected_skills(skills), ["hand-copied", "no-file", "other-source"])
            self.assertEqual(drift.unexpected_skills(skills / "missing"), [])


# Behaves like the real `gh skill` (checked against gh with the live repository): the listing is
# "name<TAB>description" on stdout; installing by name refuses with exit 1 when the skill is
# already there; --all refuses as soon as any skill is present, so the script must never use it.
FAKE_GH = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
case "$*" in *--all*|*--force*|*" -f"*) [ "$2 $3" = "update --all" ] || { echo "unexpected: $*" >&2; exit 64; } ;; esac
if [ "$1 $2" = "skill install" ]; then
  if [ $# -eq 3 ]; then
    echo "Using ref v0.5.0 (146f526b)" >&2
    [ "${FAKE_LIST_EXIT:-0}" = 0 ] || { echo "could not resolve version" >&2; exit "$FAKE_LIST_EXIT"; }
    for name in $FAKE_SKILLS; do printf '%s\\tDescription of %s. Use for things.\\n' "$name" "$name"; done
    exit 0
  fi
  [ "$5 $7 $8" = "--agent --scope user" ] || { echo "unexpected: $*" >&2; exit 64; }
  key="$4 $6"
  case " $FAKE_INSTALLED " in *" $key "*) echo "skills already installed: $4 (use --force to overwrite)" >&2; exit 1 ;; esac
  case " $FAKE_BROKEN " in *" $key "*) echo "boom installing $4" >&2; exit 2 ;; esac
  echo "Installed $4"
  exit 0
fi
[ "$1 $2 $3" = "skill update --all" ] || { echo "unexpected: $*" >&2; exit 64; }
printf '%s\\n' "$FAKE_OUTPUT"
exit "${FAKE_EXIT:-0}"
"""


class UpdateCommand(unittest.TestCase):
    ALL = "github-ci research slim-workflow"
    BOTH = " ".join(f"{n} {a}" for n in ALL.split() for a in ("claude-code", "codex"))

    def run_update(self, output: str = "Updated github-ci", code: int = 0, skills: str = ALL,
                   installed: str = BOTH, **extra: str):
        temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name, text in (("gh", FAKE_GH), ("job-ping", '#!/bin/sh\necho "$@" >> "$FAKE_PINGS"\n')):
            tool = temp / name
            tool.write_text(text)
            tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
        self.log, self.pings = temp / "gh-calls", temp / "pings"
        env = dict(os.environ, PATH=f"{temp}:{os.environ['PATH']}", FAKE_OUTPUT=output, FAKE_EXIT=str(code),
                   FAKE_SKILLS=skills, FAKE_INSTALLED=installed, FAKE_LOG=str(self.log), FAKE_PINGS=str(self.pings),
                   JOB_PINGS_FILE=str(temp / "no-pings.env"), **extra)
        return subprocess.run(["bash", str(UPDATE)], env=env, capture_output=True, text=True, timeout=30)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def pinged(self) -> list[str]:
        return self.pings.read_text().splitlines() if self.pings.exists() else []

    def test_success(self):
        result = self.run_update("Updated github-ci")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Skills current.", result.stdout)
        self.assertEqual(self.pinged(), ["gh-skill-update 0"])

    def test_nothing_new_installs_nothing_and_updates_last(self):
        self.assertEqual(self.run_update().returncode, 0)
        calls = self.calls()
        self.assertEqual(calls[0], "skill install Jacopo1991/cortex-core")
        self.assertEqual(calls[-1], "skill update --all")
        self.assertEqual(len(calls), 1 + 3 * 2 + 1)  # listing, every skill for both agents, update
        self.assertFalse([c for c in calls if "--all" in c and c != "skill update --all"])
        self.assertFalse([c for c in calls if "--force" in c])

    def test_new_skill_is_installed_for_both_agents_without_force_then_updated(self):
        result = self.run_update(skills="github-ci research slim-workflow brand-new",
                                 installed=self.BOTH)
        self.assertEqual(result.returncode, 0, result.stderr)
        installs = [c for c in self.calls() if c.startswith("skill install ") and "brand-new" in c]
        self.assertEqual(installs, ["skill install Jacopo1991/cortex-core brand-new --agent claude-code --scope user",
                                    "skill install Jacopo1991/cortex-core brand-new --agent codex --scope user"])
        self.assertIn("Installed new skill brand-new for claude-code.", result.stdout)
        self.assertIn("Installed new skill brand-new for codex.", result.stdout)
        self.assertEqual(self.calls()[-1], "skill update --all")
        self.assertEqual(self.pinged(), ["gh-skill-update 0"])

    def test_skill_missing_for_one_agent_only_is_installed_for_that_agent(self):
        installed = self.BOTH.replace("research codex", "")
        result = self.run_update(installed=installed)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Installed new skill research for codex.", result.stdout)
        self.assertNotIn("for claude-code", result.stdout)

    def test_failed_install_fails_the_run_but_the_update_still_runs(self):
        result = self.run_update(skills="github-ci brand-new", installed="github-ci claude-code github-ci codex",
                                 FAKE_BROKEN="brand-new codex")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR installing new skill brand-new for codex", result.stderr)
        self.assertIn("boom installing brand-new", result.stdout)
        self.assertEqual(self.calls()[-1], "skill update --all")
        self.assertEqual(self.pinged(), ["gh-skill-update 1"])

    def test_listing_failure_fails_the_run_but_the_update_still_runs(self):
        result = self.run_update(FAKE_LIST_EXIT="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR", result.stderr)
        self.assertEqual(self.calls(), ["skill install Jacopo1991/cortex-core", "skill update --all"])
        self.assertEqual(self.pinged(), ["gh-skill-update 1"])

    def test_empty_listing_is_a_failure(self):
        result = self.run_update(skills="")
        self.assertEqual(result.returncode, 1)
        self.assertIn("listed no skills", result.stderr)

    def test_nonzero_exit_fails_and_says_so(self):
        result = self.run_update("could not reach github.com", 1)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR", result.stderr)
        self.assertIn("could not reach github.com", result.stdout)
        self.assertEqual(self.pinged(), ["gh-skill-update 1"])

    def test_reported_failure_fails_even_with_exit_zero(self):
        result = self.run_update("failed to update github-ci")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR", result.stderr)

    def test_no_failure_false_positive_on_ordinary_names(self):
        self.assertEqual(self.run_update("Updated terror-handling").returncode, 0)

    def test_works_without_job_ping(self):
        temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        gh = temp / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        log = temp / "log"
        env = dict(os.environ, PATH=f"{temp}:/usr/bin:/bin", FAKE_OUTPUT="ok", FAKE_SKILLS=self.ALL,
                   FAKE_INSTALLED=self.BOTH, FAKE_LOG=str(log))
        result = subprocess.run(["/bin/bash", str(UPDATE)], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


def hook(event: dict) -> dict | None:
    result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


class Reminder(unittest.TestCase):
    def test_prompt_block_is_short_and_complete(self):
        out = hook({"hook_event_name": "UserPromptSubmit", "prompt": "do the task"})
        specific = out["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "UserPromptSubmit")
        text = specific["additionalContext"]
        self.assertLessEqual(len(text.splitlines()), 8)
        for needle in ("slim-workflow", "two failed attempts", "time box", "chat", "PR description",
                       "git/gh network commands", "on their own", "git -C <path>", "verify-ui", "github-ci",
                       ".github"):
            self.assertIn(needle, text)

    def test_prompt_text_is_never_echoed(self):
        out = hook({"hook_event_name": "UserPromptSubmit", "prompt": "SECRET-PROMPT-TEXT"})
        self.assertNotIn("SECRET-PROMPT-TEXT", json.dumps(out))

    def tool(self, name: str, **tool_input):
        return hook({"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": tool_input})

    def test_workflow_files_get_the_github_ci_reminder(self):
        for name, key in (("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path")):
            out = self.tool(name, **{key: "/repo/.github/workflows/gate.yml"})
            self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")
            self.assertIn("github-ci", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.tool("Write", file_path="/repo/.github/CODEOWNERS"))
        self.assertIsNone(self.tool("Write", file_path="/repo/src/app.py"))

    def test_bash_touching_workflows_or_changing_settings(self):
        for command in ("cp ci.yml .github/workflows/ci.yml", "git add .github/workflows/gate.yml",
                        "gh repo edit --enable-auto-merge", "gh secret set TOKEN", "gh workflow run gate.yml",
                        "gh workflow disable gate.yml", "gh ruleset list",
                        "gh api -X PATCH repos/Jacopo1991/bootstrap -f description=x",
                        "gh api repos/Jacopo1991/bootstrap/actions/permissions -f enabled=false",
                        "gh api --method PUT repos/Jacopo1991/bootstrap/branches/main/protection",
                        "/usr/bin/gh repo edit --visibility private"):
            out = self.tool("Bash", command=command)
            self.assertIsNotNone(out, command)
            self.assertIn("github-ci", out["hookSpecificOutput"]["additionalContext"], command)

    def test_bash_reads_and_ordinary_commands_stay_quiet(self):
        for command in ("ls", "git status", "gh pr list", "gh issue view 3", "gh repo view Jacopo1991/bootstrap",
                        "gh api /users/Jacopo1991/settings/billing/usage",
                        "gh api repos/Jacopo1991/bootstrap/actions/runs",
                        "gh skill update --all", "echo 'unterminated"):
            self.assertIsNone(self.tool("Bash", command=command), command)

    def test_bad_input_never_fails(self):
        for stdin in ("", "not json", "[]", '{"hook_event_name":"PreToolUse","tool_input":7}'):
            result = subprocess.run([sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "")

    def test_wiring(self):
        command = "python3 ~/.local/share/bootstrap/agent-policy/context_reminder.py"
        claude = json.loads((ROOT / "home/dot_claude/settings.json").read_text())["hooks"]
        prompt = [h["command"] for entry in claude["UserPromptSubmit"] for h in entry["hooks"]]
        self.assertEqual(prompt, [command])
        pre = [(e["matcher"], h["command"]) for e in claude["PreToolUse"] for h in e["hooks"]]
        self.assertIn(("Bash|Write|Edit|MultiEdit", command), pre)
        codex = json.loads((ROOT / "home/dot_codex/hooks.json").read_text())["hooks"]
        self.assertEqual([h["command"] for e in codex["UserPromptSubmit"] for h in e["hooks"]], [command])
        for events in (claude, codex):  # the policy hook is still wired for every tool event it was
            self.assertTrue(any("pre_tool_use.py" in h["command"] for e in events["PreToolUse"] for h in e["hooks"]))


class Windows(unittest.TestCase):
    def read(self, name: str) -> str:
        return (ROOT / "windows" / name).read_text()

    def test_watchdog_defaults_and_command(self):
        text = self.read("minutes-watchdog.ps1")
        self.assertIn("$IncludedMinutes = 3000", text)
        self.assertIn("$script:Thresholds = @(80, 50)", text)
        self.assertIn("gh.Source api \"/users/Jacopo1991/settings/billing/usage?year=", text)
        self.assertNotIn("Import-Module", text)
        self.assertNotIn("Install-Module", text)

    def test_installers_register_the_allowlisted_tasks(self):
        inventory = self.read("inventory.ps1")
        for installer, task, script in (("install-skills-task.ps1", "MachineBootstrap-Skills-Update", "skills-update.ps1"),
                                        ("install-minutes-watchdog-task.ps1", "MachineBootstrap-Minutes-Watchdog",
                                         "minutes-watchdog.ps1")):
            text = self.read(installer)
            self.assertIn(f"-Name '{task}'", text)
            self.assertIn(f"-ScriptFile '{script}'", text)
            self.assertTrue((ROOT / "windows" / script).is_file())
            self.assertIn(task, inventory)

    def test_skills_installer_matches_the_linux_install(self):
        text = self.read("install-skills-task.ps1")
        self.assertIn("skill install Jacopo1991/cortex-core --all --agent $agent --scope user --force", text)
        self.assertIn("'claude-code', 'codex'", text)
        self.assertEqual(len(re.findall(r"^\s*& \$gh\.Source skill install .*--force", text, re.M)), 1)
        self.assertIn("skill update --all", self.read("skills-update.ps1"))

    def test_daily_task_installs_new_skills_by_name_without_force_then_updates(self):
        text = "\n".join(l for l in self.read("skills-update.ps1").splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("--force", text)
        self.assertNotRegex(text, r"skill install[^\n]*--all")  # --all refuses once any skill is present
        self.assertIn("skill install $script:SkillsRepository 2>$null", text)  # the listing
        self.assertIn("skill install $script:SkillsRepository $name --agent $agent --scope user", text)
        self.assertIn("'claude-code', 'codex'", text)
        self.assertIn("already installed", text)
        self.assertLess(text.index("Install-NewSkills -Gh"), text.index("skill update --all 2>&1"))
        self.assertIn("Jacopo1991/cortex-core", text)

    def test_jobs_ping_their_own_checks(self):
        for script, check in (("skills-update.ps1", "windows-skills-update"),
                              ("minutes-watchdog.ps1", "windows-minutes-watchdog")):
            text = self.read(script)
            self.assertIn(f"Send-JobPing -Check '{check}' -Success ($exitCode -eq 0)", text)
            self.assertIn("job-ping.ps1", text)
            self.assertIn("exit $exitCode", text)
            self.assertLess(text.index("Send-JobPing -Check"), text.index("exit $exitCode"))

    def test_installers_copy_the_ping_script_next_to_the_task_scripts(self):
        for installer in ("install-skills-task.ps1", "install-minutes-watchdog-task.ps1"):
            text = self.read(installer)
            self.assertIn("(Join-Path $PSScriptRoot 'job-ping.ps1')", text)
            self.assertIn("'job-ping.ps1') -Force", text)
            self.assertLess(text.index("job-ping.ps1"), text.index("Register-FounderTask -Name"))

    def test_tests_are_chained_into_the_windows_job(self):
        self.assertIn("founder-tasks-test.ps1", (ROOT / "ci/windows-test.ps1").read_text())

    def test_fixture_matches_the_test_expectations(self):
        usage = json.loads((ROOT / "ci/fixtures/billing-usage.json").read_text())
        # Same rule as the watchdog: October only, Windows minutes count 2x, macOS 10x.
        weight = lambda sku: 2 if "windows" in sku else 10 if "mac" in sku else 1
        minutes = sum(i["quantity"] * weight(i.get("sku", "")) for i in usage["usageItems"]
                      if i["product"] == "actions" and i["unitType"] == "Minutes" and i["date"].startswith("2026-10"))
        self.assertEqual(minutes, 2100)


if __name__ == "__main__":
    unittest.main()
