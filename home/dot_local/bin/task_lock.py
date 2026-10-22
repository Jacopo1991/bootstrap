"""Advisory per-project PM locks for task."""
import fcntl, json, os, subprocess, time
from datetime import datetime, timezone
from pathlib import Path

class LockBusy(Exception): pass

def path(home, repo): return Path(home) / ".local/state/task" / Path(repo).name / "lock.json"

def read(home, repo):
    target = path(home, repo)
    try:
        data = json.loads(target.read_text())
        return data if isinstance(data, dict) and {"holder", "taken_at", "expires_epoch", "head"} <= data.keys() else {"invalid": True}
    except FileNotFoundError: return None
    except (OSError, ValueError): return {"invalid": True}

def head(repo):
    branch = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], capture_output=True, text=True)
    name = branch.stdout.strip().removeprefix("origin/") if branch.returncode == 0 else ""
    if not name: name = next((b for b in ("main", "master") if subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{b}"], capture_output=True).returncode == 0), "main")
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", f"refs/heads/{name}"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""

def alive(data):
    boot = Path("/proc/sys/kernel/random/boot_id")
    if data.get("boot_id") and boot.exists() and boot.read_text().strip() != data["boot_id"]: return False
    pid = data.get("pid")
    return True if pid is None else process_alive(pid)

def free(data, now=None):
    try: return not data or data.get("released") or not alive(data) or float(data["expires_epoch"]) <= (now or time.time())
    except (KeyError, TypeError, ValueError): return False

def write(home, repo, data): atomic_write(path(home, repo), data)

def acquire(home, repo, holder=None, ttl=120, pid=None):
    target = path(home, repo); target.parent.mkdir(parents=True, exist_ok=True)
    with (target.parent / ".lock.guard").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX); current = read(home, repo)
        if current and (current.get("invalid") or not isinstance(current, dict)):
            raise LockBusy(f"lock for {target.parent.name} is held by unknown; clear it with `task lock {target.parent.name} --release --force`")
        now = time.time(); name = holder or os.environ.get("TASK_HOLDER") or "unnamed"
        if current and not free(current, now) and current.get("holder") != name:
            raise LockBusy(f"held by {current.get('holder') or 'unknown'} since {current.get('taken_at', 'unknown time')}; release with `task lock {target.parent.name} --release` or wait for its TTL")
        new_head = head(repo)
        if current and free(current, now) and current.get("head", "") != new_head:
            print(f"Sources changed since the previous lock change ({current.get('head','')[:7]} -> {new_head[:7]}); re-read before acting.")
        boot = Path("/proc/sys/kernel/random/boot_id")
        data = {"holder": name, "pid": pid, "boot_id": boot.read_text().strip() if pid and boot.exists() else None,
                "taken_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "expires_at": datetime.fromtimestamp(now + ttl * 60, timezone.utc).isoformat(),
                "expires_epoch": now + ttl * 60, "head": new_head, "released": False}
        write(home, repo, data)
        if name == "unnamed": print("Lock holder is unnamed; set TASK_HOLDER or pass --holder <name> to identify this seat.")
        return data

def release(home, repo, force=False):
    target = path(home, repo); target.parent.mkdir(parents=True, exist_ok=True)
    with (target.parent / ".lock.guard").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX); current = read(home, repo)
        if current and (current.get("invalid") or not isinstance(current, dict)):
            if not force: raise LockBusy(f"lock is held by unknown; clear with `task lock {target.parent.name} --release --force`")
            target.unlink(missing_ok=True)
        elif current:
            current["released"] = True; current["holder"] = None; write(home, repo, current)
        else: print(f"No lock for {target.parent.name}."); return
        print(f"Released lock for {target.parent.name}.")

def display(data, now=None):
    if data and (data.get("invalid") or not isinstance(data, dict)): return "held by unknown (clear with task lock <repo> --release --force)"
    if not data or free(data, now): return "free"
    try: age = max(0, int(((now or time.time()) - datetime.fromisoformat(data["taken_at"]).timestamp()) / 60))
    except (KeyError, TypeError, ValueError): return "held by unknown (clear with task lock <repo> --release --force)"
    return f"{data.get('holder') or 'unnamed'} (age {age}m, expires {data.get('expires_at', 'unknown')})"

def configure(commands):
    sub = commands.add_parser("lock", help="show, take or release the advisory project lock")
    sub.add_argument("repo"); sub.add_argument("--holder"); sub.add_argument("--ttl", type=float)
    sub.add_argument("--release", action="store_true"); sub.add_argument("--force", action="store_true")

def requires_pm(args): return args.command == "lock" and (args.release or args.holder is not None or args.ttl is not None)

def before_work(args, home, resolve_repo):
    if args.command in {"start", "lane", "merge", "close", "runner-start"} or args.command == "review" and args.run: acquire(home, resolve_repo(args.repo))

def command(args, home, resolve_repo):
    repo = resolve_repo(args.repo)
    if args.release: release(home, repo, args.force)
    elif args.holder or args.ttl is not None:
        ttl = args.ttl if args.ttl is not None else 120
        if ttl <= 0: raise LockBusy("--ttl must be greater than zero minutes.")
        data = acquire(home, repo, args.holder, ttl); print(f"{repo.name}: {display(data)}")
    else: print(f"{repo.name}: {display(read(home, repo))}")

atomic_write = lambda path, data: None
process_alive = lambda pid: False
