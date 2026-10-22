"""Advisory per-project PM locks for task."""
import fcntl
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


class LockBusy(Exception):
    pass


def path(home, repo):
    return Path(home) / ".local/state/task" / Path(repo).name / "lock.json"


def read(home, repo):
    try:
        data = json.loads(path(home, repo).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def head(repo):
    result = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], capture_output=True, text=True)
    branch = result.stdout.strip().removeprefix("origin/") if result.returncode == 0 else ""
    if not branch:
        branch = next((name for name in ("main", "master") if subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
            capture_output=True).returncode == 0), "main")
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", f"refs/heads/{branch}"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def alive(data):
    boot = Path("/proc/sys/kernel/random/boot_id")
    if data.get("boot_id") and boot.exists() and boot.read_text().strip() != data["boot_id"]:
        return False
    pid = data.get("pid")
    if pid is None:
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False


def free(data, now=None):
    return not data or data.get("released") or not alive(data) or float(data.get("expires_epoch", 0)) <= (now or time.time())


def write(home, repo, data):
    target = path(home, repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="lock.", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name): os.unlink(name)


def acquire(home, repo, holder=None, ttl=120, pid=None):
    target = path(home, repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    with (target.parent / ".lock.guard").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        current = read(home, repo)
        now = time.time()
        name = holder or os.environ.get("TASK_HOLDER") or "unnamed"
        if current and not free(current, now) and current.get("holder") != name:
            taken = current.get("taken_at", "unknown time")
            raise LockBusy(f"held by {current.get('holder', 'unnamed')} since {taken}; release with `task lock {Path(repo).name} --release` or wait for its TTL")
        old_head, new_head = current.get("head", ""), head(repo)
        if current and free(current, now) and old_head != new_head:
            print(f"Sources changed since the previous lock change ({old_head[:7]} -> {new_head[:7]}); re-read before acting.")
        started = datetime.fromtimestamp(now, timezone.utc).isoformat()
        boot = Path("/proc/sys/kernel/random/boot_id")
        data = {"holder": name, "pid": pid, "boot_id": boot.read_text().strip() if pid and boot.exists() else None,
                "taken_at": started, "expires_at": datetime.fromtimestamp(now + ttl * 60, timezone.utc).isoformat(),
                "expires_epoch": now + ttl * 60, "head": new_head, "released": False}
        write(home, repo, data)
        if name == "unnamed": print("Lock holder is unnamed; set TASK_HOLDER or pass --holder <name> to identify this seat.")
        return data


def release(home, repo):
    target = path(home, repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    with (target.parent / ".lock.guard").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        current = read(home, repo)
        if not current: print(f"No lock for {Path(repo).name}."); return
        current["released"] = True
        current["holder"] = None
        write(home, repo, current)
        print(f"Released lock for {Path(repo).name}.")


def display(data, now=None):
    if not data or free(data, now): return "free"
    age = max(0, int(((now or time.time()) - datetime.fromisoformat(data["taken_at"]).timestamp()) / 60))
    return f"{data.get('holder') or 'unnamed'} (age {age}m, expires {data.get('expires_at', 'unknown')})"


def configure(commands):
    sub = commands.add_parser("lock", help="show, take or release the advisory project lock")
    sub.add_argument("repo"); sub.add_argument("--holder"); sub.add_argument("--ttl", type=float)
    sub.add_argument("--release", action="store_true")


def requires_pm(args):
    return args.command == "lock" and (args.release or args.holder is not None or args.ttl is not None)


def before_work(args, home, resolve_repo):
    if args.command in {"start", "lane", "merge", "close", "runner-start"} or args.command == "review" and args.run: acquire(home, resolve_repo(args.repo))


def command(args, home, resolve_repo):
    repo = resolve_repo(args.repo)
    if args.release: release(home, repo)
    elif args.holder or args.ttl is not None:
        ttl = args.ttl if args.ttl is not None else 120
        if ttl <= 0: raise LockBusy("--ttl must be greater than zero minutes.")
        data = acquire(home, repo, args.holder, ttl)
        print(f"{repo.name}: {display(data)}")
    else:
        print(f"{repo.name}: {display(read(home, repo))}")
