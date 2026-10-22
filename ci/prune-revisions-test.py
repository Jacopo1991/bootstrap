#!/usr/bin/env python3
"""Test revision pruning on temporary directories only."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "prune_revisions", Path(__file__).resolve().parents[1] / "system/prune-revisions.py")
PRUNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PRUNER)


class PruneRevisionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.revisions = self.root / "revisions"
        self.revisions.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def revision(self, index):
        path = self.revisions / (f"{index:040x}")
        path.mkdir()
        (path / "payload").write_text(str(index))
        return path

    def set_current(self, target):
        (self.root / "current").symlink_to(target, target_is_directory=True)

    def test_keeps_current_and_five_newest_other_revisions(self):
        paths = [self.revision(i) for i in range(8)]
        for index, path in enumerate(paths):
            os.utime(path, (100 + index, 100 + index))
        self.set_current(paths[0])
        PRUNER.prune(self.revisions, 5)
        self.assertEqual({path.name for path in self.revisions.iterdir()},
                         {paths[index].name for index in (0, 3, 4, 5, 6, 7)})

    def test_keeps_symlink_and_non_hex_name(self):
        paths = [self.revision(i) for i in range(7)]
        self.set_current(paths[0])
        outside = self.root / "outside"
        outside.mkdir()
        (self.revisions / ("f" * 40)).symlink_to(outside, target_is_directory=True)
        odd = self.revisions / "not-a-revision"
        odd.mkdir()
        PRUNER.prune(self.revisions, 2)
        self.assertTrue((self.revisions / ("f" * 40)).is_symlink())
        self.assertTrue(odd.is_dir())

    def test_unresolvable_current_removes_nothing(self):
        paths = [self.revision(i) for i in range(8)]
        (self.root / "current").symlink_to(self.root / "missing", target_is_directory=True)
        before = {path.name for path in self.revisions.iterdir()}
        PRUNER.prune(self.revisions, 1)
        self.assertEqual({path.name for path in self.revisions.iterdir()}, before)

    def test_keep_count_larger_than_revision_count_removes_nothing(self):
        paths = [self.revision(i) for i in range(3)]
        self.set_current(paths[0])
        PRUNER.prune(self.revisions, 5)
        self.assertEqual({path.name for path in self.revisions.iterdir()},
                         {path.name for path in paths})


if __name__ == "__main__":
    unittest.main()
