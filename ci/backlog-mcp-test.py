#!/usr/bin/env python3
"""Check the Backlog.md MCP launcher: the repository allowlist, MCP over stdio against a
temporary Backlog project, the generated Claude app block, and that no agent-session config
references it. `app_block` is the format of the README block and claude-app-mcp.json."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "home/dot_local/bin/executable_agentdev-backlog-mcp"
PROBE = ROOT / "ci/backlog-mcp-probe.py"
APP_JSON = ROOT / "home/dot_local/share/bootstrap/claude-app-mcp.json"
BINARY = "backlog.md-linux-x64/backlog"
REAL_MODULES = Path.home() / ".local/share/bootstrap/npm/node_modules"
NODE = subprocess.run(["node", "-p", "process.execPath"], capture_output=True, text=True,
                      check=True).stdout.strip()
ALLOWED = ["cortex-kb-company", "cortex-kb-product-harness", "cortex-kb-household-signal-harness",
           "cortex-kb-madplanner-v2", "cortex-kb-consultancy-website", "cortex-kb-home-energy-lab",
           "cortex-kb-hermes", "cortex-kb-historical-ingestion", "cortex-kb-linkedin-ops", "cortex-kb-orw"]
PRIVATE = ["cortex-kb-typo3-dkm", "cortex-kb-customer-harness", "cortex-kb-photo-organizer"]
PAGE_TOOLS = ["task_view", "task_list", "task_search", "document_view"]
# Records how the launcher started it, then exits (stdin is closed in these tests).
STUB_SERVER = """#!/bin/sh
printf '%s\\n' "$PWD" "$@" > "$HOME/stub-start.txt"
env > "$HOME/stub-env.txt"
"""

spec = importlib.util.spec_from_file_location("agent_drift", ROOT / "checks/agent-drift.py")
drift = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drift)


def launcher_code():
    return "\n".join(line for line in LAUNCHER.read_text().splitlines() if not line.lstrip().startswith("#"))


def launcher_allowlist():
    return re.search(r"^repos=\(([^)]*)\)", launcher_code(), re.M).group(1).split()


def app_block(repos):
    """The Claude app config block: one backlog-<short name> entry per repository."""
    entries = [f'    "backlog-{repo.removeprefix("cortex-kb-")}": {{\n      "command": "wsl.exe",\n'
               f'      "args": ["-d", "AgentDev", "-u", "agent", "--", '
               f'"/home/agent/.local/bin/agentdev-backlog-mcp", "{repo}"]\n    }}' for repo in repos]
    return '{\n  "mcpServers": {\n' + ",\n".join(entries) + "\n  }\n}\n"


def launcher_env(home, **extra):
    # The launcher fixes PATH under HOME; give each explicit temporary environment node.
    shims = home / ".local/share/mise/shims"
    if not (shims / "node").exists():
        shims.mkdir(parents=True, exist_ok=True)
        (shims / "node").symlink_to(NODE)
    return {"HOME": str(home), "PATH": os.environ["PATH"], **extra}


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        stub = self.home / ".local/share/bootstrap/npm/node_modules" / BINARY
        stub.parent.mkdir(parents=True)
        stub.write_text(STUB_SERVER)
        stub.chmod(0o755)
        (stub.parent.parent / "yaml").symlink_to(REAL_MODULES / "yaml")
        self.started = self.home / "stub-start.txt"

    def tearDown(self):
        self.temp.cleanup()

    def make_repo(self, name, base=None):
        repo = (base or self.home / "cortex") / name
        (repo / "backlog").mkdir(parents=True)
        (repo / "backlog/config.yml").write_text("project_name: test\nremote_operations: false\n")
        return repo

    def start(self, *args, **extra):
        self.started.unlink(missing_ok=True)
        return subprocess.run(["bash", str(LAUNCHER), *args], env=launcher_env(self.home, **extra), cwd="/",
                              stdin=subprocess.DEVNULL, capture_output=True, text=True)

    def assert_refused(self, *args):
        result = self.start(*args)
        self.assertNotEqual(result.returncode, 0, args)
        self.assertEqual(result.stdout, "", args)
        self.assertFalse(self.started.exists(), f"server started for {args}")
        return result

    def test_each_allowed_name_starts_the_pinned_server_in_its_repository(self):
        for name in ALLOWED:
            repo = self.make_repo(name)
            result = self.start(name, DEBUG="1", BACKLOG_CWD="/", NODE_OPTIONS="--not-a-real-option")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")  # stdout belongs to MCP
            self.assertEqual(self.started.read_text().splitlines(),
                             [str(repo), "mcp", "start", "--cwd", str(repo)])
            env = dict(line.split("=", 1) for line in (self.home / "stub-env.txt").read_text().splitlines()
                       if "=" in line)
            self.assertNotIn("DEBUG", env)
            self.assertNotIn("BACKLOG_CWD", env)
            self.assertEqual(env["HOME"], str(self.home))

    def test_private_and_unknown_names_paths_and_dotdot_are_refused(self):
        for name in PRIVATE + ["cortex-kb-unknown"]:
            self.make_repo(name)
            self.assertIn("not an allowed knowledge repository", self.assert_refused(name).stderr)
        self.make_repo("cortex-kb-company")
        for arg in (str(self.home / "cortex/cortex-kb-company"), "cortex/cortex-kb-company",
                    "cortex-kb-company/", "../cortex/cortex-kb-company", "..", "cortex-kb-company/..",
                    "cortex-kb-company/../cortex-kb-typo3-dkm", ".", "", "CORTEX-KB-COMPANY", "cortex-kb-company\n"):
            self.assert_refused(arg)
        self.assert_refused()
        self.assert_refused("cortex-kb-company", "cortex-kb-orw")

    def test_symlinked_repository_or_backlog_folder_is_refused(self):
        private = self.make_repo("cortex-kb-typo3-dkm", Path(self.temp.name) / "elsewhere")
        (self.home / "cortex").mkdir()
        (self.home / "cortex/cortex-kb-company").symlink_to(private)
        self.assertIn("not a real directory", self.assert_refused("cortex-kb-company").stderr)
        (self.home / "cortex/cortex-kb-company").unlink()
        repo = self.home / "cortex/cortex-kb-company"
        repo.mkdir()
        (repo / "backlog").symlink_to(private / "backlog")
        self.assert_refused("cortex-kb-company")
        (repo / "backlog").unlink()
        (repo / "backlog").mkdir()
        (repo / "backlog/config.yml").symlink_to(private / "backlog/config.yml")
        self.assert_refused("cortex-kb-company")

    def test_symlinked_cortex_folder_is_refused(self):
        other = Path(self.temp.name) / "other-cortex"
        self.make_repo("cortex-kb-company", other)
        (self.home / "cortex").symlink_to(other)
        self.assert_refused("cortex-kb-company")

    def test_symlink_in_home_path_is_refused(self):
        self.make_repo("cortex-kb-company")
        alias = Path(self.temp.name) / "home-alias"
        alias.symlink_to(self.home, target_is_directory=True)
        self.home = alias
        self.assertIn("symlink in repository path", self.assert_refused("cortex-kb-company").stderr)

    def test_nested_file_and_directory_symlinks_outside_repository_are_refused(self):
        repo = self.make_repo("cortex-kb-company")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        secret = outside / "doc-1.md"
        secret.write_text("outside must stay untouched\n")
        nested = repo / "backlog/docs/deep"
        nested.mkdir(parents=True)
        for target in (secret, outside):
            with self.subTest(target=target):
                link = nested / "escape.md"
                link.symlink_to(target)
                result = self.assert_refused("cortex-kb-company")
                self.assertIn("backlog symlink escapes repository", result.stderr)
                self.assertEqual(secret.read_text(), "outside must stay untouched\n")
                link.unlink()

    def test_internal_symlinks_and_cycles_stay_inside_repository(self):
        repo = self.make_repo("cortex-kb-company")
        docs = repo / "backlog/docs"
        docs.mkdir()
        (repo / "local.md").write_text("local document\n")
        (docs / "doc-1.md").symlink_to(repo / "local.md")
        (docs / "cycle").symlink_to(repo / "backlog")
        self.assertEqual(self.start("cortex-kb-company").returncode, 0)

    def test_checks_are_repeated_after_an_initial_safe_launch(self):
        repo = self.make_repo("cortex-kb-company")
        self.assertEqual(self.start("cortex-kb-company").returncode, 0)
        (repo / "backlog/new-link").symlink_to(Path(self.temp.name))
        self.assertIn("backlog symlink escapes repository", self.assert_refused("cortex-kb-company").stderr)
        (repo / "backlog/new-link").unlink()
        self.assertEqual(self.start("cortex-kb-company").returncode, 0)
        with (repo / "backlog/config.yml").open("a") as config:
            config.write("onStatusChange: 'touch should-never-exist'\n")
        self.assertIn("command hook", self.assert_refused("cortex-kb-company").stderr)
        self.assertFalse((repo / "should-never-exist").exists())

    def test_project_callback_spellings_and_scalar_parser_edge_cases_are_refused(self):
        repo = self.make_repo("cortex-kb-company")
        config = repo / "backlog/config.yml"
        for hook in ("onStatusChange: 'touch should-never-exist'", "on_status_change: echo callback",
                     "  onStatusChange: echo indented", "onStatusChange: |\n  echo multiline"):
            with self.subTest(hook=hook):
                config.write_text("project_name: test\nremote_operations: false\n" + hook + "\n")
                self.assertIn("command hook", self.assert_refused("cortex-kb-company").stderr)
        self.assertFalse((repo / "should-never-exist").exists())

    def test_task_frontmatter_callbacks_in_all_stores_are_refused(self):
        repo = self.make_repo("cortex-kb-company")
        for store in ("tasks", "drafts", "completed", "archive/tasks"):
            task = repo / "backlog" / store / "task-1.md"
            task.parent.mkdir(parents=True)
            for metadata in ("onStatusChange: echo callback", '"onStatusChange": echo quoted',
                             '"onStatus\\u0043hange": echo escaped',
                             "onStatusChange: >-\n  echo multiline",
                             "defaults: &callback\n  onStatusChange: echo merged\n<<: *callback"):
                with self.subTest(store=store, metadata=metadata):
                    task.write_text("---\nid: TASK-1\n" + metadata + "\n---\nBody\n")
                    self.assertIn("command hook onStatusChange in task metadata",
                                  self.assert_refused("cortex-kb-company").stderr)
            task.unlink()

    def test_metadata_is_parsed_without_executing_frontmatter_engines(self):
        repo = self.make_repo("cortex-kb-company")
        task = repo / "backlog/task-1.md"
        for header in ("---js\n({onStatusChange: 'echo unsafe'})\n---\n", "---\nid: [broken\n---\n"):
            task.write_text(header)
            self.assert_refused("cortex-kb-company")
        task.write_text("---\nid: TASK-1\nonStatusChange: ''\n---\nBody mentions onStatusChange: echo text\n")
        self.assertEqual(self.start("cortex-kb-company").returncode, 0)

    def test_auto_commit_and_remote_operations_including_defaults_are_refused(self):
        repo = self.make_repo("cortex-kb-company")
        config = repo / "backlog/config.yml"
        for setting, reason in (("remote_operations: false\nauto_commit: true", "auto_commit enabled"),
                                ("remote_operations: false\n  auto_commit: TRUE", "auto_commit enabled"),
                                ("remote_operations: true", "remote_operations must be explicitly false"),
                                ("", "remote_operations must be explicitly false"),
                                ("remote_operations: false\nremote_operations: true",
                                 "remote_operations must be explicitly false")):
            with self.subTest(setting=setting):
                config.write_text("project_name: test\n" + setting + "\n")
                self.assertIn(reason, self.assert_refused("cortex-kb-company").stderr)
        config.write_text("project_name: test\nremote_operations: false\nauto_commit: false\non_status_change: ''\n")
        self.assertEqual(self.start("cortex-kb-company").returncode, 0)

    def test_repository_without_its_own_backlog_config_is_refused(self):
        # The server would walk up and serve the parent's project instead.
        self.make_repo("cortex", self.home)
        (self.home / "cortex/cortex-kb-company").mkdir()
        self.assertIn("no backlog/config.yml", self.assert_refused("cortex-kb-company").stderr)
        (self.home / "cortex/cortex-kb-company/backlog").mkdir()
        self.assert_refused("cortex-kb-company")

    def test_root_config_that_can_move_the_backlog_folder_is_refused(self):
        repo = self.make_repo("cortex-kb-company")
        (repo / "backlog.config.yml").write_text("backlog_directory: ../cortex-kb-typo3-dkm/backlog\n")
        self.assertIn("backlog.config.yml", self.assert_refused("cortex-kb-company").stderr)

    def test_missing_server_fails_loudly(self):
        self.make_repo("cortex-kb-company")
        (self.home / ".local/share/bootstrap/npm/node_modules" / BINARY).unlink()
        self.assertIn("Missing", self.assert_refused("cortex-kb-company").stderr)

    def test_launcher_opens_no_port_and_names_no_private_repository(self):
        code = launcher_code()
        for forbidden in ("sudo", "--port", "listen", "http", "wsl.exe", "@latest", "npx", *PRIVATE):
            self.assertNotIn(forbidden, code)


@unittest.skipUnless((REAL_MODULES / BINARY).is_file(), "Backlog.md not installed")
class RealServerTests(unittest.TestCase):
    """The pinned server over stdio through the launcher, in a throwaway home and project."""

    def test_probe_lists_the_page_tools_and_task_list_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".local/share/bootstrap/npm").mkdir(parents=True)
            (home / ".local/share/bootstrap/npm/node_modules").symlink_to(REAL_MODULES)
            repo = home / "cortex/cortex-kb-company"
            repo.mkdir(parents=True)
            env = launcher_env(home, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
            git = ["git", "-c", "user.name=Probe", "-c", "user.email=probe@example.invalid", "-C", str(repo)]
            backlog = [str(REAL_MODULES / BINARY)]

            def run(args):
                subprocess.run(args, cwd=repo, env=env, check=True, capture_output=True, timeout=60)
            run(git + ["init", "-q"])
            run(backlog + ["init", "Probe", "--defaults", "--integration-mode", "none"])
            for key in ("autoCommit", "remoteOperations", "checkActiveBranches"):
                run(backlog + ["config", "set", key, "false"])
            run(backlog + ["task", "create", "Probe task", "--plain"])
            run(git + ["add", "-A"])
            run(git + ["commit", "-q", "-m", "probe project"])
            result = subprocess.run([sys.executable, str(PROBE), str(LAUNCHER), "cortex-kb-company"],
                                    env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS: ", result.stdout)
            self.assertIn("task_list answered", result.stdout)
            status = subprocess.run(git + ["status", "--porcelain", "--untracked-files=all"], env=env,
                                    capture_output=True, text=True, check=True).stdout
            self.assertEqual(status, "")


class AppBlockTests(unittest.TestCase):
    def test_launcher_allowlist_is_the_request_list(self):
        self.assertEqual(launcher_allowlist(), ALLOWED)

    def test_generated_file_is_the_allowlist_block(self):
        self.assertEqual(APP_JSON.read_text(), app_block(launcher_allowlist()))
        servers = json.loads(APP_JSON.read_text())["mcpServers"]
        self.assertEqual(len(servers), len(ALLOWED))
        for name in PRIVATE:
            self.assertNotIn(name.removeprefix("cortex-kb-"), APP_JSON.read_text())

    def test_readme_carries_the_same_block_tools_and_pin(self):
        readme = (ROOT / "README.md").read_text()
        section = readme.split("### Backlog.md MCP for the Claude app", 1)[1].split("\n### ", 1)[0]
        blocks = re.findall(r"```json\n(.*?)```", section, re.S)
        self.assertEqual(blocks, [app_block(launcher_allowlist())])
        page_policy = section.split("**What the page may call:**", 1)[1]
        manifest = page_policy.split("Backlog.md 1.53.0", 1)[0]
        self.assertEqual(re.findall(r"`([^`]+)`", manifest), PAGE_TOOLS)
        self.assertIn("Write tools stay out of the page manifest unless Jacopo later approves a server-side guard.",
                      " ".join(manifest.split()))
        self.assertIn("any other Claude app chat with these servers connected sees every Backlog.md tool",
                      " ".join(page_policy.split()))
        for text in ("1.53.0", "archive", "delete", "git history", "claude-app-mcp.json"):
            self.assertIn(text, section)

    def test_lint_runs_this_test_and_shellchecks_the_launcher(self):
        lint = (ROOT / "ci/lint.sh").read_text()
        self.assertIn("python3 ci/backlog-mcp-test.py", lint)
        self.assertIn("home/dot_local/bin/executable_agentdev-backlog-mcp", lint)


class AgentSessionTests(unittest.TestCase):
    def test_no_tracked_agent_session_config_references_the_launcher(self):
        spec = importlib.util.spec_from_file_location("shell_connector_test", ROOT / "ci/shell-connector-test.py")
        shell = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shell)
        self.assertIn(ROOT / "home/dot_claude/settings.json", shell.AGENT_SESSION_CONFIGS)
        for path in shell.AGENT_SESSION_CONFIGS:
            text = path.read_text(errors="replace").lower()
            for marker in drift.BACKLOG_MCP_MARKERS:
                self.assertNotIn(marker, text, path.relative_to(ROOT))

    def test_drift_check_reports_a_live_registration(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".claude.json").write_text(json.dumps({"mcpServers": {"qmd": {"command": "qmd", "args": ["mcp"]}}}))
            self.assertFalse(drift.backlog_mcp_references(home))
            entry = json.loads(app_block(["cortex-kb-company"]))["mcpServers"]
            (home / ".claude.json").write_text(json.dumps({"projects": {"/x": {"mcpServers": entry}}}))
            self.assertTrue(drift.backlog_mcp_references(home))
            (home / ".claude.json").unlink()
            (home / ".codex").mkdir()
            (home / ".codex/config.toml").write_text('[mcp_servers.kb]\ncommand = "backlog"\nargs = ["mcp", "start"]\n')
            self.assertTrue(drift.backlog_mcp_references(home))


if __name__ == "__main__":
    unittest.main()
