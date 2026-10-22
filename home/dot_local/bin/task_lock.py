"""Advisory project ownership. Any PM may release as an administrative override."""
import fcntl, json, math, os, subprocess, time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

class LockBusy(Exception): pass
def path(home, repo): return Path(home) / '.local/state/task' / Path(repo).name / 'lock.json'
def refusal(repo, reason): return LockBusy(f'{reason}; recover with `task lock {Path(repo).name} --release --force`')
def read(home, repo):
    try:
        d = json.loads(path(home, repo).read_text())
        if not (isinstance(d, dict) and type(d['released']) is bool and type(d['pending']) is bool): raise ValueError()
        if not (all(isinstance(d[k], str) and d[k] for k in ('holder', 'taken_at', 'expires_at', 'head', 'token'))): raise ValueError()
        if not (type(d['expires_epoch']) in (int, float) and math.isfinite(d['expires_epoch'])): raise ValueError()
        datetime.fromisoformat(d['taken_at']); datetime.fromisoformat(d['expires_at'])
        if not (d['pid'] is None or (type(d['pid']) is int and d['pid'] > 0 and isinstance(d['boot_id'], str) and bool(d['boot_id']) and isinstance(d['start'], str) and d['start'].isdigit())): raise ValueError()
        if not (d.get('unit') is None or isinstance(d['unit'], str) and d['unit'].startswith('task-runner-')): raise ValueError()
        return d
    except FileNotFoundError: return None
    except (OSError, ValueError, KeyError, TypeError, OverflowError): return {'invalid': True}
def head(repo):
    branch = subprocess.run(['git', '-C', str(repo), 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD'], capture_output=True, text=True)
    ref = branch.stdout.strip() if branch.returncode == 0 else next((b for b in ('main', 'master') if subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', '--quiet', b], capture_output=True).returncode == 0), '')
    result = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', ref], capture_output=True, text=True)
    if result.returncode or not result.stdout.strip(): raise refusal(repo, 'Cannot establish execution source revision')
    return result.stdout.strip()
def identity(pid):
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if not boot: raise OSError('missing boot id')
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return boot, fields[19], fields[0] != 'Z'
def alive(d):
    if d['pid'] and not Path('/proc/sys/kernel/random/boot_id').read_text().strip(): raise OSError('missing boot id')
    if d.get('unit'):
        result = subprocess.run(['systemctl', '--user', 'show', d['unit'], '--property=LoadState,ActiveState,MainPID'], capture_output=True, text=True)
        service = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if result.returncode and service.get('LoadState') != 'not-found': raise OSError('cannot inspect runner unit')
        if service.get('ActiveState') in {'active', 'activating', 'deactivating', 'reloading'}: return True
        if service.get('ActiveState') not in {'inactive', 'failed'}: raise OSError('unknown runner state')
    if d['pid'] is None: return False
    try: boot, start, running = identity(d['pid'])
    except FileNotFoundError:
        if not Path('/proc/sys/kernel/random/boot_id').read_text().strip(): raise OSError('missing boot id')
        return False
    return running and boot == d['boot_id'] and start == d['start']
def free(d, now=None):
    if not d: return True
    if d.get('invalid'): return False
    return d['released'] or (not alive(d) and (d['pid'] is not None or d['expires_epoch'] <= (time.time() if now is None else now)))
write = lambda home, repo, data: atomic_write(path(home, repo), data)
@contextmanager
def guarded(home, repo):
    try:
        target = path(home, repo); target.parent.mkdir(parents=True, exist_ok=True)
        with (target.parent / '.lock.guard').open('a') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX); yield
    except (OSError, ValueError, IndexError) as error: raise refusal(repo, f'Cannot inspect lock: {error}') from error
def inspect(home, repo, name):
    d = read(home, repo)
    if d and d.get('invalid'): raise refusal(repo, 'Lock held by unknown (malformed or unreadable record)')
    if d and not free(d) and d['holder'] != name: raise refusal(repo, f"held by {d['holder']} since {d['taken_at']}")
    return d
def acquire(home, repo, holder=None, ttl=120, pid=None, unit=None, revision=None):
    name = holder or os.environ.get('TASK_HOLDER', '').strip()
    if not name or not name.strip(): raise refusal(repo, 'Set TASK_HOLDER or pass --holder <name>')
    if not math.isfinite(ttl) or ttl <= 0: raise LockBusy('--ttl must be greater than zero finite minutes')
    with guarded(home, repo):
        d = inspect(home, repo, name); now = time.time(); fresh = free(d, now)
        baseline = d['head'] if d and (fresh or d['pending']) else revision or head(repo)
        if revision and d and (fresh or d['pending']) and baseline != revision:
            print(f'Sources changed since the previous lock change ({baseline[:7]} -> {revision[:7]}); re-read before acting.')
        pid = pid if pid is not None else d['pid'] if d and not fresh else os.getpid() if revision else None
        boot, start, running = (d['boot_id'], d['start'], True) if d and not fresh and pid == d['pid'] else identity(pid) if pid else (None, None, True)
        if not running: raise refusal(repo, 'Holding process is not running')
        data = dict(holder=name, pid=pid, boot_id=boot, start=start, unit=unit or (d.get('unit') if d and not fresh else None),
                    taken_at=datetime.fromtimestamp(now, timezone.utc).isoformat(), expires_at=datetime.fromtimestamp(now + ttl*60, timezone.utc).isoformat(),
                    expires_epoch=now + ttl*60, head=revision or baseline, pending=not bool(revision) and bool(d), released=False,
                    token=os.urandom(16).hex() if fresh else d['token'])
        write(home, repo, data); return data
def release(home, repo, force=False):
    with guarded(home, repo):
        d = read(home, repo)
        if d and d.get('invalid'):
            if not force: raise refusal(repo, 'Lock held by unknown')
            path(home, repo).unlink()
        elif d: d['released'] = True; write(home, repo, d)
        print(f'Released lock for {Path(repo).name}.')
def display(d, now=None):
    try:
        if d and d.get('invalid'): raise ValueError()
        if free(d, now): return 'free'
        return f"{d['holder']} (age {max(0, int(((now or time.time()) - datetime.fromisoformat(d['taken_at']).timestamp())/60))}m, expires {d['expires_at']})"
    except (OSError, ValueError, IndexError): return 'held by unknown (clear with task lock <repo> --release --force)'
def work(home, repo, revision=None):
    data = acquire(home, repo, revision=revision or head(repo)); OWNERS[str(repo)] = data['token']
def verify(home, repo):
    with guarded(home, repo):
        d = inspect(home, repo, os.environ.get('TASK_HOLDER'))
        if not d or free(d) or d['token'] != OWNERS.get(str(repo)): raise refusal(repo, 'Lock ownership changed before publication')
def configure(commands):
    sub = commands.add_parser('lock', help='show/take lock; --release is an administrative PM override')
    sub.add_argument('repo'); sub.add_argument('--holder'); sub.add_argument('--ttl', type=float)
    sub.add_argument('--release', action='store_true'); sub.add_argument('--force', action='store_true')
def requires_pm(args): return args.command == 'lock' and (args.release or args.holder is not None or args.ttl is not None)
def before_work(args, home, resolve_repo):
    if args.command in {'start', 'lane', 'merge', 'close', 'runner-start', 'realign', 'gate-drill'} or args.command == 'review' and args.run:
        for name in args.repos if args.command == 'gate-drill' else [args.repo]:
            repo = resolve_repo(name)
            if not os.environ.get('TASK_HOLDER', '').strip(): raise refusal(repo, 'Set TASK_HOLDER or pass --holder <name>')
            with guarded(home, repo): inspect(home, repo, os.environ['TASK_HOLDER'])
def command(args, home, resolve_repo):
    repo = resolve_repo(args.repo)
    if args.release: release(home, repo, args.force)
    elif args.holder is not None or args.ttl is not None: print(f"{repo.name}: {display(acquire(home, repo, args.holder, args.ttl if args.ttl is not None else 120))}")
    else: print(f'{repo.name}: {display(read(home, repo))}')
def atomic_write(path, data): raise LockBusy('Lock dependencies not initialized')
OWNERS = {}
