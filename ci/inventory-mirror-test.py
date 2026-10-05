#!/usr/bin/env python3
"""Test the inventory mirror copier on temporary fixtures (no root, no mounts)."""
import importlib.util
import os
from pathlib import Path
import pwd
import stat
import tempfile
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location(
    "inventory_mirror", Path(__file__).resolve().parents[1] / "system/inventory-mirror.py")
mirror_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mirror_module)

USER = pwd.getpwuid(os.getuid()).pw_name
SNAPSHOT = b'{"schemaVersion":2,"createdAtISO":"2026-10-03T02:30:00+02:00","distros":[]}'


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

    def run_mirror(self):
        return mirror_module.mirror(str(self.source), home=str(self.home), user=USER)

    def test_exact_bytes_owner_and_modes(self):
        old = os.umask(0o002)
        try:
            self.assertTrue(self.run_mirror())
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
        self.run_mirror()
        newer = SNAPSHOT.replace(b"02:30", b"03:30")
        self.source.write_bytes(newer)
        inode = self.latest.stat().st_ino
        self.run_mirror()
        self.assertEqual(self.latest.read_bytes(), newer)
        self.assertNotEqual(self.latest.stat().st_ino, inode)  # renamed in, never rewritten
        self.assertEqual([p.name for p in self.latest.parent.iterdir()], ["latest.json"])

    def test_missing_source_is_not_an_error(self):
        self.source.unlink()
        self.assertFalse(self.run_mirror())
        self.assertFalse((self.home / "project-data").exists())

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
            mirror_module.mirror(str(self.source), home=str(alias), user=USER)
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
                     b"\xef\xbb\xbf" + SNAPSHOT):
            self.source.write_bytes(data)
            with self.assertRaises(mirror_module.Refused, msg=data):
                self.run_mirror()
        self.source.write_bytes(SNAPSHOT + b" " * mirror_module.MAX_BYTES)
        with self.assertRaises(mirror_module.Refused):
            self.run_mirror()
        self.assertFalse(self.latest.exists())


if __name__ == "__main__":
    unittest.main()
