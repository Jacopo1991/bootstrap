#!/usr/bin/env python3
"""The package cap rule: one policy file, read once, applied by the shared gate command
package-cap-check to every manifest, lock, Dockerfile and compose format, offline.

Fixtures are temporary repositories; no real venv, repository or Docker is touched."""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "system/agent-policy/package-caps.toml"
COMMAND = ROOT / "home/dot_local/bin/executable_package-cap-check"
GATE_CONFIG = ROOT / "home/dot_local/share/bootstrap/pre-commit/pre-commit-config.yaml"
IMAGE = "ghcr.io/berriai/litellm"

loader = importlib.machinery.SourceFileLoader("package_cap_check", str(COMMAND))
spec = importlib.util.spec_from_loader(loader.name, loader)
check = importlib.util.module_from_spec(spec)
loader.exec_module(check)


class PolicyFile(unittest.TestCase):
    def test_policy_names_the_rule_and_is_parsed_once(self):
        with patch.object(tomllib, "loads", wraps=tomllib.loads) as loads:
            caps = check.load_policy(POLICY)
        self.assertEqual(loads.call_count, 1)
        self.assertEqual(list(caps), ["litellm"])
        litellm = caps["litellm"]
        self.assertEqual(litellm.max_version, "1.106.2")
        self.assertEqual(litellm.images, {"ghcr.io/berriai/litellm", "berriai/litellm"})
        self.assertIn("2026-10-21", litellm.reason)
        self.assertEqual(litellm.source, "https://docs.litellm.ai/blog/security-incident-oct-21-2026")
        raw = tomllib.loads(POLICY.read_text())
        self.assertEqual(set(raw["packages"]["litellm"]), {"max_version", "images", "reason", "source", "added"})

    def test_another_package_is_one_table(self):
        with tempfile.TemporaryDirectory() as temp:
            policy = Path(temp) / "caps.toml"
            policy.write_text(POLICY.read_text() + '\n[packages.Other_Pkg]\nmax_version = "2.0"\n'
                              'reason = "fixture"\nsource = "https://example.invalid"\n')
            caps = check.load_policy(policy)
        self.assertEqual(set(caps), {"litellm", "other-pkg"})
        self.assertEqual(caps["other-pkg"].images, set())
        self.assertIsNone(caps["other-pkg"].version_reason("2.0.0"))
        self.assertIsNotNone(caps["other-pkg"].version_reason("2.0.1"))

    def test_gate_config_runs_the_check_on_every_commit(self):
        text = GATE_CONFIG.read_text()
        self.assertIn("- id: package-caps\n", text)
        hook = text.split("- id: package-caps\n", 1)[1]
        self.assertIn("entry: package-cap-check\n", hook)
        self.assertIn("pass_filenames: false\n", hook)
        self.assertIn("always_run: true\n", hook)
        for script in ("executable_new-project", "executable_pre-commit-enable"):
            self.assertIn("package-cap-check", (ROOT / "home/dot_local/bin" / script).read_text(), script)


class Versions(unittest.TestCase):
    def test_pep440_ordering_against_the_cap(self):
        cap = check.version_key("1.106.2")
        for lower in ("1.106.2", "1.106.2.0", "v1.106.2", "1.106.1", "1.104.0", "1.106.2rc1", "1.106.2.dev3", "1.99.99"):
            self.assertLessEqual(check.version_key(lower), cap, lower)
        for higher in ("1.106.3", "1.107.0", "1.107.0rc1", "1.106.2.post1", "1.106.10", "2.0", "1.107.0.dev1"):
            self.assertGreater(check.version_key(higher), cap, higher)
        for junk in ("latest", "1.106.*", "", "main-v1.106.2", ">=1.106.2"):
            self.assertIsNone(check.version_key(junk), junk)

    def test_requirement_parsing(self):
        self.assertEqual(check.parse_requirement("litellm==1.106.2"), ("litellm", "1.106.2", True))
        self.assertEqual(check.parse_requirement("LiteLLM[proxy] == 1.104.2 ; python_version > '3.8'"),
                         ("litellm", "1.104.2", True))
        self.assertEqual(check.parse_requirement("litellm")[1:], (None, False))
        self.assertEqual(check.parse_requirement("litellm>=1.100,<1.107")[1:], (None, False))
        self.assertEqual(check.parse_requirement("litellm==1.106.*")[1:], (None, False))
        self.assertEqual(check.parse_requirement("litellm @ https://example.invalid/litellm.whl")[1:], (None, False))
        self.assertEqual(check.poetry_requirement("litellm", "1.106.2"), ("litellm", "1.106.2", True))
        self.assertEqual(check.poetry_requirement("litellm", {"version": "^1.100", "extras": ["proxy"]})[1:], (None, False))

    def test_image_tags(self):
        cap = check.load_policy(POLICY)["litellm"]
        for allowed in ("v1.106.2", "v1.106.2-stable", "1.104.0", "v1.106.2.rc.1", "litellm_stable_release_branch-v1.106.2-stable"):
            self.assertIsNone(cap.tag_reason(allowed), allowed)
        for refused in (None, "latest", "main", "main-latest", "main-stable", "main-v1.106.2", "v1.107.0", "v1.107.0-stable", "stable", "v1.106.3"):
            self.assertIsNotNone(cap.tag_reason(refused), refused)
        self.assertEqual(check.image_name("docker.io/berriai/litellm:v1.106.2@sha256:abc"), ("berriai/litellm", "v1.106.2"))
        self.assertEqual(check.image_name("ghcr.io/berriai/litellm"), (IMAGE, None))
        self.assertEqual(check.image_name("localhost:5000/berriai/litellm"), ("localhost:5000/berriai/litellm", None))


class Manifests(unittest.TestCase):
    """Each format allowed and refused, through the command exactly as the gate runs it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="package-caps-")
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)

    def tearDown(self):
        self.temp.cleanup()

    def run_check(self, *paths: str, env_policy: str | None = str(POLICY), args=()) -> tuple[int, str]:
        env = {"PATH": os.environ["PATH"], "HOME": self.temp.name, "PYTHONDONTWRITEBYTECODE": "1"}
        if env_policy is not None:
            env["PACKAGE_CAPS_FILE"] = env_policy
        result = subprocess.run([sys.executable, "-I", str(COMMAND), *args, *paths], cwd=self.repo, env=env,
                                capture_output=True, text=True, timeout=60)
        return result.returncode, result.stdout + result.stderr

    def write(self, name: str, text: str) -> str:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return name

    def allowed(self, name: str, text: str):
        code, output = self.run_check(self.write(name, text))
        self.assertEqual(code, 0, (name, text, output))
        self.assertIn("1 manifest(s)", output, (name, output))

    def refused(self, name: str, text: str, *expected: str):
        code, output = self.run_check(self.write(name, text))
        self.assertEqual(code, 1, (name, text, output))
        self.assertIn(f"{name}:", output)
        for fragment in ("1.106.2", "security-incident-oct-21-2026", *expected):
            self.assertIn(fragment, output, (name, text, output))

    def test_requirements_and_constraints(self):
        for name in ("requirements.txt", "requirements-dev.txt", "constraints.txt", "constraints-prod.txt", "api/requirements.txt"):
            self.allowed(name, "requests==2.32.0\nlitellm==1.106.2  # pinned\n")
            self.allowed(name, "litellm[proxy]==1.104.2 \\\n    --hash=sha256:0123\n")
            self.allowed(name, 'litellm==1.106.2; python_version >= "3.9"\n# litellm\n-r other.txt\n')
            self.refused(name, "litellm==1.107.0\n", "above the cap")
            self.refused(name, "litellm\n", "pinned exactly")
            self.refused(name, "litellm>=1.100\n", "pinned exactly")
            self.refused(name, "litellm~=1.106.0\n", "pinned exactly")
            self.refused(name, "litellm==1.106.*\n", "pinned exactly")
            self.refused(name, "-e git+https://github.com/BerriAI/litellm@main#egg=litellm\n", "pinned exactly")
            self.refused(name, "litellm @ https://example.invalid/litellm-1.107.0.tar.gz\n", "pinned exactly")

    def test_pyproject(self):
        self.allowed("pyproject.toml", '[project]\nname = "x"\ndependencies = ["requests", "litellm==1.106.2"]\n')
        self.allowed("pyproject.toml", '[project]\nname = "x"\n[project.optional-dependencies]\nproxy = ["litellm[proxy]==1.104.2"]\n')
        self.allowed("pyproject.toml", '[dependency-groups]\ndev = ["litellm==1.106.2", {include-group = "test"}]\ntest = ["pytest"]\n')
        self.allowed("pyproject.toml", '[tool.poetry.dependencies]\npython = "^3.12"\nlitellm = "1.106.2"\n')
        self.allowed("pyproject.toml", '[tool.poetry.dependencies]\nlitellm = {version = "==1.104.0", extras = ["proxy"]}\n')
        self.refused("pyproject.toml", '[project]\nname = "x"\ndependencies = ["litellm==1.107.0"]\n', "above the cap")
        self.refused("pyproject.toml", '[project]\ndependencies = ["litellm>=1.100"]\n', "no uv.lock or poetry.lock")
        self.refused("pyproject.toml", '[project]\ndependencies = ["litellm"]\n', "no uv.lock or poetry.lock")
        self.refused("pyproject.toml", '[tool.poetry.dependencies]\nlitellm = "^1.100"\n', "no uv.lock or poetry.lock")
        self.refused("pyproject.toml", '[tool.poetry.group.dev.dependencies]\nlitellm = "*"\n', "no uv.lock or poetry.lock")
        self.refused("pyproject.toml", '[build-system]\nrequires = ["litellm>1"]\n', "no uv.lock or poetry.lock")
        self.refused("pyproject.toml", '[tool.uv]\nconstraint-dependencies = ["litellm<1.108"]\n', "no uv.lock or poetry.lock")

    def test_pyproject_range_needs_a_lock_pinning_an_allowed_version(self):
        project = '[project]\nname = "x"\ndependencies = ["litellm>=1.100"]\n'
        lock = '[[package]]\nname = "litellm"\nversion = "{}"\n'
        self.write("uv.lock", lock.format("1.104.0"))
        self.allowed("pyproject.toml", project)
        self.write("uv.lock", lock.format("1.107.0"))
        self.refused("pyproject.toml", project, "uv.lock pins 1.107.0")
        self.write("uv.lock", '[[package]]\nname = "requests"\nversion = "2.32.0"\n')
        self.refused("pyproject.toml", project, "lock file does not pin it")
        (self.repo / "uv.lock").unlink()
        self.write("poetry.lock", lock.format("1.106.2"))
        self.allowed("pyproject.toml", '[tool.poetry.dependencies]\nlitellm = "^1.100"\n')
        self.write("poetry.lock", lock.format("1.106.3"))
        self.refused("pyproject.toml", '[tool.poetry.dependencies]\nlitellm = "^1.100"\n', "poetry.lock pins 1.106.3")

    def test_lock_files(self):
        for name in ("uv.lock", "poetry.lock"):
            self.allowed(name, '[[package]]\nname = "requests"\nversion = "2.32.0"\n\n[[package]]\nname = "litellm"\nversion = "1.104.0"\n')
            self.refused(name, '[[package]]\nname = "litellm"\nversion = "1.107.0"\n', "above the cap", f"{name}:2")
            self.refused(name, '[[package]]\nname = "litellm"\nsource = { git = "https://example.invalid" }\n', "pinned exactly")
        self.allowed("Pipfile.lock", '{"default": {"litellm": {"version": "==1.106.2"}}, "develop": {}}')
        self.refused("Pipfile.lock", '{"default": {}, "develop": {"litellm": {"version": "==1.107.0"}}}', "above the cap")
        self.refused("Pipfile.lock", '{"default": {"litellm": {"git": "https://example.invalid", "ref": "main"}}}', "pinned exactly")

    def test_dockerfiles(self):
        for name in ("Dockerfile", "Dockerfile.api", "api.Dockerfile", "Containerfile", "deploy/Dockerfile"):
            self.allowed(name, f"FROM {IMAGE}:v1.106.2-stable\nRUN pip install \"litellm==1.106.2\" && pip install requests\n")
            self.allowed(name, f"FROM --platform=linux/amd64 docker.io/berriai/litellm:v1.104.0 AS base\nFROM python:3.12\n")
            self.allowed(name, "FROM python:3.12\nRUN uv pip install -r requirements.txt\nRUN pip install litellm==1.106.2 \\\n    requests\n")
            self.refused(name, f"FROM {IMAGE}:main-v1.106.2-stable\n", "is refused")
            self.refused(name, f"FROM {IMAGE}:main-latest\n", "is refused")
            self.refused(name, f"FROM {IMAGE}:latest\n", "is refused")
            self.refused(name, f"FROM {IMAGE}\n", "has no tag")
            self.refused(name, f"FROM {IMAGE}@sha256:0123\n", "has no tag")
            self.refused(name, "FROM berriai/litellm:v1.107.0\n", "above the cap")
            self.refused(name, "FROM python:3.12\nRUN pip install litellm\n", "pinned exactly")
            self.refused(name, "FROM python:3.12\nRUN apt-get update && uv pip install litellm==1.107.0\n", "above the cap")
            self.refused(name, "FROM python:3.12\nRUN python -m pip install --no-cache-dir 'litellm>=1.100'\n", "pinned exactly")
            self.refused(name, 'FROM python:3.12\nRUN ["pipx", "install", "litellm"]\n', "pinned exactly")

    def test_compose_files(self):
        for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml", "compose.override.yaml", "docker-compose.prod.yml"):
            self.allowed(name, f'services:\n  db:\n    image: postgres:16\n  proxy:\n    image: "{IMAGE}:v1.106.2-stable"\n')
            self.allowed(name, "services:\n  proxy:\n    image: ${LITELLM_IMAGE}\n")
            self.refused(name, f"services:\n  proxy:\n    image: {IMAGE}:latest\n", "is refused")
            self.refused(name, f"services:\n  proxy:\n    image: {IMAGE}:main-stable\n", "is refused")
            self.refused(name, f"services:\n  proxy:\n    image: berriai/litellm:v1.107.0\n", "above the cap")
            self.refused(name, f"services:\n  proxy:\n    image: {IMAGE}\n", "has no tag")
            self.refused(name, f"services:\n  proxy:\n    image: '{IMAGE}@sha256:0123'\n", "has no tag")

    def test_repository_scan_and_fallbacks(self):
        self.write("requirements.txt", "litellm==1.107.0\n")
        self.write("docs/notes.md", "litellm:latest\n")
        self.write("app/compose.yaml", f"services:\n  p:\n    image: {IMAGE}:latest\n")
        code, output = self.run_check()
        self.assertEqual(code, 0, output)  # nothing tracked yet
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        code, output = self.run_check()
        self.assertEqual(code, 1, output)
        self.assertIn("requirements.txt:1:", output)
        self.assertIn("app/compose.yaml:3:", output)
        self.assertNotIn("notes.md", output)
        self.assertIn("2 finding(s)", output)
        plain = Path(self.temp.name) / "plain"
        (plain / ".venv").mkdir(parents=True)
        (plain / ".venv/requirements.txt").write_text("litellm\n")
        (plain / "requirements.txt").write_text("litellm==1.106.2\n")
        result = subprocess.run([sys.executable, "-I", str(COMMAND), "--policy", str(POLICY)], cwd=plain,
                                capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK (1 manifest(s), 1 capped package(s))", result.stdout)

    def test_policy_file_is_required(self):
        self.write("requirements.txt", "litellm==1.106.2\n")
        missing = str(self.repo / "absent.toml")
        code, output = self.run_check("requirements.txt", env_policy=missing)
        self.assertEqual(code, 2, output)
        self.assertIn("cannot read the policy file", output)
        self.assertIn("install.sh", output)
        code, output = self.run_check("requirements.txt", env_policy=None, args=("--policy", str(POLICY)))
        self.assertEqual(code, 0, output)
        broken = self.write("broken.toml", "[packages]\n")
        code, output = self.run_check("requirements.txt", env_policy=str(self.repo / broken))
        self.assertEqual(code, 2, output)


if __name__ == "__main__":
    unittest.main(verbosity=1)
