#!/usr/bin/env python3
"""The package cap rule: one policy file, read once, applied by the shared gate command
package-cap-check to every manifest, lock, Dockerfile and compose format, offline.

Fixtures are temporary repositories; no real venv, repository or Docker is touched."""
import importlib.machinery
import importlib.util
import contextlib
import io
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "system/agent-policy/package-caps.toml"
COMMAND = ROOT / "system/agent-policy/package_caps.py"
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

    def test_bootstrap_gate_runs_from_checkout_before_install(self):
        config = (ROOT / '.pre-commit-config.yaml').read_text()
        hook = config.split('- id: package-caps\n', 1)[1]
        entry = next(line.strip().removeprefix('entry: ') for line in hook.splitlines()
                     if line.strip().startswith('entry: '))
        self.assertEqual(entry, 'python3 ci/package-cap-check.py')
        self.assertIn('pass_filenames: false\n', hook)
        self.assertIn('always_run: true\n', hook)
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / 'requirements.txt'
            for requirement, expected in (('litellm==1.106.2', 0), ('litellm==1.107.0', 1)):
                manifest.write_text(requirement + '\n')
                result = subprocess.run([*shlex.split(entry), str(manifest)], cwd=ROOT,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                if expected:
                    self.assertIn('above the cap 1.106.2', result.stdout)


class Versions(unittest.TestCase):
    def test_pep440_ordering_against_the_cap(self):
        cap = check.version_key("1.106.2")
        for lower in ("1.106.2", "1.106.2.0", "v1.106.2", "1.106.1", "1.104.0", "1.106.2rc1", "1.106.2.dev3", "1.99.99"):
            self.assertLessEqual(check.version_key(lower), cap, lower)
        for higher in ("1.106.3", "1.107.0", "1.107.0rc1", "1.106.2.post1", "1.106.10", "2.0", "1.107.0.dev1"):
            self.assertGreater(check.version_key(higher), cap, higher)
        for junk in ("latest", "1.106.*", "", "main-v1.106.2", ">=1.106.2", "1.106.2+custom"):
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
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = check.main(["--root", str(self.repo), *args, *(str(self.repo / p) for p in paths)],
                              policy_path=Path(env_policy) if env_policy else POLICY)
        return code, output.getvalue()

    def write(self, name: str, text: str) -> str:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return name

    def allowed(self, name: str, text: str):
        code, output = self.run_check(self.write(name, text))
        self.assertEqual(code, 0, (name, text, output))
        self.assertIn("1 manifest(s)", output, (name, output))

    def refused(self, name: str, text: str, *expected: str, reported_path=None):
        code, output = self.run_check(self.write(name, text))
        self.assertEqual(code, 1, (name, text, output))
        self.assertIn(f"{reported_path or name}:", output)
        for fragment in ("1.106.2", "security-incident-oct-21-2026", *expected):
            self.assertIn(fragment, output, (name, text, output))

    def test_requirements_and_constraints(self):
        self.write("other.txt", "requests==2.32.0\n")
        self.write("api/other.txt", "requests==2.32.0\n")
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

    def test_uncapped_requirements_are_ignored_line_by_line(self):
        # B1: these are valid inputs for other tools, not this policy's concern.
        examples = ('-e .', '--editable ./wheel', '--prefer-binary', '--unknown-option',
                    '.', './wheel', 'git+https://example.invalid/repo#egg=',
                    'git+https://example.invalid/repo#egg=numpy',
                    'https://example.invalid/innocent.whl', 'https://[', 'numpy==$VERSION',
                    'numpy', 'litellm-extra>=9', './other_litellm-9-py3-none-any.whl')
        for name in ('requirements-dev.txt', 'constraints.in'):
            for example in examples:
                with self.subTest(name=name, example=example):
                    self.allowed(name, example + '\n')
                    # An unrelated line remains ignored beside an allowed pin.
                    self.allowed(name, example + '\nlitellm==1.106.2\n')
        self.refused('requirements.txt', '--editable git+https://example.invalid/repo#egg=litellm\n',
                     'unverifiable editable source')

    def test_uncapped_installer_arguments_are_ignored(self):
        for args in ('--timeout 100 numpy', '--no-compile numpy', '--unknown-flag numpy',
                     '-e .', '.', './wheel', 'git+https://example.invalid/repo#egg=',
                     '--prefer-binary litellm==1.106.2',
                     '--timeout 100 --no-compile litellm==1.106.2'):
            with self.subTest(args=args):
                self.allowed('Dockerfile', 'RUN pip install ' + args + '\n')
                self.allowed('compose.yaml', 'services: {app: {command: ' + repr('pip install ' + args) + '}}\n')
        for command in ('env pip install numpy', 'sh -c "pip install numpy"',
                        'xargs pip install numpy', 'uv sync', 'pip --unknown install numpy',
                        'pip --unknown install litellm==1.106.2'):
            self.allowed('Dockerfile', 'RUN ' + command + '\n')

    def test_uncapped_locks_and_images_are_ignored(self):
        for name in ('uv.lock', 'poetry.lock'):
            self.allowed(name, '[[package]]\nname="numpy"\nsource={git="https://example.invalid/repo"}\n')
        self.allowed('Pipfile.lock', '{"default":{"numpy":{"git":"https://example.invalid/repo"}}}')
        for image in ('numpy:latest', 'other/langchain-litellm:latest', '${IMAGE}', 'numpy:${TAG}'):
            self.allowed('Dockerfile', 'FROM ' + image + '\n')
            self.allowed('compose.yaml', 'services: {app: {image: "' + image + '"}}\n')

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
        self.refused("pyproject.toml", '[tool.uv]\nconstraint-dependencies = ["litellm<1.108"]\n', "unverified uv")
        self.allowed("pyproject.toml", '[tool.uv]\nconstraint-dependencies = ["litellm<=1.106.2"]\n')

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

    def test_uv_dependency_tables_and_uncapped_requirements(self):
        for key in ('constraint-dependencies', 'build-constraint-dependencies', 'override-dependencies'):
            for requirement in ('setuptools==80.9.0', 'setuptools @ https://example.invalid/setuptools.whl'):
                self.allowed('pyproject.toml', '[tool.uv]\n' + key +
                             ' = [{requirement = "' + requirement + '", hashes = ["sha256:abc"]}]\n')
            for requirement, allowed in (('litellm<=1.106.2', True),
                                         ('litellm==1.106.2', True),
                                         ('litellm==1.107.0', False), ('litellm', False)):
                text = '[tool.uv]\n' + key + ' = [{requirement = "' + requirement + '", hashes = []}]\n'
                if allowed:
                    self.allowed('pyproject.toml', text)
                else:
                    self.refused('pyproject.toml', text, 'unverified uv')
        for table, key in (('project', 'dependencies'), ('build-system', 'requires'),
                           ('project.optional-dependencies', 'test'), ('dependency-groups', 'test'),
                           ('tool.uv', 'dev-dependencies')):
            for requirement, allowed in (('setuptools==80.9.0', True), ('litellm==1.106.2', True),
                                         ('litellm==1.107.0', False), ('litellm', False)):
                text = '[' + table + ']\n' + key + ' = [{requirement = "' + requirement + '", hashes = []}]\n'
                if allowed:
                    self.allowed('pyproject.toml', text)
                else:
                    self.refused('pyproject.toml', text)

    def test_uv_workspace_member_uses_owning_root_lock(self):
        self.write('pyproject.toml', '[tool.uv.workspace]\nmembers=["packages/*"]\nexclude=["packages/excluded"]\n')
        member = 'packages/member/pyproject.toml'
        project = '[project]\nname="member"\ndependencies=["litellm>=1.100"]\n'
        lock = '[[package]]\nname="litellm"\nversion="{}"\n'
        self.write('uv.lock', lock.format('1.106.2'))
        self.allowed(member, project)
        self.refused('unrelated/pyproject.toml', project, 'no uv.lock or poetry.lock')
        self.refused('packages/excluded/pyproject.toml', project, 'no uv.lock or poetry.lock')
        self.write('uv.lock', lock.format('1.107.0'))
        self.refused(member, project, 'uv.lock pins 1.107.0')
        # A stale member lock must not hide the owning workspace's resolution.
        self.write('packages/member/uv.lock', lock.format('1.106.2'))
        self.refused(member, project, 'uv.lock pins 1.107.0')
        self.write('uv.lock', lock.format('1.104.0'))
        self.refused(member, project.replace('>=1.100', '>=1.106'), 'manifest/lock mismatch')

    def test_pep723_preserves_uv_source_metadata(self):
        for dependencies in ('[]', '["litellm==1.106.2"]'):
            prefix = '# /// script\n# dependencies = ' + dependencies + '\n'
            self.allowed('script.py', prefix + '# [tool.uv.sources]\n# numpy = {path="./numpy"}\n# ///\n')
            self.refused('script.py', prefix + '# [tool.uv.sources]\n# litellm = {path="./local"}\n# ///\n',
                         'alternate uv source refused')

    def test_lock_files(self):
        for name in ("uv.lock", "poetry.lock"):
            self.allowed(name, '[[package]]\nname = "requests"\nversion = "2.32.0"\n\n[[package]]\nname = "litellm"\nversion = "1.104.0"\n')
            self.refused(name, '[[package]]\nname = "litellm"\nversion = "1.107.0"\n', "above the cap", f"{name}:2")
            self.refused(name, '[[package]]\nname = "litellm"\nsource = { git = "https://example.invalid" }\n', "pinned exactly")
        self.allowed("Pipfile.lock", '{"default": {"litellm": {"version": "==1.106.2"}}, "develop": {}}')
        self.refused("Pipfile.lock", '{"default": {}, "develop": {"litellm": {"version": "==1.107.0"}}}', "above the cap")
        self.refused("Pipfile.lock", '{"default": {"litellm": {"git": "https://example.invalid", "ref": "main"}}}', "pinned exactly")

    def test_repository_gate_caps_transitive_locks_without_direct_declarations(self):
        # Existing projects may have no workspace resolver constraints. The gate
        # must still reject a transitive resolution in every supported lock format.
        self.write('pyproject.toml', '[project]\nname="demo"\ndependencies=["parent-package"]\n')
        for name, template in (
            ('uv.lock', '[[package]]\nname="parent-package"\nversion="1.0"\ndependencies=[{name="litellm"}]\n[[package]]\nname="litellm"\nversion="VERSION"\n'),
            ('poetry.lock', '[[package]]\nname="parent-package"\nversion="1.0"\n[package.dependencies]\nlitellm="*"\n[[package]]\nname="litellm"\nversion="VERSION"\n'),
            ('Pipfile.lock', '{"default":{"parent-package":{"version":"==1.0"},"litellm":{"version":"==VERSION"}}}'),
        ):
            for version, expected in (('1.106.2', 0), ('1.107.0', 1)):
                with self.subTest(lock=name, version=version):
                    self.write(name, template.replace('VERSION', version))
                    subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
                    code, output = self.run_check()
                    self.assertEqual(code, expected, output)
                    if expected:
                        self.assertIn(f'{name}:', output)
                        self.assertIn('litellm 1.107.0 is above the cap 1.106.2', output)
            (self.repo / name).unlink()
            subprocess.run(['git', '-C', str(self.repo), 'add', '-u'], check=True)

    def test_gate_parses_policy_once_for_the_entire_repository(self):
        self.write('requirements.txt', 'litellm==1.106.2\n')
        self.write('uv.lock', '[[package]]\nname="litellm"\nversion="1.106.2"\n')
        self.write('compose.yaml', 'services: {proxy: {image: "berriai/litellm:v1.106.2"}}\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        with patch.object(check, 'load_policy', wraps=check.load_policy) as load, \
                patch.object(tomllib, 'loads', wraps=tomllib.loads) as parse:
            code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertIn('3 manifest(s)', output)
        load.assert_called_once_with(POLICY)
        policy_parses = [call for call in parse.call_args_list if call.args[0] == POLICY.read_text()]
        self.assertEqual(len(policy_parses), 1)

    def test_dockerfiles(self):
        self.write("requirements.txt", "requests==2.32.0\n")
        self.write("deploy/requirements.txt", "requests==2.32.0\n")
        for name in ("Dockerfile", "Dockerfile.api", "api.Dockerfile", "deploy/Dockerfile"):
            self.allowed(name, f"FROM {IMAGE}:v1.106.2-stable\nRUN pip install \"litellm==1.106.2\" && pip install requests\n")
            self.allowed(name, f"FROM --platform=linux/amd64 docker.io/berriai/litellm:v1.104.0 AS base\nFROM python:3.12\n")
            self.allowed(name, "FROM python:3.12\nRUN uv pip install -r requirements.txt\nRUN pip install litellm==1.106.2 \\\n    requests\n")
            self.refused(name, f"FROM {IMAGE}:main-v1.106.2-stable\n", "is refused")
            self.refused(name, f"FROM {IMAGE}:main-latest\n", "is refused")
            self.refused(name, f"FROM {IMAGE}:latest\n", "is refused")
            self.refused(name, f"FROM {IMAGE}\n", "pinned exactly")
            self.refused(name, f"FROM {IMAGE}@sha256:0123\n", "pinned exactly")
            self.refused(name, "FROM berriai/litellm:v1.107.0\n", "above the cap")
            self.refused(name, "FROM python:3.12\nRUN pip install litellm\n", "pinned exactly")
            self.refused(name, "FROM python:3.12\nRUN apt-get update && uv pip install litellm==1.107.0\n", "above the cap")
            self.refused(name, "FROM python:3.12\nRUN python -m pip install --no-cache-dir 'litellm>=1.100'\n", "pinned exactly")
            self.refused(name, 'FROM python:3.12\nRUN ["pipx", "install", "litellm"]\n', "pinned exactly")

    def test_container_wrappers_use_only_the_text_pin(self):
        # The last amendment replaces all command interpretation with text checks.
        for version in ('1.106.2', '1.107.0'):
            commands = [f'sh -c "pip install litellm=={version}"',
                        f'env pip install litellm=={version}',
                        f'xargs -r pip install litellm=={version}',
                        f'["sh", "-c", "pip install litellm=={version}"]',
                        f'["env", "pip", "install", "litellm=={version}"]',
                        f'["xargs", "pip", "install", "litellm=={version}"]',
                        f'sh -c "echo ready && pip install litellm=={version}"']
            for command in commands:
                with self.subTest(command=command):
                    if version == '1.106.2':
                        self.allowed('Dockerfile', 'RUN ' + command + '\n')
                        self.allowed('compose.yaml', 'services:\n  app:\n    command: ' + command + '\n')
                    else:
                        self.refused('Dockerfile', 'RUN ' + command + '\n', 'above the cap')
                        self.refused('compose.yaml', 'services:\n  app:\n    command: ' + command + '\n',
                                     'above the cap')
        for command in ('pip install litellm==1.106.2', '["pip", "install", "litellm==1.106.2"]'):
            self.allowed('Dockerfile', 'RUN ' + command + '\n')
            self.allowed('compose.yaml', 'services: {app: {command: ' + command + '}}\n')
        self.refused('compose.yaml', 'services: {app: {command: "pip install litellm==1.107.0"}}\n',
                     'above the cap')
        self.refused('compose.yaml', 'services: {app: {entrypoint: [sh, -c, "pip install litellm==1.107.0"]}}\n',
                     'above the cap')

    def test_artifact_identity_stays_structured_only_in_requirements(self):
        for artifact in ('./litellm-1.106.2-py3-none-any.whl',
                         'https://example.invalid/litellm-1.106.2-py3-none-any.whl'):
            self.allowed('requirements.txt', artifact + '\n')
            self.allowed('Dockerfile', 'RUN pip install ' + artifact + '\n')
        for artifact in ('./litellm-1.107.0-py3-none-any.whl',
                         'litellm@https://example.invalid/innocent-1.0-py3-none-any.whl',
                         'git+https://example.invalid/repo#egg=litellm'):
            self.refused('requirements.txt', artifact + '\n')
        # A wheel filename is not a whole-token name; @ and = delimit names.
        self.allowed('Dockerfile', 'RUN pip install ./litellm-1.107.0-py3-none-any.whl\n')
        for artifact in ('litellm@https://example.invalid/innocent-1.0-py3-none-any.whl',
                         'git+https://example.invalid/repo#egg=litellm'):
            self.refused('Dockerfile', 'RUN pip install ' + artifact + '\n')

    def test_compose_files(self):
        for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml", "compose.override.yaml", "docker-compose.prod.yml"):
            self.allowed(name, f'services:\n  db:\n    image: postgres:16\n  proxy:\n    image: "{IMAGE}:v1.106.2-stable"\n')
            self.allowed(name, "services:\n  proxy:\n    image: ${LITELLM_IMAGE}\n")
            self.refused(name, f"services:\n  proxy:\n    image: {IMAGE}:latest\n", "is refused")
            self.refused(name, f"services:\n  proxy:\n    image: {IMAGE}:main-stable\n", "is refused")
            self.refused(name, f"services:\n  proxy:\n    image: berriai/litellm:v1.107.0\n", "above the cap")
            self.refused(name, f"services:\n  proxy:\n    image: {IMAGE}\n", "pinned exactly")
            self.refused(name, f"services:\n  proxy:\n    image: '{IMAGE}@sha256:0123'\n", "pinned exactly")

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
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = check.main(["--root", str(plain)], policy_path=POLICY)
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("OK (1 manifest(s), 1 capped package(s))", output.getvalue())

    def test_policy_file_is_required(self):
        self.write("requirements.txt", "litellm==1.106.2\n")
        missing = str(self.repo / "absent.toml")
        code, output = self.run_check("requirements.txt", env_policy=missing)
        self.assertEqual(code, 2, output)
        self.assertIn("cannot read the policy file", output)
        self.assertIn("install.sh", output)
        code, output = self.run_check("requirements.txt", env_policy=None)
        self.assertEqual(code, 0, output)
        broken = self.write("broken.toml", "[packages]\n")
        code, output = self.run_check("requirements.txt", env_policy=str(self.repo / broken))
        self.assertEqual(code, 2, output)

    def test_includes_artifacts_and_expansions(self):
        for include in ('-r deps.txt', '-c deps.txt', '--requirement=deps.txt', '--constraint deps.txt'):
            self.write('deps.txt', 'litellm==1.107.0\n')
            code, output = self.run_check(self.write('requirements.in', include + '\n'))
            self.assertEqual(code, 1, output)
            self.assertIn('deps.txt:1:', output)
            self.assertIn('above the cap 1.106.2', output)
        for body in ('-r https://example.invalid/litellm.txt\n', 'litellm==$VERSION\n'):
            self.refused('requirements.txt', body)
        code, output = self.run_check(self.write('requirements.txt', '-r missing-litellm.txt\n'))
        self.assertEqual(code, 1, output)
        self.assertIn('missing-litellm.txt:1: requirements include is unavailable', output)
        self.assertIn('1.106.2', output)
        self.assertIn('security-incident-oct-21-2026', output)
        self.write('nested.txt', 'litellm==1.104.0\n')
        self.write('deps.txt', '-c nested.txt\n')
        self.allowed('requirements.in', '-r deps.txt\n')
        for include in ('-r requirements.txt', '-r https://example.invalid/deps.txt', '-r missing.txt'):
            self.allowed('requirements.txt', include + '\n')
        self.write('deps.txt', '-r requirements.txt\nlitellm==1.107.0\n')
        self.refused('requirements.txt', '-r deps.txt\n', 'above the cap', reported_path='deps.txt')
        for operand in ('./litellm-1.107.0-py3-none-any.whl',
                        'https://example.invalid/litellm-1.107.0-py3-none-any.whl',
                        'git+https://example.invalid/litellm',
                        'litellm==1.106.2+custom', 'litellm==1.106.2.post1', 'litellm===1.106.2'):
            self.refused('requirements.txt', operand + '\n')
        self.allowed('requirements.txt', './litellm-1.106.2-py3-none-any.whl\n')

    def test_container_variables_are_text_and_digests_still_need_approval(self):
        self.refused('compose.yaml', 'x: &proxy {image: berriai/litellm:latest}\nservices: {proxy: *proxy}\n')
        self.refused('compose.yaml', 'services: {proxy: {image: "berriai/litellm:${TAG:-v1.106.2}"}}\n')
        self.refused('compose.yaml', 'services: {proxy: {image: "berriai/litellm:${TAG:-v1.107.0}"}}\n')
        self.allowed('compose.yaml', 'services: {proxy: {image: "${IMAGE}"}}\n')
        for name in ('Dockerfile.build', 'custom.Dockerfile'):
            self.refused(name, 'ARG TAG=v1.106.2\nFROM berriai/litellm:$TAG\n')
            self.refused(name, 'ARG TAG=v1.107.0\nFROM berriai/litellm:${TAG}\n')
            self.refused(name, 'ARG TAG\nFROM berriai/litellm:$TAG\n')
            for suffix in ('post1', '+custom'):
                tag = 'v1.106.2.' + suffix if suffix == 'post1' else 'v1.106.2' + suffix
                self.refused(name, 'FROM berriai/litellm:' + tag + '\n')
            self.refused(name, 'FROM berriai/litellm:v1.106.2@sha256:' + 'a' * 64 + '\n')
        cap = check.load_policy(POLICY)['litellm']
        cap.approved_digests.add('sha256:' + 'a' * 64)
        checker = check.Checker({'litellm': cap})
        path = self.repo / self.write('Dockerfile', 'FROM berriai/litellm:v1.106.2@sha256:' + 'a' * 64)
        checker.check(path)
        self.assertEqual(checker.findings, [])
        path.write_text('FROM berriai/litellm:v1.107.0@sha256:' + 'a' * 64)
        checker.check(path)
        self.assertEqual(len(checker.findings), 1)
        self.assertIn('above the cap', checker.findings[0])

    def test_dockerfile_discovery_uses_filename_patterns(self):
        for name in ('docs/dockerfile-notes.md', 'dockerfile-custom', 'odd-containerfile',
                     'custom.Dockerfile.build', 'Containerfile', 'anything.yaml', 'weird-compose.yaml'):
            code, output = self.run_check(self.write(name, 'FROM berriai/litellm:latest\n'))
            self.assertEqual(code, 0, (name, output))
            self.assertIn('0 manifest(s)', output)
        # Compose no longer resolves dockerfile references outside name discovery.
        self.write('build.recipe', 'RUN env pip install litellm==1.107.0\n')
        self.allowed('compose.yaml', 'services: {app: {build: {dockerfile: build.recipe}}}\n')

    def test_review_heredoc_and_shell_brackets(self):
        self.refused('Dockerfile', 'FROM python:3.12\nRUN <<EOF\npip install litellm==1.107.0\nEOF\n',
                     'Dockerfile:3:', 'above the cap')
        self.allowed('Dockerfile', 'FROM python:3.12\nRUN <<EOF\npip install litellm==1.106.2\nEOF\n')
        self.allowed('Dockerfile', 'RUN [ -f /x ] && rm /x\n')
        self.allowed('Dockerfile', "RUN apt-get update # don't cache\n")

    def test_review_compose_context_and_nested_dockerfile(self):
        self.allowed('compose.yaml', 'services: {app: {build: {context: ./app, dockerfile: Dockerfile}}}\n')
        self.allowed('app/Dockerfile', 'FROM python:3.12\nRUN [ -f /x ] && rm /x\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.write('app/Dockerfile', 'FROM python:3.12\nRUN pip install litellm==1.107.0\n')
        code, output = self.run_check()
        self.assertEqual(code, 1, output)
        self.assertIn('app/Dockerfile:2:', output)
        self.assertIn('above the cap', output)
        self.assertNotIn('compose.yaml:', output)

    def test_container_whole_token_boundaries(self):
        for name in ('Dockerfile', 'compose.yaml'):
            for text in ('langchain-litellm', 'litellm-extra', 'other_litellm',
                         'litellm.py', 'xlitellm', 'litellmx', 'LITELLM_IMAGE'):
                with self.subTest(name=name, text=text):
                    self.allowed(name, text + '\n')
            for text in ('litellm', '(litellm)', '/litellm', 'litellm[proxy]==1.106.2',
                         'litellm ==1.106.2', 'litellm===1.106.2', 'litellm==1.106.2junk',
                         'litellm==1.106.2+custom', 'litellm==1.106.2.post1'):
                with self.subTest(name=name, text=text):
                    self.refused(name, text + '\n')
            self.allowed(name, 'LiteLLM==1.106.2 litellm:v1.104.0\n')
            self.refused(name, 'litellm==1.106.2 litellm==1.107.0\n', 'above the cap')
            self.refused(name, 'other/litellm:latest\n', 'is refused')

    def test_container_version_must_end_the_word(self):
        for name in ('Dockerfile', 'compose.yaml'):
            for operand in ('"litellm==1.10"7.0', "'litellm==1.10'7.0",
                            'litellm==1.10\\7.0', '"litellm==1.10""7.0"',
                            'litellm==1.10\\\n7.0', '"litellm==1.10"${SUFFIX}',
                            '"berriai/litellm:v1.10"7.0', 'litellm==1.10`echo 7.0`'):
                with self.subTest(name=name, operand=operand):
                    self.refused(name, 'RUN pip install ' + operand + '\n', 'version token')
            for operand in ('"litellm==1.106.2"', "'litellm==1.106.2'", 'litellm==1.106.2',
                            '["litellm==1.106.2"]', '[litellm==1.106.2, 6379]',
                            'litellm==1.106.2 && echo done', '"litellm==1.106.2"; echo done',
                            '"berriai/litellm:v1.106.2"'):
                with self.subTest(name=name, operand=operand):
                    self.allowed(name, 'RUN ' + operand + '\n')

    def test_container_comments_and_numeric_compose_lists(self):
        for name in ('Dockerfile', 'compose.yaml'):
            self.allowed(name, '# litellm==1.106.2\n')
            self.refused(name, '\n# litellm\n', f'{name}:2:', 'pinned exactly')
            self.refused(name, '# litellm==1.107.0\n', 'above the cap')
        self.allowed('compose.yaml', 'services: {redis: {command: [redis-server, --port, 6379]}}\n')
        self.allowed('compose.yaml', 'services: {app: {command: [litellm==1.106.2, 6379]}}\n')
        self.refused('compose.yaml', 'services: {app: {command: [litellm==1.107.0, 6379]}}\n',
                     'above the cap')

    def test_container_name_patterns_anywhere(self):
        for name in ('deploy/Dockerfile', 'deploy/Dockerfile.prod', 'deploy/proxy.Dockerfile',
                     'deploy/compose.yaml', 'deploy/compose.prod.yml',
                     'deploy/docker-compose.yml', 'deploy/docker-compose.prod.yaml'):
            self.allowed(name, 'litellm==1.106.2\n')
            self.refused(name, 'litellm==1.107.0\n', 'above the cap')

    def test_lock_consistency_and_sources(self):
        self.write('uv.lock', '[[package]]\nname="litellm"\nversion="1.106.2"\n')
        for req in ('litellm>=1.107.0', 'litellm==1.104.0', 'litellm @ git+https://example.invalid/repo'):
            self.refused('pyproject.toml', '[project]\ndependencies = ["' + req + '"]\n')
        self.refused('pyproject.toml', '[tool.poetry.dependencies]\nlitellm={git="https://example.invalid", version="1.106.2"}\n')
        self.refused('pyproject.toml', '[tool.uv.sources]\nlitellm={path="./litellm"}\n')
        for name in ('uv.lock', 'poetry.lock'):
            for source in ('git="https://example.invalid/repo"', 'path="./litellm"', 'url="https://example.invalid/repo.whl"'):
                self.refused(name, '[[package]]\nname="litellm"\nversion="1.106.2"\nsource={' + source + '}\n')
            self.refused(name, '[[package]]\nname="litellm"\nversion="1.106.2"\n[[package]]\nname="litellm"\nversion="1.107.0"\n')
        self.refused('Pipfile.lock', '{"default":{"litellm":{"version":"==1.106.2","path":"./litellm"}}}')

    def test_setup_and_script_metadata(self):
        self.allowed('setup.cfg', '[options]\ninstall_requires =\n    litellm==1.106.2\n')
        self.refused('setup.cfg', '[options.extras_require]\nproxy =\n    litellm==1.107.0\n')
        self.refused('setup.py', 'setup(install_requires=["litellm==1.106.2"])\n')
        self.allowed('script.py', '# /// script\n# dependencies = ["litellm==1.106.2"]\n# ///\nprint("hello")\n')
        self.refused('script.py', '# /// script\n# dependencies = ["litellm"]\n# ///\n')

    def test_policy_schema_and_production_override_removal(self):
        original = tomllib.loads(POLICY.read_text())['packages']['litellm']
        for key, value in (('images', 'berriai/litellm'), ('max_version', 'latest'),
                           ('reason', ''), ('source', 123), ('approved_digests', ['sha256:bad'])):
            with self.subTest(key=key), self.assertRaises(ValueError):
                check.Cap('litellm', original | {key: value})
        with patch.dict(os.environ, {'PACKAGE_CAPS_FILE': '/tmp/relaxed-policy.toml'}), \
                patch.object(check, 'load_policy', return_value=check.load_policy(POLICY)) as load:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(check.main(['--root', str(self.repo)]), 0)
            load.assert_called_once_with(check.DEFAULT_POLICY)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            check.main(['--policy', str(POLICY)])
        self.assertEqual(error.exception.code, 2)
        wrapper = (ROOT / 'home/dot_local/bin/executable_package-cap-check').read_text()
        self.assertIn("runpy.run_path('/usr/local/lib/agent-policy/package_caps.py'", wrapper)
        self.assertNotIn('PACKAGE_CAPS_FILE', wrapper)


if __name__ == "__main__":
    unittest.main(verbosity=1)
