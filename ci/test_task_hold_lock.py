import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / 'home/dot_local/bin/task_hold_lock.py'
spec = importlib.util.spec_from_file_location('task_hold_lock', SOURCE)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class MarkerLockTests(unittest.TestCase):
    def test_metadata_changed_while_waiting_is_rejected_before_yield(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'agent-hold'
            lock = marker.with_name(marker.name + '.lock')
            lock.touch(mode=0o600)
            real_flock = helper.fcntl.flock

            def change_after_acquisition(fd, operation):
                real_flock(fd, operation)
                os.chmod(lock, 0o644)

            entered = False
            with mock.patch.object(helper.fcntl, 'flock', side_effect=change_after_acquisition):
                with self.assertRaises(OSError):
                    with helper.marker_lock(marker, exclusive=True):
                        entered = True
            self.assertFalse(entered)


if __name__ == '__main__':
    unittest.main()
