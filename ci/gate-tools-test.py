"""Offline lookup and fail-closed socket policy regression checks."""
import contextlib
import errno
import io
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class GateToolsTest(unittest.TestCase):
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
        for trusted in (False, True):
            for code in (errno.EPERM, errno.EACCES, errno.ENOENT):
                with self.subTest(trusted=trusted, code=code):
                    environment = {"TASK_GATE_RUN_ID": "test"} if trusted else {}
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
