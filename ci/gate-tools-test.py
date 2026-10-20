"""Offline lookup and fail-closed socket policy regression checks."""
import contextlib
import errno
import hashlib
import io
import os
from pathlib import Path
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import tarfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class GateToolsTest(unittest.TestCase):
    def test_socket_directory_respects_tmpdir_and_byte_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            python = base / 'python3'
            python.write_text('#!/bin/sh\nprintf "fixture: %s\\n" "$2"\nexit 77\n')
            python.chmod(0o755)
            for name in ('short', 'a' * 90, 'é' * 45):
                with self.subTest(name=name):
                    temporary = base / name
                    temporary.mkdir()
                    env = dict(os.environ, PATH=directory + ':' + os.environ['PATH'],
                               TMPDIR=str(temporary), TASK_GATE_RUN_ID='')
                    result = subprocess.run(['bash', 'ci/boundary-test.sh'], cwd=ROOT,
                                            env=env, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 77, result.stderr)
                    fixture = next(line.removeprefix('fixture: ') for line in
                                   result.stdout.splitlines() if line.startswith('fixture: '))
                    self.assertLess(len(os.fsencode(fixture)), 108)
                    template = temporary / 's.XXXXXXXX/WSL/123_interop'
                    expected = (temporary if len(os.fsencode(template)) < 108
                                else Path(f'/tmp/task-gate-{os.getuid()}'))
                    self.assertEqual(Path(fixture).parents[2], expected)

    def test_boundary_skip_reaches_lint_summary(self):
        lint = (ROOT / 'ci/lint.sh').read_text()
        runner = lint.split('failed=0\n', 1)[1].split('run bash ci/gate-tools.sh', 1)[0]
        summary = lint[lint.index('echo "Gate suites finished'):]
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory) / 'python3'
            # Force 77 even in trusted mode to test the runner's own policy.
            python.write_text('#!/bin/sh\nexit 77\n')
            python.chmod(0o755)
            for run_id in (None, '', 'test'):
                with self.subTest(run_id=run_id):
                    env = dict(os.environ, PATH=directory + ':' + os.environ['PATH'])
                    env.pop('TASK_GATE_RUN_ID', None)
                    if run_id is not None:
                        env['TASK_GATE_RUN_ID'] = run_id
                    result = subprocess.run(
                        (['env', '-i', 'PATH=' + env['PATH'], 'TASK_GATE_RUN_ID=',
                          'TMPDIR=' + tempfile.gettempdir()]
                         if run_id == '' else []) +
                        ['bash', '-c', 'set -euo pipefail\nfailed=0\n' + runner +
                         'run bash ci/boundary-test.sh\nrun true\n' + summary],
                        cwd=ROOT, env=env, text=True, capture_output=True)
                    self.assertEqual(result.returncode, int(bool(run_id)), result.stderr)
                    self.assertIn('PASS: suite true', result.stderr)
                    self.assertNotIn('PASS: suite bash ci/boundary-test.sh', result.stderr)
                    if run_id:
                        self.assertIn('FAIL: bash ci/boundary-test.sh (exit 77)', result.stderr)
                        self.assertIn('failed=1, skipped=0', result.stdout)
                    else:
                        self.assertIn('SKIP: suite bash ci/boundary-test.sh', result.stderr)
                        self.assertIn('failed=0, skipped=1', result.stdout)

                    # An unrelated suite cannot skip, even in a lane.
                    result = subprocess.run(
                        ['bash', '-c', 'set -euo pipefail\nfailed=0\n' + runner +
                         "run bash -c 'exit 77'\nrun true\n" + summary],
                        cwd=ROOT, env=env, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn('FAIL: bash -c exit 77 (exit 77)', result.stderr)
                    self.assertNotIn('SKIP: suite', result.stderr)
                    self.assertIn('PASS: suite true', result.stderr)
                    self.assertIn('failed=1, skipped=0', result.stdout)

    def test_summary_counts_each_failed_and_skipped_suite(self):
        lint = (ROOT / 'ci/lint.sh').read_text()
        runner = lint.split('failed=0\n', 1)[1].split('run bash ci/gate-tools.sh', 1)[0]
        summary = lint[lint.index('echo "Gate suites finished'):]
        with tempfile.TemporaryDirectory() as directory:
            for name in ('bash', 'python3'):
                stub = Path(directory) / name
                stub.write_text('#!/bin/sh\nexit 77\n')
                stub.chmod(0o755)
            env = dict(os.environ, PATH=directory + ':' + os.environ['PATH'],
                       TASK_GATE_RUN_ID='')
            result = subprocess.run(
                ['/bin/bash', '-c', 'set -euo pipefail\nfailed=0\n' + runner +
                 'run false\nrun bash failing-suite\n'
                 'run bash ci/boundary-test.sh\nrun python3 ci/gate-browser-test.py\n'
                 'run true\n' + summary],
                cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(sum(line.startswith('FAIL: ') for line in
                                 result.stderr.splitlines()), 2)
            self.assertEqual(sum(line.startswith('SKIP: suite ') for line in
                                 result.stderr.splitlines()), 2)
            self.assertIn('PASS: suite true', result.stderr)
            self.assertIn('failed=2, skipped=2', result.stdout)

    def test_pinned_download_and_failures(self):
        # Exercise real checksum verification and extraction with local transport.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / 'repo'
            for name in ('ci/lint-install.sh', 'system/common.sh',
                         'home/.chezmoitemplates/pins.env'):
                target = repo / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, target)
            binary = base / 'shellcheck'
            binary.write_text('#!/bin/sh\necho "version: 0.11.0"\n')
            binary.chmod(0o755)
            archive = base / 'archive.tar.xz'
            with tarfile.open(archive, 'w:xz') as bundle:
                bundle.add(binary, arcname='shellcheck-v0.11.0/shellcheck')
            pins = repo / 'home/.chezmoitemplates/pins.env'
            pins.write_text(pins.read_text() + '\nSHELLCHECK_SHA256=' +
                            hashlib.sha256(archive.read_bytes()).hexdigest() + '\n')
            transport = base / 'bin'
            transport.mkdir()
            curl = transport / 'curl'
            curl.write_text('#!/bin/bash\ncp ' + shlex.quote(str(archive)) + ' "${@: -1}"\n')
            curl.chmod(0o755)
            env = dict(os.environ, PATH=str(transport) + ':' + os.environ['PATH'])

            def install(label):
                return subprocess.run(['bash', str(repo / 'ci/lint-install.sh'),
                                       str(base / label), 'shellcheck'],
                                      env=env, text=True, capture_output=True)

            result = install('good')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('0.11.0', subprocess.check_output(
                [str(base / 'good/gate-bin/shellcheck'), '--version'], text=True))
            archive.write_bytes(b'corrupted download')
            self.assertNotEqual(install('corrupt').returncode, 0)
            self.assertFalse((base / 'corrupt/gate-bin/shellcheck').exists())
            curl.write_text('#!/bin/sh\nexit 6\n')
            self.assertNotEqual(install('offline').returncode, 0)
            self.assertFalse((base / 'offline/gate-bin/shellcheck').exists())

    def test_offline_lookup(self):
        pins = dict(line.split("=", 1) for line in
                    (ROOT / "home/.chezmoitemplates/pins.env").read_text().splitlines())
        pins = {key: shlex.split(value)[0] for key, value in pins.items()}

        def version(key):
            return pins[key].split("/download/")[1].split("/")[0].removeprefix("v")

        requirement = (ROOT / "home/dot_local/share/bootstrap/pre-commit/requirements.in").read_text()
        pre_commit = next(line.split("==")[1] for line in requirement.splitlines()
                          if line.startswith("pre-commit=="))
        versions = {
            "shellcheck": "version: " + version("SHELLCHECK_URL"),
            "gitleaks": version("GITLEAKS_URL"),
            "lychee": "lychee " + version("LYCHEE_URL").removeprefix("lychee-v"),
            "osv-scanner": "osv-scanner version: " + version("OSV_SCANNER_URL"),
            "chezmoi": "chezmoi version v" + pins["CHEZMOI_VERSION"],
            "pre-commit": "pre-commit " + pre_commit,
        }
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ("dirname", "sed"):
                (folder / name).symlink_to("/usr/bin/" + name)
            for name, output in versions.items():
                target = folder / name
                target.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(output) + "\n")
                target.chmod(0o755)
            # Only the version fixtures and local text utilities are reachable:
            # curl, wget, pip, uv and mise cannot be called, even as fallbacks.
            def check():
                return subprocess.run(["/bin/bash", str(ROOT / "ci/gate-tools.sh")],
                                      env={"PATH": directory}, text=True, capture_output=True)
            result = check()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for name in versions:
                target = folder / name
                original = target.read_text()
                target.write_text("#!/bin/sh\necho wrong-version\n")
                self.assertNotEqual(check().returncode, 0, name)
                target.unlink()
                result = check()
                self.assertNotEqual(result.returncode, 0, name)
                self.assertIn("missing " + name, result.stderr)
                target.write_text(original)
                target.chmod(0o755)

    def test_socket_denial_is_never_skipped_in_trusted_gate(self):
        for run_id in (None, '', 'test'):
            trusted = bool(run_id)
            for code in (errno.EPERM, errno.EACCES, errno.ENOENT):
                with self.subTest(run_id=run_id, code=code):
                    environment = {"TASK_GATE_RUN_ID": run_id} if run_id is not None else {}
                    with patch.dict(os.environ, environment, clear=True), \
                         patch.object(sys, "argv", ["boundary-sockets.py", "fixture"]), \
                         patch("socket.socket", side_effect=OSError(code, "fixture denial")), \
                         contextlib.redirect_stdout(io.StringIO()) as output:
                        if not trusted and code in (errno.EPERM, errno.EACCES):
                            with self.assertRaises(SystemExit) as result:
                                runpy.run_path(str(ROOT / "ci/boundary-sockets.py"))
                            self.assertEqual(result.exception.code, 77)
                            self.assertIn("SKIP (sandbox:", output.getvalue())
                        else:
                            with self.assertRaises(OSError):
                                runpy.run_path(str(ROOT / "ci/boundary-sockets.py"))


if __name__ == "__main__":
    unittest.main()
