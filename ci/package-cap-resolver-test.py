#!/usr/bin/env python3
"""Real pinned resolvers, offline synthetic wheels, isolated disposable state.

No litellm downloads/installs, real virtual environments, repositories or Docker.
The parent wheel proves transitive resolution, rather than just a CLI name check.
"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('resolver_caps', ROOT / 'system/agent-policy/package_caps.py')
caps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(caps)


class ResolverCaps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='package-cap-resolver-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.wheels = self.base / 'wheels'
        self.wheels.mkdir()
        self.uv, self.pip, self.uvx = (shutil.which(name) for name in ('uv', 'pip', 'uvx'))
        self.assertTrue(self.uv and self.pip and self.uvx, 'bootstrap pinned pip/uv/uvx must be installed')
        self.env = {'PATH': os.environ['PATH'], 'HOME': str(self.base / 'home'),
                    'XDG_CONFIG_HOME': str(self.base / 'config'), 'XDG_CONFIG_DIRS': str(self.base / 'system-config'),
                    'UV_CACHE_DIR': str(self.base / 'cache'), 'UV_TOOL_DIR': str(self.base / 'tools'),
                    'UV_TOOL_BIN_DIR': str(self.base / 'bin'), 'UV_PYTHON_DOWNLOADS': 'never',
                    'PIP_DISABLE_PIP_VERSION_CHECK': '1'}
        for name, version, requires in [('bootstrap_cap_probe', '2.0', ''),
                                        ('bootstrap_cap_parent', '1.0', 'Requires-Dist: bootstrap-cap-probe==2.0\n')]:
            info = f'{name}-{version}.dist-info'
            with zipfile.ZipFile(self.wheels / f'{name}-{version}-py3-none-any.whl', 'w') as wheel:
                wheel.writestr(f'{info}/METADATA', f'Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n{requires}')
                wheel.writestr(f'{info}/WHEEL', 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
                wheel.writestr(f'{info}/RECORD', '')
        self.policy = self.base / 'package-caps.toml'
        self.policy.write_text('[packages.bootstrap-cap-probe]\nmax_version="1.0"\nreason="regression fixture"\nsource="https://example.invalid"\n')
        self.constraint = self.base / 'constraints.txt'
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(caps.main(['--emit-constraints'], policy_path=self.policy), 0)
        self.assertEqual(output.getvalue(), 'bootstrap-cap-probe<=1.0\n')
        self.constraint.write_text(output.getvalue())
        self.common = ['--offline', '--no-index', '--no-build', '--find-links', str(self.wheels)]

    def run_command(self, command):
        return subprocess.run(command, cwd=self.base, env=self.env, text=True, capture_output=True, timeout=60)

    def assert_conflict(self, command):
        result = self.run_command(command)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, (command, output))
        self.assertIn('bootstrap-cap-probe', output)
        self.assertIn('<=1.0', output)
        self.assertTrue('unsatisfiable' in output or 'conflicting dependencies' in output, output)

    def test_installed_versions(self):
        self.assertIn('uv 0.12.21', self.run_command([self.uv, '--version']).stdout)
        self.assertIn('pip 26.2.1', self.run_command([self.pip, '--version']).stdout)

    def test_pip_config_and_uv_environment_all_install_paths(self):
        # Test the real managed keys, preserving an unrelated pip cooldown key.
        modify = subprocess.run(['/usr/bin/python3', str(ROOT / 'home/dot_config/pip/modify_pip.conf')],
                                input='[global]\ntimeout = 42\n', text=True, capture_output=True, check=True)
        self.assertIn('timeout = 42', modify.stdout)
        self.assertIn('build-constraint = /usr/local/lib/agent-policy/constraints.txt', modify.stdout)
        config = self.base / 'config/pip/pip.conf'
        config.parent.mkdir(parents=True)
        config.write_text(modify.stdout.replace('/usr/local/lib/agent-policy/constraints.txt', str(self.constraint)))
        profile = (ROOT / 'home/dot_profile').read_text()
        for key in ('UV_CONSTRAINT', 'UV_BUILD_CONSTRAINT'):
            self.assertIn(f'export {key}=/usr/local/lib/agent-policy/constraints.txt', profile)
            self.env[key] = str(self.constraint)
        for requirement in ('bootstrap-cap-probe==2.0', 'bootstrap-cap-parent==1.0'):
            requirements = self.base / 'requirements.txt'
            requirements.write_text(requirement + '\n')
            commands = [
                [self.pip, 'install', '--dry-run', '--ignore-installed', '--no-index', '--only-binary=:all:', '--find-links', str(self.wheels), requirement],
                [self.uv, 'pip', 'install', '--dry-run', '--python', sys.executable, *self.common, requirement],
                [self.uv, 'pip', 'compile', *self.common, str(requirements)],
                [self.uv, 'tool', 'install', '--python', sys.executable, *self.common, requirement],
                [self.uvx, '--python', sys.executable, *self.common, '--from', requirement, 'bootstrap-cap-parent'],
            ]
            for command in commands:
                with self.subTest(command=command[:3], requirement=requirement):
                    self.assert_conflict(command)

    def test_new_project_metadata_covers_lock_sync_run(self):
        project = self.base / 'pyproject.toml'
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(caps.main(['--uv-project', 'cap-fixture'], policy_path=self.policy), 0)
        # The exact generator output is a standalone project uv accepts. No
        # pre-existing [project] table is supplied by the test or left to uv init.
        project.write_text(output.getvalue())
        standalone = self.run_command([self.uv, 'lock', '--dry-run', *self.common])
        self.assertEqual(standalone.returncode, 0, standalone.stderr)
        self.assertIn('name = "cap-fixture"', project.read_text())
        project.write_text(output.getvalue().replace('dependencies = []',
                           'dependencies = ["bootstrap-cap-parent==1.0"]'))
        constrained = project.read_text()
        # Control: exactly this project without its generated workspace caps.
        project.write_text(constrained.split('[tool.uv]')[0])
        control = self.run_command([self.uv, 'lock', '--dry-run', *self.common])
        self.assertEqual(control.returncode, 0, control.stderr)
        self.assertIn('bootstrap-cap-probe v2.0', control.stderr)
        project.write_text(constrained)
        script = (ROOT / 'home/dot_local/bin/executable_new-project').read_text()
        self.assertIn('uv_project=$(package-cap-check --uv-project "$name")', script)
        self.assertIn('printf \'%s\\n\' "$uv_project" > "$code/pyproject.toml"', script)
        for command in ([self.uv, 'lock', '--dry-run', *self.common],
                        [self.uv, 'sync', '--dry-run', *self.common],
                        [self.uv, 'run', *self.common, '--', sys.executable, '--version']):
            with self.subTest(command=command[:2]):
                self.assert_conflict(command)

    def test_new_project_generator_failure_leaves_no_directories(self):
        # Fail before git init, creating only disposable command stubs, never repos.
        bins = self.base / 'stubs'
        bins.mkdir()
        for name in ('pre-commit', 'gitleaks', 'lychee', 'osv-scanner', 'package-cap-check'):
            stub = bins / name
            stub.write_text('#!/bin/sh\nexit ' + ('13' if name == 'package-cap-check' else '0') + '\n')
            stub.chmod(0o755)
        self.env['PATH'] = str(bins) + os.pathsep + self.env['PATH']
        result = self.run_command(['bash', str(ROOT / 'home/dot_local/bin/executable_new-project'),
                                   'create', 'cap-fixture', '--local-only'])
        self.assertEqual(result.returncode, 13, result.stdout + result.stderr)
        self.assertFalse((Path(self.env['HOME']) / 'dev_workspace/cap-fixture').exists())
        self.assertFalse((Path(self.env['HOME']) / 'cortex/cortex-kb-cap-fixture').exists())


if __name__ == '__main__':
    unittest.main()
