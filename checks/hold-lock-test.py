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
    run = lambda *a, e=env: subprocess.run([sys.executable, str(command), *a], env=e, capture_output=True, text=True)
    # Jacopo's pull never depends on agents: lock held by someone else beyond the bound -> pull anyway (break-glass).
    with lock.open('r') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = run('pull', 'blocked')
        assert result.returncode == 0 and marker.exists() and 'break-glass' in result.stderr, result
        # Release under a blocked lock keeps the hold (fails safe); --force is Jacopo's override.
        result = run('release')
        assert result.returncode != 0 and marker.exists(), result
        result = run('release', '--force')
        assert result.returncode == 0 and not marker.exists(), result
    # Lock tampered with (agent-owned file chmod 0644) -> pull still works.
    lock.chmod(0o644)
    result = run('pull', 'tampered')
    assert result.returncode == 0 and marker.exists() and 'break-glass' in result.stderr, result
    run('release', '--force'); lock.chmod(0o600)
    # Lock file missing and lock helper not importable -> pull still works; the reader fails closed.
    lock.unlink()
    result = run('pull', 'no-lock', e={'PATH': '/usr/bin:/bin'})
    assert result.returncode == 0 and marker.exists() and 'break-glass' in result.stderr, result
    result = run(e={'PATH': '/usr/bin:/bin'})
    assert result.returncode != 0 and 'held / cannot inspect' in result.stderr, result
    run('release', '--force')
    assert not marker.exists() and not lock.exists()
print('hold lock: PASS')
