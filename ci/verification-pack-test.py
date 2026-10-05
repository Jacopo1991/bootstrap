#!/usr/bin/env python3
"""Check the verification pack: Playwright pins, install wiring, verify-enable, and a smoke run.

The smoke run needs the pinned npm install and a Chromium that can start (installed by
verify-setup.sh); without them it is skipped.
"""
import glob
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
SHARE = ROOT / "home/dot_local/share/bootstrap"
NPM = SHARE / "npm"
TEMPLATES = SHARE / "verification"
ENABLE = ROOT / "home/dot_local/bin/executable_verify-enable"
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


class PinTests(unittest.TestCase):
    def setUp(self):
        self.package = json.loads((NPM / "package.json").read_text())
        self.lock = json.loads((NPM / "package-lock.json").read_text())
        self.packages = self.lock["packages"]

    def test_playwright_packages_share_one_exact_version(self):
        deps = self.package["dependencies"]
        version = deps["@playwright/test"]
        self.assertRegex(version, r"^\d+\.\d+\.\d+(-alpha-\d+)?$")
        self.assertEqual(deps["playwright-core"], version)
        self.assertIn("@axe-core/playwright", deps)
        self.assertRegex(deps["@axe-core/playwright"], r"^\d+\.\d+\.\d+$")
        self.assertRegex(deps["@playwright/mcp"], r"^\d+\.\d+\.\d+$")
        # The MCP server is built against exactly this Playwright version.
        mcp = self.packages["node_modules/@playwright/mcp"]
        self.assertEqual(mcp["version"], deps["@playwright/mcp"])
        self.assertEqual(mcp["dependencies"]["playwright"], version)
        self.assertEqual(mcp["dependencies"]["playwright-core"], version)
        self.assertEqual(self.packages["node_modules/@playwright/test"]["version"], version)
        self.assertEqual(self.packages["node_modules/@playwright/test"]["dependencies"]["playwright"], version)

    def test_lock_resolves_a_single_playwright_copy(self):
        for name in ("playwright", "playwright-core"):
            copies = {path: entry["version"] for path, entry in self.packages.items()
                      if path == f"node_modules/{name}" or path.endswith(f"/node_modules/{name}")}
            self.assertEqual(set(copies.values()), {self.package["dependencies"]["@playwright/test"]}, copies)
        self.assertEqual(len([p for p in self.packages if p.endswith("/playwright-core")]), 1)

    def test_axe_peer_follows_the_pin(self):
        # axe-core's open peer range would otherwise pull a second Playwright version.
        self.assertEqual(self.package["overrides"]["@axe-core/playwright"]["playwright-core"], "$playwright-core")

    def test_existing_pins_kept_and_lock_is_v2(self):
        deps = self.package["dependencies"]
        self.assertEqual(deps["@openai/codex"], "0.159.2")
        self.assertEqual(deps["@tobilu/qmd"], "2.8.3")
        self.assertEqual(deps["backlog.md"], "1.53.0")
        self.assertEqual(deps["ccusage"], "20.0.26")
        self.assertEqual(self.lock["lockfileVersion"], 2)
        self.assertEqual(self.packages[""]["dependencies"], deps)
        for path, entry in self.packages.items():
            if path:
                self.assertTrue(entry["resolved"].startswith("https://registry.npmjs.org/"), path)
                self.assertTrue(entry["integrity"].startswith("sha512-"), path)


class WiringTests(unittest.TestCase):
    def test_wrappers_use_the_one_shared_browser_cache(self):
        tools = (ROOT / "home/.chezmoitemplates/tools.sh").read_text()
        self.assertEqual(tools.count('PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright"'), 2)
        for name in ("playwright", "playwright-mcp"):
            self.assertIn(f'node_modules/.bin/{name}" ', tools)
        self.assertIn("--browser chromium --headless", tools)

    def test_setup_downloads_the_browser_then_registers_the_mcp_server(self):
        setup = (SHARE / "executable_verify-setup.sh").read_text()
        self.assertLess(setup.index("playwright install chromium"),
                        setup.index("mcp add --scope user playwright -- \"$HOME/.local/bin/playwright-mcp\""))
        self.assertNotIn("2>/dev/null", setup)
        self.assertNotIn("|| true", setup)
        install = (ROOT / "install.sh").read_text()
        self.assertLess(install.index("chezmoi --source"), install.index("verify-setup.sh"))
        self.assertLess(install.index("qmd-setup.sh"), install.index("verify-setup.sh"))

    def test_nothing_downloads_a_browser_at_test_time(self):
        for path in [*TEMPLATES.iterdir(), ENABLE]:
            self.assertNotIn("playwright install", path.read_text(), path.name)
            self.assertNotIn("npx", path.read_text(), path.name)

    def test_config_has_both_viewports_failure_evidence_and_no_html_report(self):
        config = (TEMPLATES / "playwright.config.ts").read_text()
        for expected in ("Desktop Chrome", "Pixel 7", "trace: 'retain-on-failure'",
                         "screenshot: 'only-on-failure'", "VERIFY_OUT", "VERIFY_URL"):
            self.assertIn(expected, config)
        self.assertNotIn("'html'", config)

    def test_skill_carries_the_method(self):
        skill = (ROOT / "skills-proposed/verify-ui/SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: verify-ui\n"))
        for expected in ("Independence", "Role A", "Role B", "verdict.json", "attestation", "read_source",
                         "~/project-data/<project>/verification/<run>/", "underspecified", "just verify"):
            self.assertIn(expected, skill)
        # The documented verdict example is valid JSON.
        block = skill.split("```json\n", 1)[1].split("\n```", 1)[0]
        verdict = json.loads(block)
        self.assertFalse(verdict["attestation"]["read_source"])
        self.assertEqual(verdict["overall"], "fail")


class TempTree:
    """A copy of the installed layout: bin/verify-enable beside share/bootstrap/{verification,npm}."""

    def __init__(self, test, modules=None):
        temp = tempfile.TemporaryDirectory()
        test.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        shutil.copy(ENABLE, self.bin / "verify-enable")
        share = self.root / "share/bootstrap"
        shutil.copytree(TEMPLATES, share / "verification")
        self.modules = share / "npm/node_modules"
        if modules:
            self.modules.parent.mkdir(parents=True)
            self.modules.symlink_to(modules)
        else:
            self.modules.mkdir(parents=True)
        stub = self.root / "stubs"
        stub.mkdir()
        (stub / "playwright").write_text("#!/bin/sh\nexit 0\n")
        (stub / "playwright").chmod(0o755)
        self.env = {**os.environ, **GIT_ENV, "PATH": f"{stub}:{os.environ['PATH']}"}
        self.test = test

    def repo(self, name="cortex-kb-demo"):
        path = self.root / name
        subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=self.env)
        return path

    def enable(self, target):
        return subprocess.run(["bash", str(self.bin / "verify-enable"), str(target)],
                              capture_output=True, text=True, env=self.env)


class VerifyEnableTests(unittest.TestCase):
    def setUp(self):
        self.tree = TempTree(self)
        self.repo = self.tree.repo()

    def test_adds_acceptance_and_a_verify_recipe(self):
        result = self.tree.enable(self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Next: commit acceptance/ and justfile", result.stdout)
        self.assertIn("~/project-data/demo/verification/<run>/", result.stdout)
        acceptance = self.repo / "acceptance"
        for name in ("playwright.config.ts", "axe.ts", "example.spec.ts"):
            self.assertEqual((acceptance / name).read_text(), (TEMPLATES / name).read_text())
        self.assertEqual((acceptance / ".gitignore").read_text(), "node_modules\n")
        self.assertEqual(os.readlink(acceptance / "node_modules"), str(self.tree.modules))
        recipe = (self.repo / "justfile").read_text()
        self.assertIn("verify url tag='':", recipe)
        self.assertIn("$HOME/project-data/demo/verification/", recipe)
        self.assertNotIn("@PROJECT@", recipe)
        example = (acceptance / "example.spec.ts").read_text()
        self.assertIn("{ tag: '@EXAMPLE-AC1' }", example)
        self.assertIn("expectNoA11yViolations", example)

    def test_second_run_is_a_no_op_and_keeps_edits(self):
        self.assertEqual(self.tree.enable(self.repo).returncode, 0)
        (self.repo / "acceptance/example.spec.ts").write_text("// replaced by the project\n")
        before = (self.repo / "justfile").read_text()
        result = self.tree.enable(self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already in place", result.stdout)
        self.assertNotIn("Next:", result.stdout)
        self.assertEqual((self.repo / "acceptance/example.spec.ts").read_text(), "// replaced by the project\n")
        self.assertEqual((self.repo / "justfile").read_text(), before)

    def test_appends_to_an_existing_justfile(self):
        (self.repo / "justfile").write_text("check:\n    echo ok\n")
        self.assertEqual(self.tree.enable(self.repo).returncode, 0)
        recipe = (self.repo / "justfile").read_text()
        self.assertTrue(recipe.startswith("check:\n    echo ok\n"))
        self.assertEqual(recipe.count("\nverify url"), 1)

    def test_keeps_an_existing_verify_recipe(self):
        (self.repo / "justfile").write_text("verify:\n    echo mine\n")
        result = self.tree.enable(self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already has a verify recipe", result.stdout)
        self.assertEqual((self.repo / "justfile").read_text(), "verify:\n    echo mine\n")

    def test_refuses_a_different_config_and_changes_nothing(self):
        (self.repo / "acceptance").mkdir()
        (self.repo / "acceptance/playwright.config.ts").write_text("export default {};\n")
        result = self.tree.enable(self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("merge it by hand", result.stderr)
        self.assertEqual((self.repo / "acceptance/playwright.config.ts").read_text(), "export default {};\n")
        self.assertFalse((self.repo / "acceptance/axe.ts").exists())
        self.assertFalse((self.repo / "justfile").exists())

    def test_rejects_other_targets_and_missing_tools(self):
        other = self.tree.repo("some-code-repo")
        self.assertEqual(self.tree.enable(other).returncode, 1)
        self.assertFalse((other / "acceptance").exists())
        self.assertNotEqual(self.tree.enable(self.tree.root / "not-a-repo").returncode, 0)
        result = subprocess.run(["bash", str(self.tree.bin / "verify-enable")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        no_playwright = {**self.tree.env, "PATH": "/usr/bin:/bin"}
        result = subprocess.run(["bash", str(self.tree.bin / "verify-enable"), str(self.repo)],
                                capture_output=True, text=True, env=no_playwright)
        self.assertEqual(result.returncode, 1)
        self.assertIn("playwright is not on PATH", result.stderr)

    @unittest.skipUnless(shutil.which("just"), "just not available")
    def test_just_accepts_the_recipe(self):
        self.assertEqual(self.tree.enable(self.repo).returncode, 0)
        result = subprocess.run(["just", "--justfile", str(self.repo / "justfile"), "--summary"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["verify"])


def usable_chromium():
    """Return the shared browser cache if a Chromium there can start, else None."""
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache/ms-playwright")
    candidates = glob.glob(str(cache / "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))
    if not candidates or not shutil.which("ldd"):
        return None
    missing = subprocess.run(["ldd", candidates[0]], capture_output=True, text=True).stdout
    return None if "not found" in missing else cache


def installed_modules():
    for modules in (Path.home() / ".local/share/bootstrap/npm/node_modules", NPM / "node_modules"):
        if (modules / ".bin/playwright").exists():
            return modules
    return None


@unittest.skipUnless(installed_modules(), "pinned Playwright install not present")
class ListTests(unittest.TestCase):
    """The written config loads and finds the tagged example tests on both viewports (no browser needed)."""

    def test_list_shows_tagged_tests_for_desktop_and_phone(self):
        tree = TempTree(self, modules=installed_modules())
        repo = tree.repo()
        self.assertEqual(tree.enable(repo).returncode, 0)
        env = {**tree.env, "VERIFY_URL": "http://127.0.0.1:1", "VERIFY_OUT": str(tree.root / "evidence"),
               "PATH": f"{installed_modules() / '.bin'}:{os.environ['PATH']}"}
        result = subprocess.run(["playwright", "test", "--list", "--config", "acceptance/playwright.config.ts"],
                                cwd=repo, capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Total: 4 tests in 1 file", result.stdout)
        for expected in ("[desktop]", "[phone]"):
            self.assertIn(expected, result.stdout)
        # `just verify <url> @TAG` selects by tag.
        tagged = subprocess.run(["playwright", "test", "--list", "--grep", "@EXAMPLE-AC2",
                                 "--config", "acceptance/playwright.config.ts"],
                                cwd=repo, capture_output=True, text=True, env=env, timeout=120)
        self.assertIn("Total: 2 tests", tagged.stdout)
        self.assertIn("accessibility violations", tagged.stdout)
        self.assertNotIn("main heading", tagged.stdout)
        # Without the inputs the config refuses to run.
        env.pop("VERIFY_URL")
        result = subprocess.run(["playwright", "test", "--list", "--config", "acceptance/playwright.config.ts"],
                                cwd=repo, capture_output=True, text=True, env=env, timeout=120)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("VERIFY_URL", result.stdout + result.stderr)


GOOD_PAGE = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
             'content="width=device-width, initial-scale=1"><title>Smoke</title></head>'
             '<body><main><h1>Smoke page</h1><p>Hello.</p></main></body></html>')
BAD_PAGE = GOOD_PAGE.replace("<h1>Smoke page</h1>", "")


@unittest.skipUnless(installed_modules() and usable_chromium(),
                     "pinned Playwright install or a startable Chromium not present")
class SmokeTests(unittest.TestCase):
    def serve(self, html):
        site = Path(tempfile.mkdtemp(dir=self.tree.root))
        (site / "index.html").write_text(html)
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(site)))
        server.RequestHandlerClass.log_message = lambda *args: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def run_suite(self, url):
        out = Path(tempfile.mkdtemp(dir=self.tree.root, prefix="evidence-"))
        env = {**self.tree.env, "VERIFY_URL": url, "VERIFY_OUT": str(out),
               "PLAYWRIGHT_BROWSERS_PATH": str(usable_chromium()),
               "PATH": f"{installed_modules() / '.bin'}:{os.environ['PATH']}"}
        result = subprocess.run(["playwright", "test", "--config", "acceptance/playwright.config.ts"],
                                cwd=self.repo, capture_output=True, text=True, env=env, timeout=240)
        return result, out

    def setUp(self):
        self.tree = TempTree(self, modules=installed_modules())
        self.repo = self.tree.repo("cortex-kb-smoke")
        self.assertEqual(self.tree.enable(self.repo).returncode, 0)

    def test_example_passes_on_a_good_static_page_on_both_viewports(self):
        result, out = self.run_suite(self.serve(GOOD_PAGE))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads((out / "results.json").read_text())
        self.assertEqual(report["stats"]["expected"], 4)
        self.assertEqual(report["stats"]["unexpected"], 0)
        projects = {test["projectName"] for suite in report["suites"] for spec in suite["specs"]
                    for test in spec["tests"]}
        self.assertEqual(projects, {"desktop", "phone"})
        self.assertFalse(list(out.rglob("*.html")), "no HTML report")

    def test_failure_is_reported_with_a_trace_and_screenshot(self):
        result, out = self.run_suite(self.serve(BAD_PAGE))
        self.assertNotEqual(result.returncode, 0)
        report = json.loads((out / "results.json").read_text())
        self.assertGreaterEqual(report["stats"]["unexpected"], 2)
        self.assertTrue(list(out.rglob("trace.zip")))
        self.assertTrue(list(out.rglob("*.png")))


if __name__ == "__main__":
    unittest.main()
