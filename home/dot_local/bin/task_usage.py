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


def _unknown(vendor, reason):
    return {"vendor": vendor, "reason": reason}


def _reading(vendor, at, weekly, resets, five_hour, five_resets, now):
    """Reject a reading from before the last reset or older than the weekly window."""
    if resets <= now:
        return _unknown(vendor, f"newest reading ({at:%Y-%m-%d %H:%M} UTC) is from before the last weekly reset")
    if now - at > WEEK:
        return _unknown(vendor, f"newest reading ({at:%Y-%m-%d %H:%M} UTC) is older than the 7-day window")
    if five_resets is not None and five_resets <= now:
        five_hour = None
    return {"vendor": vendor, "reason": "", "at": at, "resets_at": resets, "weekly": float(weekly),
            "five_hour": None if five_hour is None else float(five_hour)}


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
                return _reading("codex", at, weekly["used_percent"], _epoch(weekly["resets_at"]),
                                five.get("used_percent"),
                                _epoch(five["resets_at"]) if five else None, now)
            except (KeyError, TypeError, ValueError):
                continue
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
        return _unknown("claude", "no rate_limit_event recorded yet (a recorded Claude lane or review writes one)")


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


def assess(reading, pol, now):
    """Add spare/limit and the pace verdict to a reading (see `task usage` for the meanings)."""
    vendor = reading["vendor"]
    spare = pol["spare_resets"][vendor] > 0
    result = dict(reading, spare=spare, limit=100 - (0 if spare else pol["reserve"][vendor]))
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


def record_override(home, repo, task, model, tier, text, now=None):
    """Append one model-rule override (a top model outside its tier) to the overrides ledger."""
    entry = dict(time=(now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
                 repo=repo, task=task, model=model, tier=tier, override=text)
    directory = _dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "overrides.jsonl").open("a") as stream:
        stream.write(json.dumps(entry) + "\n")


def override_summary(home, now=None):
    """One line naming the model-rule overrides of the last 7 days."""
    since = (now or datetime.now(timezone.utc)) - WEEK
    found = []
    try:
        for line in (_dir(home) / "overrides.jsonl").read_text().splitlines():
            entry = json.loads(line)
            if _stamp(entry["time"]) >= since:
                found.append(f"{entry['repo']}/{entry['task']} {entry['model']} on {entry['tier']} "
                             f"at {entry['time']}: {entry['override']}")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return f"model overrides, last 7 days: {len(found)}" + (" (" + "; ".join(found) + ")" if found else "")


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
    seen = {vendor: assess(item, pol, now) for vendor, item in readings(home, now).items()}
    for item in seen.values():
        note(home, item, now)
    return pol, seen, notes


def _ahead(item):
    return item["verdict"] == "ahead" and not item["spare"]


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
    elif found["verdict"] == EXHAUSTED:
        if found["spare"]:
            notes.append(f"Jacopo: {vendor} is at 100 %; use your spare reset")
        alternative = seen[other]
        if kind == "lane" and tier == "routine" and alternative["verdict"] != EXHAUSTED and not _ahead(alternative):
            notes.append(f"pace: {vendor} automated share used up; routine lane moves to {other}")
            out["vendor"], found = other, alternative
            if alternative["verdict"] == "unknown":
                notes.append(f"pace warning: {other} usage unknown ({alternative['reason']}); unchanged")
        else:
            out["hold"] = (f"{vendor} automated share used up ({found['weekly']:.0f} % of {found['limit']:.0f} %); "
                           f"no new {kind}s there")
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
        if item["verdict"] == "unknown":
            lines.append(f"{vendor}: unknown ({item['reason']})")
            continue
        five = "n/a" if item["five_hour"] is None else f"{item['five_hour']:.0f} %"
        age = (now - item["at"]) / timedelta(minutes=1)
        lines += [f"{vendor}: weekly used {item['weekly']:.0f} %, 5-hour used {five}",
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
        lines.append(f"{vendor} week ending {ends}: used {used}, exhausted {week.get('exhausted') or 'no'}, "
                     f"lanes held back {len(week.get('held', []))}")
    return lines + [override_summary(home, now)]
