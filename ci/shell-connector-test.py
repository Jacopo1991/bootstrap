#!/usr/bin/env python3
"""Check the AgentDev shell connector: the Desktop Commander pin, the vendor-host block, the
launcher's forced config, MCP over stdio, and that no agent-session config references it."""
import getpass
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
NPM = ROOT / "home/dot_local/share/bootstrap/npm"
LAUNCHER = ROOT / "home/dot_local/bin/executable_agentdev-shell-mcp"
PROBE = ROOT / "ci/shell-connector-probe.py"
HOSTS_SCRIPT = ROOT / "system/vendor-block-hosts.sh"
NAME = "@wonderwhy-er/desktop-commander"
INTEGRITY = "sha512-VNeKfaBR6TN/8MlP/ziTpjNEeHrGOFmmzQjrwzr52kQalBJoNNISWwaSQElPFc6+I17NOOM354KbSdS/aDrqGA=="
VENDOR_HOSTS = [
    "desktopcommander.app",
    "telemetry.desktopcommander.app",
    "mcp.desktopcommander.app",
    "dc-telemetry-proxy-83847352264.europe-west1.run.app",
]
ALLOWED = ["/home/agent/dev_workspace", "/home/agent/cortex", "/tmp/claude-1001/scratch"]
REAL_MODULES = Path.home() / ".local/share/bootstrap/npm/node_modules"
REAL_PACKAGE = REAL_MODULES / NAME

spec = importlib.util.spec_from_file_location("agent_drift", ROOT / "checks/agent-drift.py")
drift = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drift)

# A stand-in for the pinned server: the server's defaults, and a tiny MCP stdio server that
# records how the launcher started it and runs start_process commands through bash.
STUB_CONFIG_MANAGER = """export const configManager = { getDefaultConfig() { return {
  blockedCommands: ['sudo'], defaultShell: '/bin/sh', allowedDirectories: [], telemetryEnabled: true,
  fileReadLineLimit: 1000, pendingWelcomeOnboarding: true, welcomeOnboardingEligible: true }; } };
"""
STUB_SERVER = """import { createInterface } from 'node:readline';
import { execFileSync } from 'node:child_process';
import { writeFileSync } from 'node:fs';
writeFileSync(process.env.HOME + '/stub-start.json', JSON.stringify({
  argv: process.argv.slice(2), cwd: process.cwd(), telemetry: process.env.DESKTOP_COMMANDER_DISABLE_TELEMETRY }));
const reply = (id, result) => process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, result }) + '\\n');
for await (const line of createInterface({ input: process.stdin })) {
  const { id, method, params } = JSON.parse(line);
  if (method === 'initialize') reply(id, { protocolVersion: params.protocolVersion, capabilities: { tools: {} },
    serverInfo: { name: 'stub-commander', version: '0' } });
  else if (method === 'tools/list') reply(id, { tools: ['start_process', 'read_file', 'list_directory']
    .map((name) => ({ name, inputSchema: { type: 'object' } })) });
  else if (method === 'tools/call') reply(id, { content: [{ type: 'text',
    text: execFileSync('/bin/bash', ['-c', params.arguments.command], { encoding: 'utf8' }) }] });
}
"""


NODE = subprocess.run(["node", "-p", "process.execPath"], capture_output=True, text=True,
                      check=True).stdout.strip()


def launcher_env(home):
    # The launcher keeps a fixed PATH under $HOME; give the throwaway home the same node.
    shims = home / ".local/share/mise/shims"
    if not (shims / "node").exists():
        shims.mkdir(parents=True, exist_ok=True)
        (shims / "node").symlink_to(NODE)
    return {"HOME": str(home), "PATH": os.environ["PATH"], "PROBE_USER": getpass.getuser()}


class PinTests(unittest.TestCase):
    def setUp(self):
        self.package = json.loads((NPM / "package.json").read_text())
        self.lock = json.loads((NPM / "package-lock.json").read_text())

    def test_exact_pin_and_existing_pins_kept(self):
        deps = self.package["dependencies"]
        self.assertEqual(deps[NAME], "0.2.52")
        self.assertEqual(deps["@tobilu/qmd"], "2.8.3")
        self.assertEqual(deps["@openai/codex"], "0.159.2")
        self.assertEqual(deps["backlog.md"], "1.53.0")
        self.assertEqual(self.lock["packages"][""]["dependencies"], deps)

    def test_lock_entry_has_the_reviewed_integrity_and_licence(self):
        entry = self.lock["packages"][f"node_modules/{NAME}"]
        self.assertEqual(entry["version"], "0.2.52")
        self.assertEqual(entry["license"], "MIT")
        self.assertEqual(entry["integrity"], INTEGRITY)
        self.assertEqual(entry["resolved"], "https://registry.npmjs.org/@wonderwhy-er/desktop-commander/-/desktop-commander-0.2.52.tgz")

    def test_never_latest(self):
        for path in (LAUNCHER, ROOT / "README.md", ROOT / "install.sh"):
            self.assertNotRegex(path.read_text(), r"desktop-commander@latest|npx\s+-y\s+@wonderwhy-er")

    @unittest.skipUnless(REAL_PACKAGE.is_dir(), "Desktop Commander not installed")
    def test_installed_package_is_the_pin(self):
        self.assertEqual(json.loads((REAL_PACKAGE / "package.json").read_text())["version"], "0.2.52")


class HostsTests(unittest.TestCase):
    def run_script(self, path):
        subprocess.run(["bash", str(HOSTS_SCRIPT), str(path)], check=True)

    def test_block_is_added_once_and_other_lines_are_kept(self):
        with tempfile.TemporaryDirectory() as temp:
            hosts = Path(temp) / "hosts"
            original = "127.0.0.1 localhost\n10.0.0.1 agentdev\n"
            hosts.write_text(original)
            self.run_script(hosts)
            first = hosts.read_text()
            self.run_script(hosts)
            self.assertEqual(hosts.read_text(), first)
            self.assertTrue(first.startswith(original))
            for host in VENDOR_HOSTS:
                self.assertEqual(first.count(f"0.0.0.0 {host}\n"), 1)
                self.assertEqual(first.count(f":: {host}\n"), 1)
            self.assertEqual(first.count("# BEGIN bootstrap vendor-block"), 1)

    def test_stale_block_is_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            hosts = Path(temp) / "hosts"
            hosts.write_text("127.0.0.1 localhost\n# BEGIN bootstrap vendor-block\n0.0.0.0 old.example\n"
                             "# END bootstrap vendor-block\n10.0.0.1 agentdev\n")
            self.run_script(hosts)
            text = hosts.read_text()
            self.assertNotIn("old.example", text)
            self.assertIn("10.0.0.1 agentdev\n", text)
            self.assertIn("0.0.0.0 desktopcommander.app\n", text)

    def test_symlinked_hosts_file_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            real = Path(temp) / "real"
            real.write_text("127.0.0.1 localhost\n")
            link = Path(temp) / "hosts"
            link.symlink_to(real)
            result = subprocess.run(["bash", str(HOSTS_SCRIPT), str(link)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(real.read_text(), "127.0.0.1 localhost\n")

    def test_base_sh_blocks_the_hosts_and_keeps_wsl_from_regenerating_them(self):
        base = (ROOT / "system/base.sh").read_text()
        self.assertIn('bash "$BOOTSTRAP_ROOT/system/vendor-block-hosts.sh" /etc/hosts', base)
        self.assertIn("('network','generateHosts','false')", base)
        install = (ROOT / "install.sh").read_text()
        self.assertLess(install.index("system/base.sh"), install.index("chezmoi"))
        self.assertNotIn("vendor-block.sh", install)
        self.assertFalse((ROOT / "system/vendor-block.service").exists())
        # The fresh-distro run checks the real /etc/hosts after base.sh.
        self.assertIn("generateHosts", (ROOT / "ci/wsl-config.py").read_text())
        for host in VENDOR_HOSTS:
            self.assertIn(host, HOSTS_SCRIPT.read_text())
            self.assertIn(host, (ROOT / "ci/wsl-config.py").read_text())

    @unittest.skipUnless(REAL_PACKAGE.is_dir(), "Desktop Commander not installed")
    def test_every_vendor_host_in_the_installed_package_is_blocked(self):
        found = set()
        for path in (REAL_PACKAGE / "dist").rglob("*.js"):
            found.update(re.findall(r"https?://([a-z0-9.-]*(?:desktopcommander\.app|\.run\.app))", path.read_text(errors="replace")))
        self.assertTrue(found)
        self.assertLessEqual(found, set(VENDOR_HOSTS))


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        package = self.home / ".local/share/bootstrap/npm/node_modules" / NAME
        (package / "dist").mkdir(parents=True)
        (package / "package.json").write_text('{"type": "module"}\n')
        (package / "dist/config-manager.js").write_text(STUB_CONFIG_MANAGER)
        (package / "dist/index.js").write_text(STUB_SERVER)
        self.config = self.home / ".claude-server-commander/config.json"

    def tearDown(self):
        self.temp.cleanup()

    def start(self, check=True):
        result = subprocess.run(["bash", str(LAUNCHER)], env=launcher_env(self.home), cwd="/",
                                stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_first_start_takes_the_server_defaults_and_forces_three_keys(self):
        result = self.start()
        self.assertEqual(result.stdout, "")  # stdout belongs to MCP
        config = json.loads(self.config.read_text())
        self.assertIs(config["telemetryEnabled"], False)
        self.assertEqual(config["defaultShell"], "/bin/bash")
        self.assertEqual(config["allowedDirectories"], ALLOWED)
        self.assertEqual(config["blockedCommands"], ["sudo"])
        self.assertEqual(config["fileReadLineLimit"], 1000)
        self.assertIs(config["pendingWelcomeOnboarding"], False)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        started = json.loads((self.home / "stub-start.json").read_text())
        self.assertEqual(started, {"argv": ["--no-onboarding"], "cwd": str(self.home), "telemetry": "1"})

    def test_second_start_turns_telemetry_off_again_and_keeps_other_keys(self):
        self.start()
        config = json.loads(self.config.read_text())
        config.update(telemetryEnabled=True, defaultShell="/bin/sh", allowedDirectories=["/"],
                      fileReadLineLimit=42, clientId="kept")
        self.config.write_text(json.dumps(config))
        self.start()
        config = json.loads(self.config.read_text())
        self.assertIs(config["telemetryEnabled"], False)
        self.assertEqual(config["defaultShell"], "/bin/bash")
        self.assertEqual(config["allowedDirectories"], ALLOWED)
        self.assertEqual(config["fileReadLineLimit"], 42)
        self.assertEqual(config["clientId"], "kept")
        self.assertEqual(config["blockedCommands"], ["sudo"])

    def test_unreadable_config_is_replaced(self):
        self.config.parent.mkdir(mode=0o700)
        self.config.write_text("{not json")
        result = self.start()
        self.assertIn("replacing unreadable", result.stderr)
        self.assertIs(json.loads(self.config.read_text())["telemetryEnabled"], False)

    def test_server_chrome_lookup_finds_the_pinned_playwright_chromium(self):
        chrome = self.home / ".cache/ms-playwright/chromium-1247/chrome-linux64/chrome"
        chrome.parent.mkdir(parents=True)
        chrome.write_text("#!/bin/sh\n")
        chrome.chmod(0o755)
        self.start()
        link = self.home / ".claude-server-commander/puppeteer-cache/chrome/playwright-chromium/chrome-linux64"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), chrome.parent.resolve())

    def test_missing_server_fails_loudly(self):
        (self.home / ".local/share/bootstrap/npm/node_modules" / NAME / "dist/index.js").unlink()
        result = self.start(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing", result.stderr)

    def test_probe_speaks_mcp_through_the_launcher(self):
        result = subprocess.run([sys.executable, str(PROBE), str(LAUNCHER)], env=launcher_env(self.home),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ran `echo ok`", result.stdout)

    def test_launcher_opens_no_port_and_uses_no_sudo(self):
        code = "\n".join(line for line in LAUNCHER.read_text().splitlines() if not line.lstrip().startswith("#"))
        for forbidden in ("sudo", "--port", "listen", "http", "wsl.exe", "@latest"):
            self.assertNotIn(forbidden, code)


@unittest.skipUnless(REAL_PACKAGE.is_dir(), "Desktop Commander not installed")
class RealServerTests(unittest.TestCase):
    """The pinned server itself, in a throwaway home (runs wherever chezmoi installed it)."""

    def test_real_server_over_stdio_twice(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".local/share/bootstrap/npm").mkdir(parents=True)
            (home / ".local/share/bootstrap/npm/node_modules").symlink_to(REAL_MODULES)
            playwright = Path.home() / ".cache/ms-playwright"
            if playwright.is_dir():
                (home / ".cache").mkdir()
                (home / ".cache/ms-playwright").symlink_to(playwright)
            probe = [sys.executable, str(PROBE), str(LAUNCHER)]
            result = subprocess.run(probe, env=launcher_env(home), capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            config_path = home / ".claude-server-commander/config.json"
            config = json.loads(config_path.read_text())
            self.assertIn("sudo", config["blockedCommands"])
            config.update(telemetryEnabled=True, fileWriteLineLimit=77)
            config_path.write_text(json.dumps(config))
            result = subprocess.run(probe, env=launcher_env(home), capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            config = json.loads(config_path.read_text())
            self.assertIs(config["telemetryEnabled"], False)
            self.assertEqual(config["fileWriteLineLimit"], 77)
            self.assertEqual(config["allowedDirectories"], ALLOWED)


def agent_session_references(path):
    """Connector markers in an agent-session config, ignoring plugin switches that turn it off."""
    text = path.read_text(errors="replace")
    if path.suffix == ".json":
        value = json.loads(text)
        plugins = value.get("enabledPlugins") if isinstance(value, dict) else None
        if isinstance(plugins, dict):
            value["enabledPlugins"] = {key: on for key, on in plugins.items() if on is not False}
        text = json.dumps(value)
    return [marker for marker in drift.SHELL_CONNECTOR_MARKERS if marker in text.lower()]


AGENT_SESSION_CONFIGS = sorted(
    [path for base in ("home/dot_claude", "home/dot_codex", "home/dot_config")
     for path in (ROOT / base).rglob("*") if path.is_file()]
    + list((ROOT / "home/dot_local/share/bootstrap").glob("executable_*-setup.sh"))
    + [ROOT / "system/claude-managed.sh", ROOT / "system/claude-managed-mods.json", ROOT / "install.sh"])


class AgentSessionTests(unittest.TestCase):
    def test_no_agent_session_config_references_the_connector(self):
        self.assertIn(ROOT / "home/dot_claude/settings.json", AGENT_SESSION_CONFIGS)
        self.assertIn(ROOT / "home/dot_codex/config.toml", AGENT_SESSION_CONFIGS)
        for path in AGENT_SESSION_CONFIGS:
            self.assertEqual(agent_session_references(path), [], path.relative_to(ROOT))

    def test_the_scan_catches_a_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Path(temp) / "settings.json"
            settings.write_text(json.dumps({"enabledPlugins": {"desktop-commander@synced": False}}))
            self.assertEqual(agent_session_references(settings), [])
            settings.write_text(json.dumps({"enabledPlugins": {"desktop-commander@synced": True}}))
            self.assertNotEqual(agent_session_references(settings), [])
            settings.write_text(json.dumps({"mcpServers": {"shell": {"command": "/home/agent/.local/bin/agentdev-shell-mcp"}}}))
            self.assertNotEqual(agent_session_references(settings), [])

    def test_drift_check_reports_a_live_registration(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".claude.json").write_text(json.dumps({"mcpServers": {"context7": {"type": "http", "url": "https://mcp.context7.com/mcp"}}}))
            self.assertFalse(drift.shell_connector_references(home))
            (home / ".claude.json").write_text(json.dumps({"projects": {"/x": {"mcpServers": {
                "shell": {"command": "wsl.exe", "args": ["--", "/home/agent/.local/bin/agentdev-shell-mcp"]}}}}}))
            self.assertTrue(drift.shell_connector_references(home))
            (home / ".claude.json").unlink()
            (home / ".codex").mkdir()
            (home / ".codex/config.toml").write_text('[mcp_servers.dc]\ncommand = "npx"\nargs = ["@wonderwhy-er/desktop-commander"]\n')
            self.assertTrue(drift.shell_connector_references(home))


class ReadmeTests(unittest.TestCase):
    def test_exact_claude_app_entry(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn('"command": "wsl.exe"', readme)
        self.assertIn('"args": ["-d", "AgentDev", "-u", "agent", "--", "/home/agent/.local/bin/agentdev-shell-mcp"]', readme)
        self.assertIn("wsl.exe -d AgentDev -u agent -- /home/agent/.local/bin/agentdev-shell-mcp", readme)
        for host in VENDOR_HOSTS:
            self.assertIn(host, readme)

    def test_lint_and_distro_run_this_test_and_the_probe(self):
        lint = (ROOT / "ci/lint.sh").read_text()
        self.assertIn("python3 ci/shell-connector-test.py", lint)
        self.assertIn("home/dot_local/bin/executable_agentdev-shell-mcp", lint)
        distro = (ROOT / "ci/distro.sh").read_text()
        self.assertIn("sudo -H -u agent python3 /repo/ci/shell-connector-probe.py", distro)


if __name__ == "__main__":
    unittest.main()
