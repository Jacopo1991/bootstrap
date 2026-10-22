#!/usr/bin/env python3
"""Test the inventory mirror copier on temporary fixtures (no root, no mounts)."""
import contextlib
from datetime import datetime, timedelta
import importlib.util
import io
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "inventory_mirror", Path(__file__).resolve().parents[1] / "system/inventory-mirror.py")
mirror_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mirror_module)

USER = pwd.getpwuid(os.getuid()).pw_name
SNAPSHOT = b'{"schemaVersion":2,"createdAtISO":"2026-10-03T02:30:00+02:00","distros":[]}'
CREATED = datetime.fromisoformat("2026-10-03T02:30:00+02:00")
NOW = CREATED + timedelta(hours=1)


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.source = self.root / "latest.json"
        self.source.write_bytes(SNAPSHOT)
        self.latest = self.home / "project-data/inventory/latest.json"

    def tearDown(self):
        self.temp.cleanup()

    def run_mirror(self, now=NOW):
        return mirror_module.mirror(str(self.source), home=str(self.home), user=USER, now=now)

    def run_main(self, now):
        """Run main() as the unit would; return (exit code, stdout, stderr)."""
        real_geteuid = os.geteuid
        os.geteuid = lambda: 0
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = mirror_module.main(["--source-file", str(self.source)],
                                          home=str(self.home), user=USER, now=now)
        finally:
            os.geteuid = real_geteuid
        return code, out.getvalue(), err.getvalue()

    def mount_source(self, own, init=None, **patches):
        """Run mount_source() against fake ns links and a fake mount; return the mock of the mount."""
        for name, target in (("self-ns", own), ("init-ns", init)):
            if target is not None and not (self.root / name).is_symlink():
                (self.root / name).symlink_to(target)
        mount_point = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0)
        with mock.patch.object(mirror_module, "SELF_NS", str(self.root / "self-ns")), \
                mock.patch.object(mirror_module, "INIT_NS", str(self.root / "init-ns")), \
                mock.patch.object(os, "lstat", return_value=mount_point), \
                mock.patch.object(mirror_module.subprocess, "run") as run:
            try:
                mirror_module.mount_source()
            finally:
                self.mount_run = run
        return run

    def test_private_namespace_proceeds_to_mount(self):
        run = self.mount_source("mnt:[4026532500]", "mnt:[4026531841]")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][-1], mirror_module.MOUNT_POINT)

    def test_shared_namespace_refused_before_any_mount(self):
        with self.assertRaisesRegex(mirror_module.Refused, "shared mount namespace"):
            self.mount_source("mnt:[4026531841]", "mnt:[4026531841]")
        self.mount_run.assert_not_called()

    def test_unreadable_pid_one_refused_before_any_mount(self):
        # Missing link, then the kernel's ptrace refusal the unit hit without CAP_SYS_PTRACE.
        with self.assertRaisesRegex(mirror_module.Refused, "cannot read PID 1's mount namespace"):
            self.mount_source("mnt:[4026532500]")
        self.mount_run.assert_not_called()
        denied = PermissionError(13, "Permission denied", mirror_module.INIT_NS)
        with mock.patch.object(os, "readlink", side_effect=denied):
            with self.assertRaisesRegex(mirror_module.Refused, "cannot read PID 1's mount namespace.*Errno 13"):
                self.mount_source("mnt:[4026532500]")
        self.mount_run.assert_not_called()

    def test_failure_writes_one_line_last_error_that_success_removes(self):
        last_error = self.latest.parent / "last-error"
        self.source.write_bytes(b"not json SECRET")
        code, out, err = self.run_main(NOW)
        self.assertEqual((code, out, err), (1, "", "inventory-mirror: FAILED: source is not UTF-8 JSON\n"))
        self.assertEqual(last_error.read_text(), "2026-10-03T01:30:00Z source is not UTF-8 JSON\n")
        self.assertEqual(stat.S_IMODE(last_error.stat().st_mode), 0o600)
        self.assertEqual(last_error.stat().st_uid, os.getuid())
        self.assertEqual(stat.S_IMODE(last_error.parent.stat().st_mode), 0o700)
        self.assertEqual([p.name for p in last_error.parent.iterdir()], ["last-error"])
        self.source.write_bytes(SNAPSHOT)
        self.assertEqual(self.run_main(NOW), (0, "inventory-mirror: copied\n", ""))
        self.assertEqual(sorted(p.name for p in last_error.parent.iterdir()), ["latest.json"])

    def test_last_error_replaced_by_the_next_failure(self):
        self.source.unlink()
        self.run_main(NOW)
        self.run_main(NOW + timedelta(minutes=15))
        last_error = self.latest.parent / "last-error"
        self.assertEqual(last_error.read_text(), "2026-10-03T01:45:00Z Windows has not written latest.json\n")
        self.assertEqual([p.name for p in last_error.parent.iterdir()], ["last-error"])

    def run_main_mounted(self, now, unmount):
        """Run main() on the mount path with mount_source and the unmount faked."""
        real_geteuid = os.geteuid
        os.geteuid = lambda: 0
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    mock.patch.object(mirror_module, "mount_source"), \
                    mock.patch.object(mirror_module, "MOUNT_POINT", str(self.root)), \
                    mock.patch.object(mirror_module, "unmount", side_effect=unmount) as unmount_mock:
                code = mirror_module.main([], home=str(self.home), user=USER, now=now)
        finally:
            os.geteuid = real_geteuid
        unmount_mock.assert_called_once()
        return code, out.getvalue(), err.getvalue()

    def test_unmount_failure_writes_last_error_and_fails_the_run(self):
        last_error = self.latest.parent / "last-error"
        for failure, reason in (
                (OSError(5, "Input/output error"), "unmount failed: [Errno 5] Input/output error"),
                (subprocess.TimeoutExpired("umount", 60), "unmount failed: Command 'umount' timed out after 60 seconds")):
            code, out, err = self.run_main_mounted(NOW, mock.Mock(side_effect=failure))
            self.assertEqual((code, out), (1, ""))
            self.assertEqual(err, f"inventory-mirror: FAILED: {reason}\n")
            self.assertEqual(last_error.read_text(), f"2026-10-03T01:30:00Z {reason}\n")
            self.assertEqual(self.latest.read_bytes(), SNAPSHOT)  # the copy itself was published
            last_error.unlink()
        # A mirror failure and an unmount failure are both recorded in the one line.
        self.source.unlink()
        code, out, err = self.run_main_mounted(NOW, mock.Mock(side_effect=OSError(5, "Input/output error")))
        self.assertEqual(code, 1)
        self.assertEqual(last_error.read_text(), "2026-10-03T01:30:00Z Windows has not written latest.json; "
                         "unmount failed: [Errno 5] Input/output error\n")

    def test_last_error_survives_a_failed_unmount_and_goes_after_a_clean_one(self):
        last_error = self.latest.parent / "last-error"
        last_error.parent.mkdir(parents=True, mode=0o700)
        last_error.write_text("2026-10-02T00:00:00Z earlier failure\n")
        code, out, err = self.run_main_mounted(NOW, mock.Mock(side_effect=OSError(5, "Input/output error")))
        self.assertEqual(code, 1)
        self.assertIn("unmount failed", last_error.read_text())  # replaced by this run's failure, not removed
        code, out, err = self.run_main_mounted(NOW, mock.Mock(return_value=None))
        self.assertEqual((code, out, err), (0, "inventory-mirror: copied\n", ""))
        self.assertFalse(last_error.exists())

    def test_nonzero_unmount_exit_fails_the_run_and_keeps_last_error(self):
        # The real unmount() runs; only the umount binary is swapped for a command with a chosen exit status.
        real_run = subprocess.run
        last_error = self.latest.parent / "last-error"
        last_error.parent.mkdir(parents=True, mode=0o700)
        last_error.write_text("2026-10-02T00:00:00Z earlier failure\n")

        def umount_exiting(status):
            command = [sys.executable, "-c", f"raise SystemExit({status})"]
            return lambda argv, **kwargs: real_run(command, **kwargs)

        with mock.patch.object(mirror_module.subprocess, "run", umount_exiting(32)):
            code, out, err = self.run_main_mounted(NOW, mirror_module.unmount)
        self.assertEqual((code, out), (1, ""))
        self.assertRegex(err, r"^inventory-mirror: FAILED: unmount failed: Command .* returned non-zero exit status 32\.\n$")
        self.assertRegex(last_error.read_text(), r"^2026-10-03T01:30:00Z unmount failed: .* exit status 32\.\n$")
        self.assertEqual(self.latest.read_bytes(), SNAPSHOT)
        with mock.patch.object(mirror_module.subprocess, "run", umount_exiting(0)):
            code, out, err = self.run_main_mounted(NOW, mirror_module.unmount)
        self.assertEqual((code, out, err), (0, "inventory-mirror: copied\n", ""))
        self.assertFalse(last_error.exists())

    def test_non_root_run_refuses_without_touching_project_data(self):
        with mock.patch.object(os, "geteuid", return_value=1000), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = mirror_module.main(["--source-file", str(self.source)],
                                      home=str(self.home), user=USER, now=NOW)
        self.assertEqual((code, err.getvalue()), (1, "inventory-mirror: must run as root\n"))
        self.assertEqual(list(self.home.iterdir()), [])

    def test_symlinked_or_linked_last_error_refused(self):
        self.run_mirror()
        last_error = self.latest.parent / "last-error"
        outside = self.root / "outside"
        outside.write_bytes(b"unchanged")
        for plant in (lambda: last_error.symlink_to(outside), lambda: os.link(outside, last_error)):
            plant()
            self.source.unlink()
            code, out, err = self.run_main(NOW)  # failing run: the error cannot be written
            self.assertEqual(code, 1)
            self.assertEqual(err.splitlines()[0], "inventory-mirror: FAILED: Windows has not written latest.json")
            self.assertIn("could not write last-error: existing last-error is not a plain single-link file", err)
            self.source.write_bytes(SNAPSHOT)
            code, out, err = self.run_main(NOW)  # successful copy: the link is not followed or removed
            self.assertEqual(code, 1)
            self.assertIn("existing last-error is not a plain single-link file", err)
            self.assertTrue(last_error.is_symlink() or last_error.stat().st_nlink == 2)
            self.assertEqual(outside.read_bytes(), b"unchanged")
            last_error.unlink()

    def test_exact_bytes_owner_and_modes(self):
        old = os.umask(0o002)
        try:
            self.run_mirror()
        finally:
            os.umask(old)
        self.assertEqual(self.latest.read_bytes(), SNAPSHOT)
        self.assertEqual(self.latest.stat().st_uid, os.getuid())
        self.assertEqual(stat.S_IMODE(self.latest.stat().st_mode), 0o600)
        for directory in (self.home / "project-data", self.latest.parent):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(directory.stat().st_uid, os.getuid())
        self.assertEqual([p.name for p in self.latest.parent.iterdir()], ["latest.json"])

    def test_replace_is_atomic_and_leaves_no_staging_file(self):
        self.assertIsNone(self.run_mirror())
        newer = SNAPSHOT.replace(b"02:30", b"03:30")
        self.source.write_bytes(newer)
        inode = self.latest.stat().st_ino
        self.run_mirror()
        self.assertEqual(self.latest.read_bytes(), newer)
        self.assertNotEqual(self.latest.stat().st_ino, inode)  # renamed in, never rewritten
        self.assertEqual([p.name for p in self.latest.parent.iterdir()], ["latest.json"])

    def test_missing_source_fails_with_one_line(self):
        self.source.unlink()
        with self.assertRaises(mirror_module.Refused):
            self.run_mirror()
        self.assertFalse((self.home / "project-data").exists())
        code, out, err = self.run_main(NOW)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, "inventory-mirror: FAILED: Windows has not written latest.json\n")

    def test_stale_source_fails_with_one_line_and_keeps_previous_copy(self):
        self.run_mirror()
        self.source.write_bytes(SNAPSHOT.replace(b"02:30", b"03:30"))
        later = CREATED + timedelta(hours=37, seconds=1)  # the new source is an hour newer
        with self.assertRaises(mirror_module.Refused):
            self.run_mirror(now=later)
        self.assertEqual(self.latest.read_bytes(), SNAPSHOT)
        code, out, err = self.run_main(CREATED + timedelta(hours=40))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err.count("\n"), 1)
        self.assertTrue(err.startswith("inventory-mirror: FAILED: latest.json is stale: "), err)
        self.assertIn("39.0 hours old (limit 36)", err)

    def test_fresh_source_succeeds(self):
        # Windows writes .NET round-trip timestamps with seven fractional digits.
        self.source.write_bytes(SNAPSHOT.replace(b"02:30:00+", b"02:30:00.1234567+"))
        code, out, err = self.run_main(CREATED + timedelta(hours=35))
        self.assertEqual((code, out, err), (0, "inventory-mirror: copied\n", ""))
        self.assertTrue(self.latest.exists())
        self.latest.unlink()
        self.run_mirror(now=CREATED + timedelta(hours=36))  # exactly at the limit is still fresh
        self.assertTrue(self.latest.exists())

    def test_source_symlink_hardlink_fifo_and_directory_refused(self):
        real = self.root / "real.json"
        real.write_bytes(SNAPSHOT)
        self.source.unlink()
        self.source.symlink_to(real)
        with self.assertRaises(mirror_module.Refused):
            self.run_mirror()
        self.source.unlink()
        os.link(real, self.source)
        with self.assertRaises(mirror_module.Refused):
            self.run_mirror()
        self.source.unlink()
        os.mkfifo(self.source, 0o600)
        with self.assertRaises(mirror_module.Refused):
            self.run_mirror()
        self.source.unlink()
        self.source.mkdir()
        with self.assertRaises(mirror_module.Refused):
            self.run_mirror()
        self.assertFalse(self.latest.exists())

    def test_destination_symlink_hardlink_and_fifo_refused(self):
        self.run_mirror()
        outside = self.root / "outside"
        outside.write_bytes(b"unchanged")
        for plant in (lambda: self.latest.symlink_to(outside),
                      lambda: os.link(outside, self.latest),
                      lambda: os.mkfifo(self.latest, 0o600)):
            self.latest.unlink()
            plant()
            with self.assertRaises(mirror_module.Refused):
                self.run_mirror()
            self.assertEqual(outside.read_bytes(), b"unchanged")
            self.assertEqual([p.name for p in self.latest.parent.iterdir()], ["latest.json"])

    def test_symlinked_project_data_inventory_and_home_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.home / "project-data").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(mirror_module.Refused):
            self.run_mirror()
        (self.home / "project-data").unlink()
        (self.home / "project-data").mkdir(mode=0o700)
        (self.home / "project-data/inventory").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(mirror_module.Refused):
            self.run_mirror()
        alias = self.root / "alias"
        alias.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(OSError):
            mirror_module.mirror(str(self.source), home=str(alias), user=USER, now=NOW)
        self.assertEqual(list(outside.iterdir()), [])

    def test_wrong_owner_refused(self):
        self.run_mirror()
        real_getpwnam = pwd.getpwnam
        other = SimpleNamespace(pw_uid=os.getuid() + 1, pw_gid=os.getgid())
        pwd.getpwnam = lambda name: other
        try:
            with self.assertRaises(mirror_module.Refused):
                self.run_mirror()
        finally:
            pwd.getpwnam = real_getpwnam

    def test_non_inventory_and_oversized_source_refused(self):
        for data in (b"not json", b"\xff\xfe", b'{"schemaVersion":3,"distros":[]}',
                     b'{"schemaVersion":true,"distros":[]}', b'{"schemaVersion":2}', b"[]",
                     b"\xef\xbb\xbf" + SNAPSHOT, b'{"schemaVersion":2,"distros":[]}',
                     b'{"schemaVersion":2,"createdAtISO":"yesterday","distros":[]}',
                     b'{"schemaVersion":2,"createdAtISO":"2026-10-03T02:30:00","distros":[]}'):
            self.source.write_bytes(data)
            with self.assertRaises(mirror_module.Refused, msg=data):
                self.run_mirror()
        self.source.write_bytes(SNAPSHOT + b" " * mirror_module.MAX_BYTES)
        with self.assertRaises(mirror_module.Refused):
            self.run_mirror()
        self.assertFalse(self.latest.exists())


if __name__ == "__main__":
    unittest.main()
