#!/usr/bin/env python3
"""Exercise actual marker commands under a temporary owner lock."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as temporary:
    directory = Path(temporary)
    marker = directory / 'hold'
    lock = directory / 'hold.lock'
    lock.touch(mode=0o600)
    source = (root / 'system/agent-hold').read_text().replace('Path("/etc/agent-hold")', f'Path({str(marker)!r})')
    # Simulate the owner only; filesystem state and flock are real.
    source = source.replace('os.geteuid()', '0').replace('os.fchown(stream.fileno(), 0, 0)', 'None')
    command = directory / 'owner.py'
    command.write_text(source)
    env = {'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(root / 'home/dot_local/bin')}
    with lock.open('r') as held:
        fcntl.flock(held, fcntl.LOCK_SH)
        writer = subprocess.Popen([sys.executable, str(command), 'pull', 'synthetic'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(.3)
        if writer.poll() is not None:
            raise AssertionError(writer.communicate())
        assert not marker.exists(), 'writer bypassed shared lock'
        fcntl.flock(held, fcntl.LOCK_UN)
        out, err = writer.communicate(timeout=6)
        assert writer.returncode == 0, (out, err)
    with lock.open('r') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        reader = subprocess.Popen([sys.executable, str(command)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(.3)
        assert reader.poll() is None, 'reader bypassed exclusive lock'
        fcntl.flock(held, fcntl.LOCK_UN)
        out, err = reader.communicate(timeout=6)
        assert reader.returncode == 0 and 'synthetic' in out, (out, err)
    subprocess.run([sys.executable, str(command), 'release'], env=env, check=True, capture_output=True)
    assert not marker.exists() and lock.exists()
    lock.unlink()
    result = subprocess.run([sys.executable, str(command), 'pull', 'synthetic'], env=env, capture_output=True)
    assert result.returncode != 0 and not marker.exists() and not lock.exists()
print('hold lock: PASS')
