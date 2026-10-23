"""Same-seat processes share flock; chat leases use TTL. Only the holder releases, unless forced."""
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
        if not (isinstance(d, dict) and all(type(d[k]) is bool for k in ('released', 'pending', 'process'))): raise ValueError()
        if not (all(isinstance(d[k], str) and d[k] for k in ('holder', 'taken_at', 'head', 'token'))): raise ValueError()
        datetime.fromisoformat(d['taken_at'])
        if not d['process']:
            if not (type(d['expires_epoch']) in (int, float) and math.isfinite(d['expires_epoch'])): raise ValueError()
            datetime.fromisoformat(d['expires_at'])
        d['_file'] = str(path(home, repo).with_name('.lock.process'))
        return d
    except FileNotFoundError: return None
    except (OSError, ValueError, KeyError, TypeError, OverflowError): return {'invalid': True}
def head(repo):
    branch = subprocess.run(['git', '-C', str(repo), 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD'], capture_output=True, text=True)
    ref = branch.stdout.strip() if branch.returncode == 0 else next((b for b in ('main', 'master') if subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', '--quiet', b], capture_output=True).returncode == 0), '')
    result = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', ref], capture_output=True, text=True)
    if result.returncode or not result.stdout.strip(): raise refusal(repo, 'Cannot establish execution source revision')
    return result.stdout.strip()
def active(d):
    with Path(d['_file']).open('a') as probe:
        try: fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB); return False
        except BlockingIOError: return True
def free(d, now=None):
    if not d or d.get('invalid'): return not d
    return not active(d) if d['process'] else d['released'] or d['expires_epoch'] <= (time.time() if now is None else now)
write = lambda home, repo, data: atomic_write(path(home, repo), {k: v for k, v in data.items() if not k.startswith('_')})
@contextmanager
def guarded(home, repo):
    try:
        target = path(home, repo); target.parent.mkdir(parents=True, exist_ok=True)
        with (target.parent / '.lock.guard').open('a') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX); yield
    except (OSError, ValueError, IndexError) as error: raise refusal(repo, f'Cannot inspect lock: {error}') from error
def inspect(home, repo, name):
    d = read(home, repo)
    if d and d.get('invalid') or not d and active({'_file': str(path(home, repo).with_name('.lock.process'))}): raise refusal(repo, 'Lock held by unknown (malformed, missing or unreadable record)')
    if d and not free(d) and d['holder'] != name: raise refusal(repo, f"held by {d['holder']} since {d['taken_at']}")
    return d
def acquire(home, repo, holder=None, ttl=120, revision=None):
    name = holder or os.environ.get('TASK_HOLDER', '').strip()
    if not name or not name.strip(): raise refusal(repo, 'Set TASK_HOLDER or pass --holder <name>')
    if not math.isfinite(ttl) or ttl <= 0: raise LockBusy('--ttl must be greater than zero finite minutes')
    with guarded(home, repo):
        d = inspect(home, repo, name); now = time.time(); fresh = free(d, now)
        baseline = d['head'] if d and (fresh or d['pending']) else revision or head(repo)
        if revision and d and (fresh or d['pending']) and baseline != revision:
            print(f'Sources changed since the previous lock change ({baseline[:7]} -> {revision[:7]}); re-read before acting.')
        key = str(path(home, repo))
        if revision and key not in OWNERS:
            descriptor = path(home, repo).with_name('.lock.process').open('a')
            fcntl.flock(descriptor, fcntl.LOCK_SH); OWNERS[key] = (descriptor, None)
        process = bool(revision) or bool(d and not fresh and d['process'])
        data = dict(holder=name, process=process, taken_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    expires_at=None if process else datetime.fromtimestamp(now + ttl*60, timezone.utc).isoformat(),
                    expires_epoch=None if process else now + ttl*60, head=revision or baseline, pending=not bool(revision) and bool(d), released=False,
                    token=os.urandom(16).hex() if fresh else d['token'])
        write(home, repo, data)
        if revision: OWNERS[key] = (OWNERS[key][0], data['token'])
        return read(home, repo)
def release(home, repo, force=False, holder=None):
    with guarded(home, repo):
        if active({'_file': str(path(home, repo).with_name('.lock.process'))}): raise refusal(repo, 'Work is still running; cancel it before releasing ownership')
        d = read(home, repo)
        if d and d.get('invalid'):
            if not force: raise refusal(repo, 'Lock held by unknown')
            path(home, repo).unlink()
        else:
            name = holder or os.environ.get('TASK_HOLDER', '').strip()
            if not force and (not name or not name.strip()): raise refusal(repo, 'Set TASK_HOLDER or pass --holder <name>')
            if not force and d and not free(d) and d['holder'] != name: raise refusal(repo, f"held by {d['holder']} since {d['taken_at']}")
            if d: d['released'] = True; write(home, repo, d)
        print(f'Released lock for {Path(repo).name}.')
def display(d, now=None):
    try:
        if d and d.get('invalid'): raise ValueError()
        if free(d, now): return 'free'
        return f"{d['holder']} (age {max(0, int(((now or time.time()) - datetime.fromisoformat(d['taken_at']).timestamp())/60))}m, expires {d['expires_at'] if not d['process'] else 'when work exits'})"
    except (OSError, ValueError, IndexError): return 'held by unknown (clear with task lock <repo> --release --force)'
def work(home, repo, revision=None): acquire(home, repo, revision=revision or head(repo))
def descriptors(): return tuple(item[0].fileno() for item in OWNERS.values())
def verify(home, repo):
    with guarded(home, repo):
        d = inspect(home, repo, os.environ.get('TASK_HOLDER'))
        if not d or free(d) or d['token'] != OWNERS.get(str(path(home, repo)), (None, None))[1]: raise refusal(repo, 'Lock ownership changed before publication')
def configure(commands):
    sub = commands.add_parser('lock', help='show/take lock; --release requires the holder or --force')
    sub.add_argument('repo'); sub.add_argument('--holder'); sub.add_argument('--ttl', type=float)
    sub.add_argument('--release', action='store_true'); sub.add_argument('--force', action='store_true')
def requires_pm(args): return args.command == 'lock' and (args.release or args.holder is not None or args.ttl is not None)
def before_work(args, home, resolve_repo):
    if args.command in {'start', 'lane', 'merge', 'close', 'risk', 'redteam', 'runner-start', 'realign', 'gate-drill'} or args.command == 'review' and args.run:
        for name in args.repos if args.command == 'gate-drill' else [args.repo]:
            repo = resolve_repo(name)
            if not os.environ.get('TASK_HOLDER', '').strip(): raise refusal(repo, 'Set TASK_HOLDER or pass --holder <name>')
            with guarded(home, repo): inspect(home, repo, os.environ['TASK_HOLDER'])
def command(args, home, resolve_repo):
    repo = resolve_repo(args.repo)
    if args.release: release(home, repo, args.force, args.holder)
    elif args.holder is not None or args.ttl is not None: print(f"{repo.name}: {display(acquire(home, repo, args.holder, args.ttl if args.ttl is not None else 120))}")
    else: print(f'{repo.name}: {display(read(home, repo))}')
def atomic_write(path, data): raise LockBusy('Lock dependencies not initialized')
OWNERS = {}
