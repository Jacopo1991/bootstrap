#!/bin/bash
# Owner-only, ONE supervised run after freezing launchers and draining all work.
# Usage: bash qualify-live.sh --idle-confirmed
# Requires installed bootstrap commands and the reviewed layer checkout below.
# This is a lock qualification, not authorization to admit production work.
set -euo pipefail
[[ $# == 1 && $1 == --idle-confirmed && $EUID != 0 && $(id -un) != agent ]] || {
  echo 'FAIL preflight: run as the non-root admin with --idle-confirmed after draining work.' >&2
  exit 1
}
sudo -v
# Elevate this fixed probe; never elevate the agent-owned layer implementation.
sudo -n /usr/bin/python3 -I - <<'PY'
import fcntl
import hashlib
import os
from pathlib import Path
import pwd
import runpy
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
MARKER = '/etc/agent-hold'
LOCK = MARKER + '.lock'
OWNER = '/usr/local/bin/agent-hold'
CORE = '/usr/local/lib/studio-layer/current/core'
children = set()
armed = False
initial_identity = None
held_fd = None
checks = {name: False for name in ('a-break-glass', 'b-reader-wait', 'c-cleanup')}


def passed(name):
    checks[name] = True
    print('PASS ' + name, flush=True)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def protected(path):
    # Do not execute a writable/symlinked installed owner command as root.
    p = Path(path)
    for part in (p, *p.parents):
        s = part.lstat()
        require(not stat.S_ISLNK(s.st_mode) and s.st_uid == 0
                and not s.st_mode & 0o022, 'unsafe installed owner path')


def spawn(function):
    pid = os.fork()
    if pid == 0:
        # Fork must not inherit the parent's lock and accidentally retain it.
        if held_fd is not None:
            os.close(held_fd)
        try:
            function()
        except BaseException:
            os._exit(1)
        os._exit(0)
    children.add(pid)
    return pid


def status(pid):
    got, value = os.waitpid(pid, os.WNOHANG)
    if got:
        children.discard(pid)
        return os.waitstatus_to_exitcode(value)
    return None


def wait(pid, seconds=5.5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = status(pid)
        if value is not None:
            return value
        time.sleep(0.01)
    raise RuntimeError('owned probe process exceeded bound')


def owner(action, reason=None):
    argv = [OWNER, action] + ([] if reason is None else [reason])
    # Output can contain marker text; keep it out of this evidence stream.
    result = subprocess.run(argv, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=6)
    return result.returncode


def signal_byte(fd, expected, seconds=4):
    require(bool(select.select([fd], [], [], seconds)[0]), 'handshake timed out')
    require(os.read(fd, 1) == expected, 'handshake failed')


def release_owned_children():
    # These are only our unreaped children, whose PIDs cannot yet be reused.
    for pid in list(children):
        if status(pid) is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            children.discard(pid)


def cleanup():
    global held_fd
    release_owned_children()
    if held_fd is not None:
        os.close(held_fd)
        held_fd = None
    if not armed:
        return
    # Never unlink the marker ourselves; the real owner release must take flock.
    code = owner('release')
    s = os.lstat(LOCK)
    require(code == 0 and not os.path.lexists(MARKER),
            'owner release failed: Jacopo must run sudo agent-hold release')
    require((s.st_dev, s.st_ino) == initial_identity, 'lock identity changed')
    require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1
            and stat.S_IMODE(s.st_mode) == 0o600, 'lock metadata changed')
    passed('c-cleanup')


def interrupted(signum, frame):
    raise RuntimeError('interrupted')


for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(sig, interrupted)

try:
    require(os.geteuid() == 0, 'admin elevation required')
    protected(OWNER)
    protected('/usr/local/bin/task_hold_lock.py')
    sys.path.insert(0, '/usr/local/bin')
    from task_hold_lock import marker_lock
    agent = pwd.getpwnam('agent')
    # This also validates parents, links, mode and the deployed helper's policy.
    with marker_lock(MARKER, exclusive=True):
        s = os.lstat(LOCK)
        initial_identity = (s.st_dev, s.st_ino)
        require(not os.path.lexists(MARKER),
                'pre-existing hold: preserved; owner must choose when to retry')
    for path in (OWNER, '/usr/local/bin/task_hold_lock.py',
                 CORE + '/safe_core.py', CORE + '/layer_core.py'):
        print('SOURCE ' + path + ' sha256=' + hashlib.sha256(Path(path).read_bytes()).hexdigest(), flush=True)
    print('LOCK dev=%s inode=%s; reader uid=%s' % (*initial_identity, agent.pw_uid), flush=True)
    armed = True

    # Break-glass must pull even after a five-second lock timeout.
    held_fd = os.open(LOCK, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    require((os.fstat(held_fd).st_dev, os.fstat(held_fd).st_ino) == initial_identity,
            'lock changed before probe')
    fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    started = time.monotonic()
    result = subprocess.run([OWNER, 'pull', 'qualification'], capture_output=True, timeout=8)
    require(result.returncode == 0 and b'break-glass' in result.stderr
            and time.monotonic() - started < 7 and os.path.isfile(MARKER),
            'break-glass pull failed')
    require(owner('release') != 0 and os.path.lexists(MARKER),
            'release did not preserve hold under contention')
    result = subprocess.run([OWNER, 'release', '--force'], capture_output=True, timeout=8)
    require(result.returncode == 0 and not os.path.lexists(MARKER),
            'forced release failed')
    os.close(held_fd)
    held_fd = None
    passed('a-break-glass')

    # (b) Audit hooks pause immediately BEFORE the actual os.replace syscall.
    # The unchanged installed command body has already acquired its own flock.
    ready_r, ready_w = os.pipe()
    go_r, go_w = os.pipe()

    def paused_owner():
        def audit(event, args):
            if event == 'os.rename' and str(args[1]) == MARKER:
                os.write(ready_w, b'W')
                signal_byte(go_r, b'G')
        sys.addaudithook(audit)
        sys.argv = [OWNER, 'pull', 'qual1-reader-wait']
        with open(os.devnull, 'wb') as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            runpy.run_path(OWNER, run_name='__main__')

    writer = spawn(paused_owner)
    signal_byte(ready_r, b'W')
    reader_r, reader_w = os.pipe()

    def core_reader():
        # Agent-owned code is imported ONLY after dropping every root privilege.
        os.initgroups('agent', agent.pw_gid)
        os.setgid(agent.pw_gid)
        os.setuid(agent.pw_uid)
        sys.path.insert(0, CORE)
        from safe_core import acquire_marker_lock, safe_read
        os.write(reader_w, b'R')
        started = time.monotonic()
        fd = acquire_marker_lock({'bootstrap_hold_file': MARKER})
        try:
            require(b'qual1-reader-wait' in safe_read(MARKER), 'reader missed mutation')
        finally:
            os.close(fd)
        require(0.5 <= time.monotonic() - started < 5.5, 'reader did not wait within bound')
        os.write(reader_w, b'D')

    reader = spawn(core_reader)
    signal_byte(reader_r, b'R')
    time.sleep(0.75)
    require(status(reader) is None and not select.select([reader_r], [], [], 0)[0],
            'core reader bypassed writer lock or failed validation')
    os.write(go_w, b'G')
    require(wait(writer) == 0, 'instrumented installed writer failed')
    signal_byte(reader_r, b'D')
    require(wait(reader) == 0, 'core reader failed after mutation')
    passed('b-reader-wait')
except BaseException as error:
    # Exception text may originate in runtime code: report type, never contents.
    print('FAIL probe (' + type(error).__name__ + ')', flush=True)
finally:
    # A second catchable signal must not interrupt the cleanup trap.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    try:
        cleanup()
    except BaseException:
        print('FAIL c-cleanup: run sudo agent-hold release and investigate lock identity.', flush=True)
    for name, ok in checks.items():
        if not ok:
            print('FAIL ' + name, flush=True)
if all(checks.values()):
    try:
        release = os.path.realpath('/usr/local/lib/studio-layer/current')
        commit = os.path.basename(release)
        require(len(commit) == 40 and all(c in '0123456789abcdef' for c in commit),
                'installed release is not pinned')
        protected(release)
        target = '/etc/studio-layer/qualified'
        protected('/etc/studio-layer')
        fd, tmp = tempfile.mkstemp(prefix='.qualified-', dir='/etc/studio-layer')
        try:
            with os.fdopen(fd, 'w') as f:
                os.fchown(f.fileno(), 0, 0)
                os.fchmod(f.fileno(), 0o644)
                f.write('date=%s\nlock_inode=%s\nrelease=%s\n' %
                        (time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), initial_identity[1], commit))
                f.flush()
                os.fsync(f.fileno())
            os.link(tmp, target, follow_symlinks=False)
        finally:
            os.unlink(tmp)
        print('PASS qualified', flush=True)
    except BaseException:
        print('FAIL qualified marker', flush=True)
        sys.exit(1)
sys.exit(0 if all(checks.values()) else 1)
PY
