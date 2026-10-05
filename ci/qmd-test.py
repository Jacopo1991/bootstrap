#!/usr/bin/env python3
"""Check the qmd pin, the rendered index.yml, the user units and the bootstrap wiring.

Usage: qmd-test.py [path-to-chezmoi]
"""
import configparser
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
NPM = ROOT / "home/dot_local/share/bootstrap/npm"
UNITS = ROOT / "home/dot_config/systemd/user"
CHEZMOI = sys.argv.pop(1) if len(sys.argv) > 1 else shutil.which("chezmoi")


def units(name):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(UNITS / name, encoding="utf-8")
    return parser


class PinTests(unittest.TestCase):
    def setUp(self):
        self.package = json.loads((NPM / "package.json").read_text())
        self.lock = json.loads((NPM / "package-lock.json").read_text())

    def test_exact_pin_and_existing_pins_kept(self):
        deps = self.package["dependencies"]
        self.assertEqual(deps["@tobilu/qmd"], "2.8.3")
        self.assertEqual(deps["@openai/codex"], "0.159.2")
        self.assertEqual(deps["ccusage"], "20.0.26")

    def test_lockfile_v2_resolves_qmd_with_integrity(self):
        self.assertEqual(self.lock["lockfileVersion"], 2)
        self.assertEqual(self.lock["packages"][""]["dependencies"], self.package["dependencies"])
        qmd = self.lock["packages"]["node_modules/@tobilu/qmd"]
        self.assertEqual(qmd["version"], "2.8.3")
        self.assertEqual(qmd["license"], "MIT")
        self.assertEqual(self.lock["dependencies"]["@tobilu/qmd"]["version"], "2.8.3")

    def test_every_locked_package_is_pinned_to_the_registry(self):
        for path, entry in self.lock["packages"].items():
            if not path:
                continue
            self.assertTrue(entry["resolved"].startswith("https://registry.npmjs.org/"), path)
            self.assertTrue(entry["integrity"].startswith("sha512-"), path)

    def test_tools_script_links_qmd_and_registers_mcp(self):
        tools = (ROOT / "home/.chezmoitemplates/tools.sh").read_text()
        self.assertIn('ln -sfn "$npm_root/node_modules/.bin/qmd" "$HOME/.local/bin/qmd"', tools)
        self.assertIn('mcp add --scope user qmd -- "$HOME/.local/bin/qmd" mcp', tools)
        for step in ("qmd\" pull", "qmd\" update", "qmd\" embed"):
            self.assertIn(step, tools)


@unittest.skipUnless(CHEZMOI, "chezmoi not available")
class RenderedConfigTests(unittest.TestCase):
    def render(self, repos):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            for name, intent in repos.items():
                (home / "cortex" / name).mkdir(parents=True)
                if intent is not None:
                    (home / "cortex" / name / "INTENT.md").write_text(intent)
            (home / "cortex" / "stray-file.md").write_text("not a repository")
            (home / "cortex" / ".worktrees").mkdir()
            (home / "dest").mkdir()
            env = {**os.environ, "HOME": str(home)}
            template = (ROOT / "home/dot_config/qmd/index.yml.tmpl").read_text()
            def chezmoi(*args, **kwargs):
                result = subprocess.run(
                    [CHEZMOI, "--source", str(ROOT), "--destination", str(home / "dest"),
                     "--config", str(home / "chezmoi.toml"), "execute-template", *args],
                    capture_output=True, text=True, env=env, **kwargs)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout

            index = home / "index.yml"
            index.write_text(chezmoi(input=template))
            parsed = chezmoi(f'{{{{ output "cat" "{index}" | fromYaml | toJson }}}}')
            return str(home), json.loads(parsed)

    def test_one_collection_per_cortex_repository(self):
        home, config = self.render({
            "cortex-core": '---\ntitle: "Core rules and schemas"\n---\n# x\n',
            "cortex-kb-demo": "---\nid: X\ntitle: Demo shared-runtime intent\n---\n",
            "cortex-kb-bare": None,
        })
        self.assertEqual(set(config["collections"]), {"cortex-core", "cortex-kb-demo", "cortex-kb-bare"})
        for name, collection in config["collections"].items():
            self.assertEqual(collection["path"], f"{home}/cortex/{name}")
            self.assertEqual(collection["pattern"], "**/*.md")
            context = collection["context"]["/"]
            self.assertTrue(context and "\n" not in context)
        self.assertEqual(config["collections"]["cortex-core"]["context"]["/"],
                         "cortex-core: Core rules and schemas")
        self.assertEqual(config["collections"]["cortex-kb-demo"]["context"]["/"],
                         "cortex-kb-demo: Demo shared-runtime intent")
        self.assertEqual(config["collections"]["cortex-kb-bare"]["context"]["/"],
                         "Cortex knowledge repository cortex-kb-bare")

    def test_hostile_title_stays_a_single_quoted_string(self):
        _, config = self.render({"cortex-kb-x": '---\ntitle: a "b": c # d\n---\n'})
        self.assertEqual(set(config["collections"]), {"cortex-kb-x"})
        self.assertIn("# d", config["collections"]["cortex-kb-x"]["context"]["/"])


class UnitTests(unittest.TestCase):
    def test_service_only_indexes(self):
        service = units("qmd-index.service")
        self.assertEqual(service["Service"]["Type"], "oneshot")
        # configparser keeps only the last duplicate key, so read the raw lines.
        lines = (UNITS / "qmd-index.service").read_text().splitlines()
        exec_lines = [line.split("=", 1)[1] for line in lines if line.startswith("ExecStart")]
        self.assertEqual(exec_lines, ["%h/.local/bin/qmd update", "%h/.local/bin/qmd embed"])
        self.assertFalse([line for line in lines if line.startswith("ExecStartPre")])

    def test_timer_every_fifteen_minutes_and_enabled(self):
        timer = units("qmd-index.timer")
        self.assertEqual(timer["Timer"]["OnCalendar"], "*:0/15")
        self.assertEqual(timer["Install"]["WantedBy"], "timers.target")
        link = UNITS / "timers.target.wants/symlink_qmd-index.timer"
        self.assertEqual(link.read_text().strip(), "../qmd-index.timer")

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not available")
    def test_systemd_accepts_the_timer_calendar(self):
        result = subprocess.run(["systemd-analyze", "calendar", "--iterations=3", "*:0/15"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for minute in (":15:00", ":30:00", ":45:00"):
            self.assertIn(minute, result.stdout)


if __name__ == "__main__":
    unittest.main()
