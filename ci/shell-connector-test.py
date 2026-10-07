#!/usr/bin/env python3
"""Check the Desktop Commander pin, the vendor-host block, the launcher and the README entry."""
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
NPM = ROOT / "home/dot_local/share/bootstrap/npm"
LAUNCHER = ROOT / "home/dot_local/bin/executable_agentdev-shell-mcp"
HOSTS_SCRIPT = ROOT / "system/vendor-block-hosts.sh"
VENDOR_HOSTS = [
    "desktopcommander.app",
    "telemetry.desktopcommander.app",
    "mcp.desktopcommander.app",
    "dc-telemetry-proxy-83847352264.europe-west1.run.app",
]
NAME = "@wonderwhy-er/desktop-commander"


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

    def test_lock_entry_has_integrity_licence_and_binary(self):
        entry = self.lock["packages"][f"node_modules/{NAME}"]
        self.assertEqual(entry["version"], "0.2.52")
        self.assertEqual(entry["license"], "MIT")
        self.assertTrue(entry["integrity"].startswith("sha512-"))
        self.assertEqual(entry["resolved"], "https://registry.npmjs.org/@wonderwhy-er/desktop-commander/-/desktop-commander-0.2.52.tgz")
        self.assertEqual(entry["bin"]["desktop-commander"], "dist/index.js")


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
                self.assertIn(f"0.0.0.0 {host}\n", first)
                self.assertEqual(first.count(f"0.0.0.0 {host}\n"), 1)
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

    def test_every_vendor_host_in_the_source_is_blocked_and_wiring_runs_before_npm_ci(self):
        script = HOSTS_SCRIPT.read_text()
        for host in VENDOR_HOSTS:
            self.assertIn(host, script)
        install = (ROOT / "install.sh").read_text()
        self.assertLess(install.index("system/vendor-block.sh"), install.index("chezmoi --source"))
        installer = (ROOT / "system/vendor-block.sh").read_text()
        self.assertIn("vendor-block-hosts.sh\" /etc/hosts", installer)
        self.assertIn("systemctl enable vendor-block.service", installer)
        unit = (ROOT / "system/vendor-block.service").read_text()
        self.assertIn("User=root", unit)
        self.assertIn("ExecStart=/usr/bin/bash /opt/machine-bootstrap/current/system/vendor-block-hosts.sh", unit)
        self.assertIn("WantedBy=multi-user.target", unit)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        server_dir = self.home / ".local/share/bootstrap/npm/node_modules/.bin"
        server_dir.mkdir(parents=True)
        # A stand-in for the pinned server: report what the launcher handed it.
        server = server_dir / "desktop-commander"
        server.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$PWD" "${DESKTOP_COMMANDER_DISABLE_TELEMETRY:-}"\n')
        server.chmod(0o755)
        self.config = self.home / ".claude-server-commander/config.json"

    def tearDown(self):
        self.temp.cleanup()

    def start(self):
        env = {"HOME": str(self.home), "PATH": os.environ["PATH"]}
        return subprocess.run(["bash", str(LAUNCHER)], env=env, cwd="/", capture_output=True, text=True, check=True)

    def test_starts_server_in_home_with_telemetry_off(self):
        result = self.start()
        self.assertEqual(result.stdout.split("\n")[:2], [str(self.home), "1"])

    def test_seeds_config_and_reseeds_after_tampering(self):
        self.start()
        for _ in range(2):
            config = json.loads(self.config.read_text())
            self.assertIs(config["telemetryEnabled"], False)
            self.assertEqual(config["allowedDirectories"], [str(self.home)])
            self.assertEqual(config["defaultShell"], "/bin/bash")
            for command in ("sudo", "su", "wsl.exe", "powershell.exe", "cmd.exe"):
                self.assertIn(command, config["blockedCommands"])
            self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
            self.config.write_text(json.dumps({"telemetryEnabled": True, "allowedDirectories": [], "blockedCommands": []}))
            self.start()

    def test_missing_server_fails_loudly(self):
        (self.home / ".local/share/bootstrap/npm/node_modules/.bin/desktop-commander").unlink()
        env = {"HOME": str(self.home), "PATH": os.environ["PATH"]}
        result = subprocess.run(["bash", str(LAUNCHER)], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing", result.stderr)

    def test_launcher_opens_no_port_and_uses_no_sudo(self):
        code = "\n".join(line for line in LAUNCHER.read_text().splitlines()
                         if not line.lstrip().startswith("#") and "blockedCommands" not in line
                         and not line.lstrip().startswith('"sudo"'))
        for forbidden in ("sudo", "--port", "listen", "http"):
            self.assertNotIn(forbidden, code)


class ReadmeTests(unittest.TestCase):
    def test_exact_claude_app_entry_and_boundary_statement(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn('"command": "wsl.exe"', readme)
        self.assertIn('"args": ["-d", "AgentDev", "-u", "agent", "--", "/home/agent/.local/bin/agentdev-shell-mcp"]', readme)
        self.assertIn("advisory", readme)
        self.assertIn("desktopcommander.app", readme)

    def test_lint_runs_this_test_and_checks_the_launcher(self):
        lint = (ROOT / "ci/lint.sh").read_text()
        self.assertIn("python3 ci/shell-connector-test.py", lint)
        self.assertIn("home/dot_local/bin/executable_agentdev-shell-mcp", lint)


if __name__ == "__main__":
    unittest.main()
