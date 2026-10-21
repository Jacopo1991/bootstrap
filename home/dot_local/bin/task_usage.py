"""Zero-token vendor usage readings, usage-pacing policy and pace verdicts for task.

Readings are the vendors' own numbers: the newest Codex rollout `rate_limits` and the newest
Claude `rate_limit_event` that a recorded lane or review left behind. Nothing here calls a model.
"""
import json
import os
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

VENDORS = ("claude", "codex")
WEEK = timedelta(days=7)
CPH = ZoneInfo("Europe/Copenhagen")
CHEAPER = {"claude": "haiku", "codex": "gpt-6-luna"}
DEEPER = {"claude": "opus", "codex": "gpt-6.1-sol"}
SPARE = "spare reset held; reset at 100 %"
EXHAUSTED = "exhausted-automated-share"
FIVE_OUT, FIVE_WARN = "5h-exhausted", "5h-warning"
FIVE_BAND = 15  # points below the 5-hour limit where only one lane or review may run
STALE = timedelta(minutes=45)
DEFAULTS = {"reserve": {"claude": 25, "codex": 10}, "spare_resets": {"claude": 0, "codex": 1},
            "priority": ["madplanner-v2", "household-signal-harness", "product-harness", "cortex-kb-company"],
            "under_use_percent": 60, "under_use_hours": 36, "ahead_margin": 10, "research_backlog": []}


def _json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=1))
    temporary.replace(path)


def _dir(home):
    return home / ".local/state/task/usage"


def _stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _epoch(value):
    return datetime.fromtimestamp(value, timezone.utc)


def _number(value, top=None):
    return type(value) in (int, float) and value >= 0 and (top is None or value <= top)


def policy(home):
    """Company policy TOML (pointer file, else the default path) over built-in defaults."""
    pointer = _text(home / ".config/task/usage-policy-path")
    path = home / pointer[2:] if pointer.startswith("~/") else Path(pointer) if pointer else (
        home / "cortex/cortex-kb-company/policy/usage-pacing.toml")
    result = json.loads(json.dumps(DEFAULTS))
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return result
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{path}: {error}")
    for key, value in raw.items():
        if key in ("reserve", "spare_resets"):
            valid = isinstance(value, dict) and all(
                name in VENDORS and _number(item, 100 if key == "reserve" else None) for name, item in value.items())
            result[key].update(value if valid else {})
        elif key in ("priority", "research_backlog"):
            valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
            result[key] = value if valid else result[key]
        else:
            valid = key in DEFAULTS and _number(value)
            result[key] = value if valid else result.get(key)
        if not valid:
            raise ValueError(f"{path}: invalid {key}")
    return result


def _text(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _unknown(vendor, reason, has_five=False):
    return {"vendor": vendor, "reason": reason, "has_five": has_five}


def _reading(vendor, at, weekly, resets, five_hour, five_resets, now, has_five=True):
    """Reject a reading from before the last reset or older than the weekly window.

    `has_five` says the vendor has a 5-hour window at all (Claude always; Codex when one was seen in 7 days).
    """
    if resets <= now:
        return _unknown(vendor, f"newest reading ({at:%Y-%m-%d %H:%M} UTC) is from before the last weekly reset", has_five)
    if now - at > WEEK:
        return _unknown(vendor, f"newest reading ({at:%Y-%m-%d %H:%M} UTC) is older than the 7-day window", has_five)
    if five_resets is not None and five_resets <= now:
        five_hour = None
    return {"vendor": vendor, "reason": "", "has_five": has_five, "at": at, "resets_at": resets, "weekly": float(weekly),
            "five_hour": None if five_hour is None else float(five_hour),
            "five_resets": five_resets if five_hour is not None else None}


def read_codex(home, now):
    """Newest Codex rollout's rate_limits: window_minutes >= 10080 is weekly, shorter is 5-hour."""
    root = home / ".codex/sessions"
    files = []
    for path in root.glob("**/rollout-*.jsonl"):
        try:
            files.append((path.stat().st_mtime, path))
        except OSError:
            pass
    if not files:
        return _unknown("codex", f"no rollout file under {root}")
    found, seen_five = None, False  # the newest reading; whether any event of the last 7 days had a 5-hour window
    for _, path in sorted(files, reverse=True)[:5]:
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - (1 << 20)))
                lines = stream.read().decode(errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                event = json.loads(line)
                limits = event["payload"]["rate_limits"]
                at = _stamp(event["timestamp"])
                windows = {"weekly" if item["window_minutes"] >= 10080 else "five": item
                           for item in (limits.get("primary"), limits.get("secondary")) if item}
                weekly, five = windows["weekly"], windows.get("five", {})
                found = found or (at, weekly["used_percent"], _epoch(weekly["resets_at"]), five.get("used_percent"),
                                  _epoch(five["resets_at"]) if five else None)
                seen_five = seen_five or bool(five) and now - at <= WEEK
            except (KeyError, TypeError, ValueError):
                continue
            if seen_five:
                break
        if seen_five:
            break
    if found:
        return _reading("codex", *found, now, seen_five)
    return _unknown("codex", "no weekly rate_limits in the newest Codex rollouts")


def read_claude(home, now):
    """Newest rate_limit_event per window, kept in task state by recorded lanes and reviews."""
    state = _json(_dir(home) / "claude.json")
    try:
        week, five = state["seven_day"], state.get("five_hour") or {}
        return _reading("claude", _stamp(week["at"]), week["utilization"] * 100, _epoch(week["resetsAt"]),
                        five["utilization"] * 100 if five else None,
                        _epoch(five["resetsAt"]) if five else None, now)
    except (KeyError, TypeError, ValueError):
        return _unknown("claude", "no rate_limit_event recorded yet (a recorded Claude lane or review writes one)", True)


def readings(home, now):
    return {"claude": read_claude(home, now), "codex": read_codex(home, now)}


def record_claude(home, event, now=None):
    """Keep the newest per-window readings from one stream-json rate_limit_event."""
    info = event.get("rate_limit_info") or {}
    windows = dict(info.get("unifiedWindows") or {})
    if info.get("rateLimitType") in ("five_hour", "seven_day"):
        windows.setdefault(info["rateLimitType"], info)
    path = _dir(home) / "claude.json"
    state = _json(path)
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    for name in ("five_hour", "seven_day"):
        window = windows.get(name)
        if isinstance(window, dict) and _number(window.get("utilization")) and _number(window.get("resetsAt")):
            state[name] = {"utilization": window["utilization"], "resetsAt": window["resetsAt"], "at": stamp}
    try:
        _write(path, state)
    except OSError:
        pass


class ClaudeStream:
    """Split `claude -p --output-format stream-json` into the old text output and usage readings.

    Lines that are not JSON objects pass through as text, so plain output still works.
    """

    def __init__(self, home):
        self.home, self.pending = home, b""

    def feed(self, chunk):
        """Text for the complete lines in this chunk."""
        *lines, self.pending = (self.pending + chunk).split(b"\n")
        return "".join(map(self.line, lines))

    def finish(self):
        pending, self.pending = self.pending, b""
        return self.line(pending) if pending else ""

    def line(self, raw):
        try:
            event = json.loads(raw)
        except ValueError:
            event = None
        if not isinstance(event, dict):
            return raw.decode(errors="replace") + "\n"
        if event.get("type") == "rate_limit_event":
            record_claude(self.home, event)
        return event["result"] + "\n" if event.get("type") == "result" and isinstance(event.get("result"), str) else ""


def claim(home, vendor, kind, pid, now=None):
    """Record a started lane or review process, so the 5-hour rules can count what runs machine-wide."""
    now = now or datetime.now(timezone.utc)
    directory = _dir(home) / "running" / vendor
    try:
        for old in directory.glob("*.json"):
            if now - _stamp(_json(old).get("at", "1970-01-01T00:00:00+00:00")) > timedelta(days=2):
                old.unlink()
    except (OSError, ValueError):
        pass
    try:
        _write(directory / f"{pid}.json", {"kind": kind, "pid": pid, "at": now.isoformat()})
    except OSError:
        pass


def _claims(home, vendor):
    for path in (_dir(home) / "running" / vendor).glob("*.json"):
        entry = _json(path)
        try:
            yield _stamp(entry["at"]), entry["pid"]
        except (KeyError, TypeError, ValueError):
            pass


def running(home, vendor):
    """Lanes and reviews of this vendor whose process is alive, machine-wide."""
    count = 0
    for _, pid in _claims(home, vendor):
        try:
            os.kill(int(pid), 0)
            count += 1
        except (OSError, ValueError, TypeError):
            pass
    return count


def _five(reading, pol, now, ran):
    """5-hour verdict: exhausted at 100 - reserve; warning 15 points lower, or on a missing or stale reading.

    A vendor with no 5-hour window in its readings has none to pace: always ok. Otherwise unknown is never plenty.
    """
    reserve, used, has = pol["reserve"][reading["vendor"]], reading.get("five_hour"), reading["has_five"]
    stale = has and ran and reading["reason"] == "" and now - reading["at"] > STALE
    missing = has and used is None
    if used is not None and used >= 100 - reserve:
        return {"five": FIVE_OUT, "stale": False, "missing": False}
    return {"five": FIVE_WARN if stale or missing or used is not None and used >= 100 - reserve - FIVE_BAND else "ok",
            "stale": stale, "missing": missing}


def assess(reading, pol, now, ran=False):
    """Add spare/limit and the pace verdict to a reading (see `task usage` for the meanings).

    `ran` says the vendor's lanes ran since the reading: an old reading is then stale.
    The 5-hour verdict ignores spare resets, which only refill the weekly window.
    """
    vendor = reading["vendor"]
    spare = pol["spare_resets"][vendor] > 0
    result = dict(reading, spare=spare, limit=100 - (0 if spare else pol["reserve"][vendor]),
                  **_five(reading, pol, now, ran))
    if reading["reason"]:
        return dict(result, verdict="unknown")
    left = (reading["resets_at"] - now) / timedelta(hours=1)
    elapsed = min(100.0, max(0.0, 100 * (1 - left / (WEEK / timedelta(hours=1)))))
    used, margin = reading["weekly"], pol["ahead_margin"]
    if used >= result["limit"]:
        verdict = EXHAUSTED
    elif used < pol["under_use_percent"] and left <= pol["under_use_hours"]:
        verdict = "under-used-near-reset"
    else:
        verdict = "ahead" if used > elapsed + margin else "under" if used < elapsed - margin else "on"
    return dict(result, verdict=verdict, left=left, elapsed=elapsed)


def _top(home, pol):
    """Highest-priority repository with an unmerged started task."""
    for name in pol["priority"]:
        if any(not (task.parent / "merge.json").exists()
               for task in (home / ".local/state/task" / name).glob("*/task.json")):
            return name
    return ""


def log(home, text, now=None):
    try:
        directory = _dir(home)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "pace.log").open("a") as stream:
            stream.write(f"{(now or datetime.now(timezone.utc)).isoformat(timespec='seconds')} {text}\n")
    except OSError:
        pass


def note(home, found, now, held=None):
    """Remember this week's used %, first exhaustion day and held-back lanes for the weekly line."""
    if found["reason"]:
        return
    path = _dir(home) / "weeks.json"
    state = _json(path)
    weeks = state.setdefault(found["vendor"], {})
    week = weeks.setdefault(found["resets_at"].isoformat(), {"held": []})
    week["used"] = round(found["weekly"], 1)
    if found["verdict"] == EXHAUSTED and not week.get("exhausted"):
        week["exhausted"] = now.astimezone(CPH).strftime("%a %Y-%m-%d")
    if found["five"] == FIVE_OUT:  # one entry per 5-hour window, keyed by its reset
        week.setdefault("five_exhausted", {}).setdefault(
            found["five_resets"].isoformat(), found["at"].astimezone(CPH).strftime("%a %Y-%m-%d %H:%M"))
    if held and held not in week["held"]:
        week["held"].append(held)
    for old in sorted(weeks)[:-4]:
        del weeks[old]
    try:
        _write(path, state)
    except OSError:
        pass


def assessments(home, now):
    """Policy and one assessment per vendor, plus warnings; a broken policy file falls back to defaults."""
    notes = []
    try:
        pol = policy(home)
    except ValueError as error:
        pol = json.loads(json.dumps(DEFAULTS))
        notes.append(f"pace warning: {error}; built-in defaults used")
    seen = {}
    for vendor, item in readings(home, now).items():
        since = item.get("at")
        ran = bool(since) and (running(home, vendor) > 0 or any(at > since for at, _ in _claims(home, vendor)))
        seen[vendor] = found = assess(item, pol, now, ran)
        if found["stale"]:
            notes.append(f"pace warning: {vendor} usage reading is {(now - since) / timedelta(minutes=1):.0f} min old "
                         "while its lanes ran; treated as the 5-hour warning band (one lane or review at a time)")
        if found["missing"]:
            notes.append(f"pace warning: {vendor} has no current 5-hour reading; "
                         "treated as the 5-hour warning band (one lane or review at a time)")
        note(home, found, now)
    return pol, seen, notes


def _ahead(item):
    return item["verdict"] == "ahead" and not item["spare"]


def _room(home, vendor, item):
    """Room on both windows: not weekly exhausted or ahead, and the 5-hour window allows one more."""
    five = item["five"] != FIVE_OUT and (item["five"] != FIVE_WARN or not running(home, vendor))
    return item["verdict"] != EXHAUSTED and not _ahead(item) and five


def five_caps(home, now=None):
    """Parallel lanes the 5-hour rules allow per vendor: 0 when exhausted, 1 in the warning band, else None."""
    _, seen, _ = assessments(home, now or datetime.now(timezone.utc))
    return {vendor: {FIVE_OUT: 0, FIVE_WARN: 1}.get(item["five"]) for vendor, item in seen.items()}


def gate(home, kind, vendor, tier, repo, key, now=None):
    """Pace check for one lane or review: which vendor, model hint, hold reason, notes.

    Returns vendor (maybe switched), hold (reason text or ""), cheaper (use the cheap routine model),
    spend (ISO reset time when the vendor is under-used near its reset) and notes to print.
    """
    now = now or datetime.now(timezone.utc)
    pol, seen, notes = assessments(home, now)
    out = {"vendor": vendor, "hold": "", "cheaper": False, "spend": "", "notes": notes}
    other = "codex" if vendor == "claude" else "claude"
    found = seen[vendor]
    if found["verdict"] == "unknown":
        notes.append(f"pace warning: {vendor} usage unknown ({found['reason']}); unchanged")
    if found["verdict"] == EXHAUSTED and found["spare"]:
        notes.append(f"Jacopo: {vendor} is at 100 %; use your spare reset")
    if found["verdict"] == EXHAUSTED or found["five"] == FIVE_OUT:
        alternative = seen[other]
        if kind == "lane" and tier == "routine" and _room(home, other, alternative):
            notes.append(f"pace: {vendor} {'5-hour window' if found['five'] == FIVE_OUT else 'automated share'} "
                         f"used up; routine lane moves to {other}")
            out["vendor"], found = other, alternative
            if alternative["verdict"] == "unknown":
                notes.append(f"pace warning: {other} usage unknown ({alternative['reason']}); unchanged")
        elif found["five"] == FIVE_OUT:
            out["hold"] = (f"{vendor} 5-hour window used up ({found['five_hour']:.0f} % of {100 - pol['reserve'][vendor]} %); "
                           f"no new {kind}s there until the reset at {_when(found['five_resets'])}")
        else:
            out["hold"] = (f"{vendor} automated share used up ({found['weekly']:.0f} % of {found['limit']:.0f} %); "
                           f"no new {kind}s there")
    if not out["hold"] and found["five"] == FIVE_WARN and running(home, out["vendor"]):
        out["hold"] = (f"{out['vendor']} 5-hour window in the warning band"
                       f"{' (reading stale)' if found['stale'] else ' (no 5-hour reading)' if found['missing'] else ''}: "
                       f"{running(home, out['vendor'])} lane or review running; one at a time")
    if not out["hold"] and _ahead(found):
        out["cheaper"] = kind == "lane" and tier == "routine"
        top = _top(home, pol)
        if kind == "lane" and top and top != repo:
            out["hold"] = f"{vendor} is ahead of pace; only {top} gets new lanes"
    if not out["hold"] and found["verdict"] == "under-used-near-reset":
        out["spend"] = found["resets_at"].isoformat()
    if out["hold"]:
        note(home, seen[vendor], now, f"{repo}/{key}")
        log(home, f"held {kind} {repo}/{key}: {out['hold']}", now)
    return out


def batch_gate(home, builders, now=None):
    """Reason a new batch must not start on its own, or ""; plus warnings."""
    now = now or datetime.now(timezone.utc)
    _, seen, notes = assessments(home, now)
    for vendor in sorted(set(builders)):
        item = seen[vendor]
        if item["verdict"] == "unknown":
            notes.append(f"pace warning: {vendor} usage unknown ({item['reason']}); unchanged")
        elif item["verdict"] == "ahead" and not item["spare"]:
            log(home, f"batch not started: {vendor} ahead of pace", now)
            return f"{vendor} is ahead of pace; no new batch starts on its own", notes
    return "", notes


def _when(value):
    return f"{value:%Y-%m-%d %H:%M} UTC / {value.astimezone(CPH):%Y-%m-%d %H:%M} Europe/Copenhagen"


def _applied(vendor, item, pol, top):
    reserve = f"reserve {pol['reserve'][vendor]} %"
    if item["spare"]:
        return f"{SPARE}; no reserve, lanes not held back for pace"
    return {
        EXHAUSTED: f"{reserve} used up: no new lanes or reviews on {vendor}; routine lanes move to the other vendor if it has room",
        "ahead": f"{reserve}; routine lanes use {CHEAPER[vendor]}, no new batch starts on its own, "
                 f"new lanes only for {top or 'the top-priority repo with work'}",
        "under-used-near-reset": f"{reserve}; spend before the reset: boost on until it ends (stronger models on queued work), "
                                 f"deeper reviews ({DEEPER[vendor]}), then the research backlog: "
                                 f"{', '.join(pol['research_backlog']) or 'none named'}",
    }.get(item["verdict"], f"{reserve}; normal")


def report(home, now=None):
    """Lines for `task usage`."""
    now = now or datetime.now(timezone.utc)
    pol, seen, lines = assessments(home, now)
    for vendor in VENDORS:
        item = seen[vendor]
        reserve = pol["reserve"][vendor]
        effect = {FIVE_OUT: f"no new lanes or reviews on {vendor} until the reset; routine lanes move to the other vendor if it has room",
                  FIVE_WARN: f"at most one lane or review of {vendor} at a time, machine-wide, {running(home, vendor)} running now"
                  }.get(item["five"], "normal")
        if item["verdict"] == "unknown":
            lines.append(f"{vendor}: unknown ({item['reason']})")
            if item["missing"]:
                lines.append(f"  5-hour: {item['five']} (no 5-hour reading); {effect}")
            continue
        five = "n/a" if item["five_hour"] is None else f"{item['five_hour']:.0f} %"
        age = (now - item["at"]) / timedelta(minutes=1)
        reset = f", resets at {_when(item['five_resets'])}" if item["five_resets"] else ""
        state = " (reading stale)" if item["stale"] else " (no 5-hour reading)" if item["missing"] else ""
        limits = (f"exhausted at {100 - reserve} %, warning at {100 - reserve - FIVE_BAND} %; {effect}" if item["has_five"]
                  else "no 5-hour window in the readings of the last 7 days; only the weekly rules apply")
        lines += [f"{vendor}: weekly used {item['weekly']:.0f} %, 5-hour used {five}",
                  f"  5-hour: {item['five'] if item['has_five'] else 'none'}{state}{reset}; {limits}",
                  f"  resets at {_when(item['resets_at'])}",
                  f"  reading {item['at']:%Y-%m-%d %H:%M} UTC, {age:.0f} min old; week elapsed {item['elapsed']:.0f} %",
                  f"  pace: {item['verdict']}",
                  f"  policy: {_applied(vendor, item, pol, _top(home, pol))}"]
    return lines


def weekly_lines(home, now=None):
    """One line per vendor for the company STATUS."""
    now = now or datetime.now(timezone.utc)
    _, seen, _ = assessments(home, now)
    state = _json(_dir(home) / "weeks.json")
    lines = []
    for vendor in VENDORS:
        item = seen[vendor]
        weeks = state.get(vendor, {})
        key = item["resets_at"].isoformat() if not item["reason"] else max(weeks, default="")
        week = weeks.get(key, {})
        ends = f"{_stamp(key).astimezone(CPH):%Y-%m-%d}" if key else "unknown"
        used = f"{week['used']:.0f} %" if "used" in week else "unknown"
        five = "".join(f", 5-hour exhausted {when}" for when in week.get("five_exhausted", {}).values())
        lines.append(f"{vendor} week ending {ends}: used {used}, exhausted {week.get('exhausted') or 'no'}, "
                     f"lanes held back {len(week.get('held', []))}{five}")
    return lines
