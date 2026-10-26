"""PM wake events (T-77): `task` queues short event lines per PM; one detached flusher per PM batches
them for about 90 s and delivers one labelled message with `claude -p ... --cloud <session>`.

Emitting never raises and never waits on delivery. The flusher runs outside the hold switch, and
outside the unit that emitted the event, so a lane or check unit ending does not take it down.
"""
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# Repository to PM; any other repository goes to the PM named after it, only when the map has one.
ROUTES = {"company": "company", "cortex-kb-company": "company",
          "bootstrap": "workflow", "cortex-core": "workflow"}
LABEL = "[EVENT from AgentDev for the {pm} PM; information only, not Jacopo's approval]"
SESSION = re.compile(r"session_[A-Za-z0-9]+")
PM_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
WINDOW, RETRY, TIMEOUT, MAX_LINES, MAX_LINE = 90, 20, 60, 20, 160


def map_path(home):
    return Path(home) / ".config/pm-wake/sessions"


def state_root(home):
    return Path(home) / ".local/state/task/pm-wake"


def sessions(home):
    """`pm=session_id` lines; comments, blanks and malformed lines are ignored."""
    try:
        text = map_path(home).read_text()
    except OSError:
        return {}
    found = {}
    for line in text.splitlines():
        pm, _, session = line.strip().partition("=")
        if PM_NAME.fullmatch(pm.strip()) and SESSION.fullmatch(session.strip()):
            found[pm.strip()] = session.strip()
    return found


def pm_for(repo):
    name = Path(str(repo)).name
    return ROUTES.get(name, name.removeprefix("cortex-kb-"))


def short(line):
    line = " ".join(str(line).split())
    return line if len(line) <= MAX_LINE else line[:MAX_LINE - 3] + "..."


def emit(home, repo, line, *, event_class="WAKE", everyone=False):
    """Record a digest event or queue a wake for the repository's PM; silent when unmapped."""
    try:
        if event_class not in {"WAKE", "DIGEST"}:
            return
        mapped = sessions(home)
        targets = sorted(mapped) if everyone else [pm for pm in (pm_for(repo),) if pm in mapped]
        for pm in targets:
            message = line
            root = state_root(home)
            root.mkdir(parents=True, exist_ok=True)
            with open(root / f"{pm}.queue.lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                count_path = root / f"{pm}.digest.count"
                if event_class == "DIGEST":
                    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    with open(root / f"{pm}.digest", "a") as digest:
                        digest.write(f"{stamp} {short(line)}\n")
                    try:
                        count = int(count_path.read_text())
                    except (OSError, ValueError):
                        count = 0
                    count_path.write_text(str(count + 1))
                    continue
                try:
                    count = int(count_path.read_text())
                except (OSError, ValueError):
                    count = 0
                if count:
                    message = f"{line} (+{count} digest events since the last wake)"
                    count_path.write_text("0")
                with open(root / f"{pm}.spool", "a") as spool:
                    spool.write(short(message) + "\n")
            if not flusher_alive(home, pm):
                start_flusher(home, pm)
    except Exception:  # delivery must never disturb the command that emitted the event
        pass


def flusher_alive(home, pm):
    with open(state_root(home) / f"{pm}.flush.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def start_flusher(home, pm):
    """A transient user service leaves the emitting unit's cgroup and has no hold condition; without
    a user manager (a plain shell, the tests) a detached process does the same job;
    there is then no emitting unit whose cgroup it could need to leave."""
    window = os.environ.get("TASK_PM_WAKE_WINDOW", str(WINDOW))
    retry = os.environ.get("TASK_PM_WAKE_RETRY", str(RETRY))
    command = [sys.executable, str(Path(__file__).resolve()), "flush", pm, window, retry]
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        started = subprocess.run(["systemd-run", "--user", "--collect", "--quiet",
                                  "--unit", f"task-pm-wake-{pm}-{secrets.token_hex(4)}",
                                  f"--setenv=HOME={home}", f"--setenv=PATH={os.environ.get('PATH', '')}",
                                  "--", *command], timeout=10, **quiet)
        if started.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    subprocess.Popen(command, start_new_session=True, env=dict(os.environ, HOME=str(home)), **quiet)


def flush(home, pm, window=WINDOW, retry=RETRY):
    """Deliver batches until a window passes with nothing queued; one flusher per PM."""
    root = state_root(home)
    root.mkdir(parents=True, exist_ok=True)
    owner = open(root / f"{pm}.flush.lock", "a")
    try:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    while True:
        time.sleep(window)
        with open(root / f"{pm}.queue.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            spool = root / f"{pm}.spool"
            try:
                lines = spool.read_text().splitlines()
            except FileNotFoundError:
                lines = []
            spool.unlink(missing_ok=True)
            if not lines:
                owner.close()  # released under the queue lock: a later event starts a new flusher
                return 0
        deliver(home, pm, lines, retry)


def message(pm, lines):
    shown = lines[:MAX_LINES]
    extra = f"; and {len(lines) - len(shown)} more" if len(lines) > len(shown) else ""
    return LABEL.format(pm=pm) + " " + "; ".join(shown) + extra


def send(text, session):
    try:
        result = subprocess.run(["claude", "-p", text, "--cloud", session, "--output-format", "json"],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"timeout after {TIMEOUT} s"
    except OSError as error:
        return False, f"cannot run claude: {error.strerror or error}"
    if re.search(r'"ok"\s*:\s*true', result.stdout):
        return True, "ok"
    return False, f"exit {result.returncode}, no \"ok\":true"


def deliver(home, pm, lines, retry=RETRY):
    """One attempt and one retry; each attempt is a TSV line in sends.log, a failed batch shows in status."""
    session = sessions(home).get(pm)
    attempts = 2 if session else 0
    reason = "no session in the map"
    for attempt in range(1, attempts + 1):
        delivered, reason = send(message(pm, lines), session)
        log(home, pm, session, attempt, delivered, reason, lines)
        if delivered:
            record(home, pm, None)
            return True
        if attempt < attempts:
            time.sleep(retry)
    if not attempts:
        log(home, pm, "-", 0, False, reason, lines)
    record(home, pm, reason)
    return False


def log(home, pm, session, attempt, delivered, reason, lines):
    row = [datetime.now(timezone.utc).isoformat(timespec="seconds"), pm, session, str(attempt),
           "ok" if delivered else "failed", reason, str(len(lines)), " | ".join(lines)]
    with open(state_root(home) / "sends.log", "a") as stream:
        stream.write("\t".join(" ".join(field.split()) for field in row) + "\n")


def record(home, pm, reason):
    path = state_root(home) / "failures.json"
    with open(state_root(home) / "failures.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        if reason is None:
            data.pop(pm, None)
        else:
            data[pm] = dict(count=data.get(pm, {}).get("count", 0) + 1, reason=reason,
                            last=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        path.write_text(json.dumps(data, sort_keys=True))


def status_lines(home):
    try:
        data = json.loads((state_root(home) / "failures.json").read_text())
    except (OSError, ValueError):
        return []
    return [f"pm-wake {pm}: {row['count']} batch(es) undelivered after one retry, last {row['last']} "
            f"({row['reason']}); see {state_root(home) / 'sends.log'}" for pm, row in sorted(data.items())]


def set_session(home, pm, session):
    """`task pm-session`: validate, then replace the map in one rename, keeping every other line."""
    if not PM_NAME.fullmatch(pm):
        raise ValueError(f"PM name {pm!r}: lowercase letters, digits and '-' only")
    if not SESSION.fullmatch(session):
        raise ValueError(f"session id {session!r}: expected session_ followed by letters and digits")
    path = map_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / ".sessions.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            lines = []
        kept = [line for line in lines if line.strip().partition("=")[0].strip() != pm]
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".sessions-", delete=False) as stream:
            stream.write("\n".join([*kept, f"{pm}={session}"]) + "\n")
        os.chmod(stream.name, 0o600)
        os.replace(stream.name, path)
    return path


def digest(home, pm):
    """Return this mapped PM's digest lines and current count; unmapped PMs are silent."""
    if pm not in sessions(home):
        return None
    root = state_root(home)
    root.mkdir(parents=True, exist_ok=True)
    with open(root / f"{pm}.queue.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            lines = (root / f"{pm}.digest").read_text().splitlines()
        except OSError:
            lines = []
        try:
            count = int((root / f"{pm}.digest.count").read_text())
        except (OSError, ValueError):
            count = 0
    return lines, count


if __name__ == "__main__" and sys.argv[1:2] == ["flush"]:
    sys.exit(flush(Path.home(), sys.argv[2], float(sys.argv[3]), float(sys.argv[4])))
