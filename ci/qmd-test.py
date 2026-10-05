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

    def test_tools_script_links_qmd_and_installs_pinned_cuda_libraries(self):
        tools = (ROOT / "home/.chezmoitemplates/tools.sh").read_text()
        self.assertIn('exec "$HOME/.local/share/bootstrap/npm/node_modules/.bin/qmd" "$@"', tools)
        self.assertIn('linux-x64-cuda/bins/linux-x64-cuda', tools)
        self.assertIn('/usr/lib/wsl/lib', tools)
        pins = (ROOT / "home/.chezmoitemplates/pins.env").read_text()
        for key in ("CUDA_RUNTIME", "CUBLAS"):
            self.assertIn(f"{key}_URL='https://files.pythonhosted.org/", pins)
            self.assertRegex(pins, rf"{key}_SHA256='[0-9a-f]{{64}}'")
            self.assertIn(f"${key}_URL", tools)
        for library in ("libcudart.so.13", "libcublas.so.13", "libcublasLt.so.13"):
            self.assertIn(library, tools)

    def test_setup_script_does_every_step_and_hides_no_error(self):
        setup = (ROOT / "home/dot_local/share/bootstrap/executable_qmd-setup.sh").read_text()
        order = ["apply \"$HOME/.config/qmd/index.yml\"", "qmd pull", "qmd update", "qmd embed",
                 "enable --now qmd-index.timer", "mcp add --scope user qmd"]
        positions = [setup.index(step) for step in order]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("|| true\n", setup.replace("grep -E '^(CUDA|Vulkan):' || true\n", ""))
        self.assertNotIn("2>/dev/null", setup)
        install = (ROOT / "install.sh").read_text()
        self.assertIn("system/user-systemd.sh", install)
        self.assertIn("qmd-setup.sh", install)
        self.assertLess(install.index("chezmoi --source"), install.index("qmd-setup.sh"))
        self.assertIn("DBUS_SESSION_BUS_ADDRESS", install)


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


class RefreshTests(unittest.TestCase):
    """qmd-refresh fast-forwards only clean main checkouts that have an origin."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.temp = Path(self._temp.name)
        self.cortex = self.temp / "cortex"
        self.cortex.mkdir()
        bin_dir = self.temp / "bin"
        bin_dir.mkdir()
        # Record qmd calls instead of indexing.
        self.calls = self.temp / "qmd-calls"
        stub = bin_dir / "qmd"
        stub.write_text(f'#!/bin/sh\necho "$1" >> {self.calls}\n[ "$1" != embed ] || exit "${{QMD_STUB_EMBED_EXIT:-0}}"\n')
        stub.chmod(0o755)
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(self.temp),
            "QMD_CORTEX_DIR": str(self.cortex), "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        # The script prepends ~/.local/bin; point HOME's one at the stub too.
        (self.temp / ".local/bin").mkdir(parents=True)
        (self.temp / ".local/bin/qmd").symlink_to(stub)

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def make_origin(self, name):
        origin = self.temp / "origin" / name
        origin.mkdir(parents=True)
        self.git(origin, "init", "-q", "-b", "main")
        (origin / "a.md").write_text("one\n")
        self.git(origin, "add", "-A")
        self.git(origin, "commit", "-q", "-m", "one")
        clone = self.cortex / name
        self.git(self.temp, "clone", "-q", str(origin), str(clone))
        return origin, clone

    def advance(self, origin):
        (origin / "b.md").write_text("two\n")
        self.git(origin, "add", "-A")
        self.git(origin, "commit", "-q", "-m", "two")
        return self.git(origin, "rev-parse", "HEAD")

    def refresh(self):
        return subprocess.run(["bash", str(ROOT / "home/dot_local/bin/executable_qmd-refresh")],
                              env=self.env, capture_output=True, text=True)

    def test_fast_forwards_clean_main_and_skips_everything_else(self):
        origin, clean = self.make_origin("clean")
        head = self.advance(origin)
        dirty_origin, dirty = self.make_origin("dirty")
        dirty_head = self.advance(dirty_origin)
        (dirty / "a.md").write_text("local edit\n")
        branch_origin, branch = self.make_origin("branch")
        branch_head = self.advance(branch_origin)
        self.git(branch, "switch", "-q", "-c", "topic")
        diverged_origin, diverged = self.make_origin("diverged")
        (diverged / "c.md").write_text("local\n")
        self.git(diverged, "add", "-A")
        self.git(diverged, "commit", "-q", "-m", "local commit")
        diverged_head = self.git(diverged, "rev-parse", "HEAD")
        self.advance(diverged_origin)
        (self.cortex / "no-origin").mkdir()
        self.git(self.cortex / "no-origin", "init", "-q", "-b", "main")
        (self.cortex / "plain-dir").mkdir()
        (self.cortex / ".worktrees").mkdir()
        (self.cortex / "stray.md").write_text("x")

        result = self.refresh()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(clean, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(dirty, "rev-parse", "HEAD"), self.git(dirty, "rev-parse", "main"))
        self.assertNotEqual(self.git(dirty, "rev-parse", "HEAD"), dirty_head)
        self.assertEqual((dirty / "a.md").read_text(), "local edit\n")
        self.assertNotEqual(self.git(branch, "rev-parse", "main"), branch_head)
        self.assertEqual(self.git(diverged, "rev-parse", "HEAD"), diverged_head)
        for line in ("ok clean: updated", "skip dirty: working tree not clean",
                     "skip branch: on topic, not main", "skip diverged: main cannot fast-forward",
                     "skip no-origin: no origin", "skip plain-dir: not a git checkout"):
            self.assertIn(line, result.stdout)
        self.assertNotIn(".worktrees", result.stdout)
        self.assertEqual(self.calls.read_text().split(), ["update", "embed"])

    def test_fetch_failure_is_reported_but_indexing_still_runs(self):
        _, repo = self.make_origin("gone")
        self.git(repo, "remote", "set-url", "origin", str(self.temp / "does-not-exist"))
        result = self.refresh()
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR gone: git fetch failed", result.stderr)
        self.assertEqual(self.calls.read_text().split(), ["update", "embed"])

    def test_indexing_failure_fails_the_unit(self):
        self.env["QMD_STUB_EMBED_EXIT"] = "3"
        self.assertEqual(self.refresh().returncode, 1)


class UnitTests(unittest.TestCase):
    def test_service_only_indexes(self):
        service = units("qmd-index.service")
        self.assertEqual(service["Service"]["Type"], "oneshot")
        # configparser keeps only the last duplicate key, so read the raw lines.
        lines = (UNITS / "qmd-index.service").read_text().splitlines()
        exec_lines = [line.split("=", 1)[1] for line in lines if line.startswith("ExecStart")]
        self.assertEqual(exec_lines, ["%h/.local/bin/qmd-refresh"])
        self.assertFalse([line for line in lines if line.startswith("ExecStartPre")])

    def test_timer_every_fifteen_minutes_and_enabled(self):
        timer = units("qmd-index.timer")
        self.assertEqual(timer["Timer"]["OnCalendar"], "*:0/15")
        self.assertEqual(timer["Install"]["WantedBy"], "timers.target")
        link = UNITS / "timers.target.wants/symlink_qmd-index.timer"
        self.assertEqual(link.read_text().strip(), "../qmd-index.timer")

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not available")
    def test_systemd_accepts_the_timer_calendar(self):
        result = subprocess.run(["systemd-analyze", "calendar", "--iterations=4", "*:0/15"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for minute in (":15:00", ":30:00", ":45:00"):
            self.assertIn(minute, result.stdout)


if __name__ == "__main__":
    unittest.main()
