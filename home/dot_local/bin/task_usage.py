"""Zero-token vendor usage readings, usage-pacing policy and pace verdicts for task.

Readings are the vendors' own numbers: the newest Codex rollout `rate_limits` and the newest
Claude `rate_limit_event` that a recorded lane or review left behind. Nothing here calls a model.
"""
import email.utils
import json
import fnmatch
import fcntl
import http.client
import urllib.request
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
import os
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree
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
_ASSESSMENTS = ContextVar("usage_assessments", default=None)
# Decision-24: public Codex reset feed (primary JSON, RSS fallback), polled hourly by task-watch.
RESET_FEEDS = ("https://codexresets.com/api/resets", "https://aiidelist.com/codex-reset.xml")
RESET_POLL, RESET_FRESH = timedelta(hours=1), timedelta(hours=3)
RESET_FIELDS = ("announced_at", "target_date", "completed_at", "event_status", "reset_type", "timing_kind", "text", "tweet_url")
NOT_ANNOUNCED = ("teas", "predict", "interval", "estimat", "rumo")  # teasers and interval predictions change nothing
SPEND_DOWN = "decision-24 spend-down"
DEFAULTS = {"reserve": {"claude": 25, "codex": 10}, "spare_resets": {"claude": 0, "codex": 1},
            "priority": ["madplanner-v2", "household-signal-harness", "product-harness", "cortex-kb-company"],
            "under_use_percent": 60, "under_use_hours": 36, "ahead_margin": 10, "research_backlog": [],
            "boost_five_hour_max": 50,
            "codex_ramp": None,  # decision-23: { per_day = <points>, ceiling = <0-100> }; None is no ramp
            "blocked": {},  # vendor -> open foundation-defect reason, set by hand by the PM
            "review": {"high_paths": {"*": ["policy/**", "skills/**/SKILL.md", "skills/**/references/**",
                "AGENTS.md", "CLAUDE.md", "home/dot_codex/AGENTS.md", "home/dot_claude/CLAUDE.md",
                "INTENT.md", "decisions/**", "backlog/decisions/**"], "bootstrap": [
                "system/agent-policy/**", "home/dot_local/share/bootstrap/agent-policy/**", "system/*managed*", "system/codex-requirements.toml",
                "home/dot_claude/settings.json", "home/dot_codex/**", "home/.chezmoitemplates/codex-config.toml",
                "home/dot_local/bin/executable_task-hold", "HOLD*", "**/HOLD*", "**/*sandbox*",
                "home/dot_gitconfig.tmpl", "checks/boundary.sh", "system/vendor-block-hosts.sh", ".gitleaks.toml"]},
                "low": {"max_rounds": 2, "extra_rounds": 1},
                # Company decision-32: reviewer per builder; Codex reviews Codex-built normal-risk work.
                "normal": {"codex": "gpt-6.1-sol", "claude": "gpt-6-luna", "max_rounds": 3, "extra_rounds": 1},
                "high": {"codex": "sonnet", "claude": "gpt-6.1-sol", "max_rounds": 3, "extra_rounds": 1,
                         "red_team_model": "gpt-6.1-sol", "red_team_model_claude": "sonnet"}}}


def risk_paths(repo, record, paths, policy):
    patterns = policy["high_paths"].get("*", []) + policy["high_paths"].get(repo, [])
    forced = next((p for p in paths if p and any(fnmatch.fnmatchcase(p, pat) for pat in patterns)), "")
    risk = "high" if forced else record.get("risk", "normal")
    return risk, policy[risk], forced


def review_policy(value, result):
    """Apply each valid [review] table to `result`; return the problems of the others, which keep their defaults."""
    if not isinstance(value, dict):
        return ["invalid review keys"]
    problems = [f"invalid review.{tier}" for tier in value.keys() - result.keys()]
    for tier, fields in value.items():
        if tier not in result:
            continue
        if not isinstance(fields, dict) or tier != "high_paths" and fields.keys() - result[tier].keys():
            problems.append(f"invalid review.{tier}")
            continue
        bad = next((key for key, item in fields.items() if not (
                    isinstance(item, list) and all(isinstance(p, str) and p.strip() for p in item)
                    if tier == "high_paths" else
                    type(item) is int and item >= (1 if key == "max_rounds" else 0)
                    if key in {"max_rounds", "extra_rounds"} else
                    isinstance(item, str) and item.replace("-", "").replace(".", "").replace("_", "").isalnum())), None)
        if bad is not None:
            problems.append(f"invalid review.{tier}.{bad}")
            continue
        result[tier].update(fields)
    return problems


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


def policy(home, warnings=None):
    """Company policy TOML (pointer file, else the default path) over built-in defaults.

    An unreadable or unparseable file raises ValueError. An unknown or invalid key keeps its default and
    adds one warning line to `warnings` (printed to stderr when no list is given); every other key is kept.
    """
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
        problems = [f"invalid {key}"]
        if key == "review":
            problems = review_policy(value, result[key])
            valid = not problems
        elif key in ("reserve", "spare_resets"):
            valid = isinstance(value, dict) and all(
                name in VENDORS and _number(item, 100 if key == "reserve" else None) for name, item in value.items())
            result[key].update(value if valid else {})
        elif key in ("priority", "research_backlog"):
            valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
            result[key] = value if valid else result[key]
        elif key == "codex_ramp":
            valid = (isinstance(value, dict) and value.keys() == {"per_day", "ceiling"}
                     and _number(value["per_day"]) and value["per_day"] > 0 and _number(value["ceiling"], 100))
            result[key] = value if valid else None
        elif key == "blocked":
            valid = isinstance(value, dict) and all(
                name in VENDORS and isinstance(item, str) and item.strip() for name, item in value.items())
            result[key] = value if valid else {}
        else:
            valid = key in DEFAULTS and _number(value, 100 if key == "boost_five_hour_max" else None)
            problems = problems if key in DEFAULTS else [f"unknown key {key}"]
            if valid:
                result[key] = value
        for problem in [] if valid else problems:
            line = f"pace warning: {path}: {problem}; ignored, other keys kept"
            if warnings is None:
                print(line, file=sys.stderr)
            else:
                warnings.append(line)
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
        reading = _reading("claude", _stamp(week["at"]), week["utilization"] * 100, _epoch(week["resetsAt"]),
                        five["utilization"] * 100 if five else None,
                        _epoch(five["resetsAt"]) if five else None, now)
        return dict(reading, five_at=_stamp(five["at"]) if five else None)
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


def process_start(pid):
    """Linux start ticks identify a process even after its PID is reused."""
    try:
        fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] not in {"Z", "X"} else None
    except (OSError, ValueError, TypeError, IndexError):
        return None


def release(home, vendor, pid):
    if vendor in VENDORS and pid is not None:
        try:
            (_dir(home) / "running" / vendor / f"{pid}.json").unlink(missing_ok=True)
        except OSError:
            pass


def claim(home, vendor, kind, pid, now=None):
    """Record a started lane or review process, so the 5-hour rules can count what runs machine-wide."""
    now = now or datetime.now(timezone.utc)
    try:
        _write(_dir(home) / f"{vendor}-last-start.json", {"at": now.isoformat()})
        _write(_dir(home) / "running" / vendor / f"{pid}.json",
               {"kind": kind, "pid": pid, "start": process_start(pid), "at": now.isoformat()})
    except OSError:
        pass


def _claims(home, vendor):
    for path in (_dir(home) / "running" / vendor).glob("*.json"):
        entry = _json(path)
        try:
            at, pid = _stamp(entry["at"]), entry["pid"]
            if entry.get("start") and entry["start"] == process_start(pid):
                yield at, pid
                continue
        except (KeyError, TypeError, ValueError):
            pass
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def running(home, vendor):
    """Live lanes and reviews of this vendor, pruning dead and reused claims."""
    return sum(1 for _ in _claims(home, vendor))


def _five(reading, pol, now, ran):
    """5-hour verdict: exhausted at 100 - reserve; warning 15 points lower, or on a missing or stale reading.

    The reserve shrinks with the time left in the 5-hour window: reserve x (hours left / 5). A missing or stale
    reading keeps the full reserve. A vendor with no 5-hour window in its readings has none to pace: always ok.
    Otherwise unknown is never plenty.
    """
    reserve, used, has = pol["reserve"][reading["vendor"]], reading.get("five_hour"), reading["has_five"]
    stale = has and ran and reading["reason"] == "" and now - reading["at"] > STALE
    missing = has and used is None
    hours = (reading["five_resets"] - now) / timedelta(hours=1) if reading.get("five_resets") else 0
    if used is not None and not stale and 0 < hours < 5:
        reserve = reserve * hours / 5
    if used is not None and used >= 100 - reserve:
        return {"five": FIVE_OUT, "stale": False, "missing": False, "five_reserve": reserve}
    return {"five": FIVE_WARN if stale or missing or used is not None and used >= 100 - reserve - FIVE_BAND else "ok",
            "stale": stale, "missing": missing, "five_reserve": reserve}


def boost_pause(reading, pol, five):
    """Use the warning band's missing/stale rule only for vendors with a 5-hour window."""
    if not reading["has_five"]:
        return ""
    used = reading.get("five_hour")
    if five["missing"]:
        reason = "no current 5-hour reading"
    elif five["stale"]:
        reason = "5-hour reading stale"
    else:
        reason = f"5-hour {used:g} % ≥ {pol['boost_five_hour_max']:g} %" if used is not None and used >= pol["boost_five_hour_max"] else ""
    return f"boost paused: {reading['vendor']} {reason}" if reason else ""


def boost_pauses(home, now=None):
    return {vendor: item["boost_pause"] for vendor, item in assessments(home, now or datetime.now(timezone.utc))[1].items()
            if item["boost_pause"]}


def saved_boosts(home):
    """Unexpired saved boosts, including any paused by the current usage assessment."""
    result = {}
    for vendor, until in _json(home / ".local/state/task/boost.json").items():
        try:
            when = _stamp(until)
            if vendor in VENDORS and when.tzinfo and datetime.now(timezone.utc) < when:
                result[vendor] = until
        except (ValueError, AttributeError, TypeError):
            pass
    return result


def ramp(reading, pol, now):
    """Decision-23 Codex daily ramp: started day of the weekly window, allowance and whether use is past it.

    The window starts at the last weekly reset (resets_at - 7 days), so an early reset restarts at day 1.
    None for Claude, without a `codex_ramp` policy, or without a current reading.
    """
    rule = pol.get("codex_ramp")
    if reading["vendor"] != "codex" or not rule or reading["reason"]:
        return None
    day = min(7, max(1, (now - (reading["resets_at"] - WEEK)) // timedelta(days=1) + 1))
    allowance = min(rule["ceiling"], rule["per_day"] * day)
    return {"day": day, "allowance": allowance, "ceiling": rule["ceiling"], "per_day": rule["per_day"],
            "over": reading["weekly"] > allowance}


def ramp_hold(item, kind, exempt=""):
    """Refusal text for a new Codex launch past its ramp, or "" (exempt work may launch above it)."""
    found = item.get("ramp")
    if not found or not found["over"] or exempt or item.get("spend_down"):
        return ""
    return (f"codex past its daily ramp: weekly used {item['weekly']:.0f} % > allowance {found['allowance']:g} % "
            f"(day {found['day']}, {found['per_day']:g} %/day, ceiling {found['ceiling']:g} %); no new {kind}s on codex "
            "until the allowance grows; running work continues; --ramp-exempt hold|service|security|money for exempt work")


def assess(reading, pol, now, ran=False):
    """Add spare/limit and the pace verdict to a reading (see `task usage` for the meanings).

    `ran` says the vendor's lanes ran since the reading: an old reading is then stale.
    The 5-hour verdict ignores spare resets and the release below, which only touch the weekly window.
    In the final `under_use_hours` before the weekly reset the weekly reserve is released (limit 100 %).
    """
    vendor = reading["vendor"]
    spare = pol["spare_resets"][vendor] > 0
    left = None if reading["reason"] else (reading["resets_at"] - now) / timedelta(hours=1)
    released = left is not None and left <= pol["under_use_hours"]
    five = _five(reading, pol, now, ran)
    result = dict(reading, spare=spare, released=released, boost_pause=boost_pause(reading, pol, five),
                  limit=100 - (0 if spare or released else pol["reserve"][vendor]), ramp=ramp(reading, pol, now), **five)
    if reading["reason"]:
        return dict(result, verdict="unknown")
    elapsed = min(100.0, max(0.0, 100 * (1 - left / (WEEK / timedelta(hours=1)))))
    used, margin = reading["weekly"], pol["ahead_margin"]
    if used >= result["limit"]:
        verdict = EXHAUSTED
    elif used < pol["under_use_percent"] and left <= pol["under_use_hours"]:
        verdict = "under-used-near-reset"
    else:
        verdict = "ahead" if used > elapsed + margin else "under" if used < elapsed - margin else "on"
    return dict(result, verdict=verdict, left=left, elapsed=elapsed)


def _feed_time(value):
    """Aware datetime from an ISO stamp (a bare date is its UTC midnight) or an RSS date; None otherwise."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        when = _stamp(value.strip())
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "task-watch (decision-24 reset feed)"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read(1 << 20)


def _parse_resets(raw, rss):
    """Events of the primary JSON feed, or the RSS fallback's landed resets; raises on anything unparseable."""
    if rss:
        events = []
        for item in ElementTree.fromstring(raw).iter("item"):
            landed = _feed_time(item.findtext("pubDate"))
            if not landed:
                raise ValueError("RSS item without a date")
            events.append({"key": item.findtext("guid") or item.findtext("link") or landed.isoformat(),
                           "completed_at": landed.isoformat(), "event_status": "completed",
                           "reset_type": item.findtext("category") or "", "text": item.findtext("title") or ""})
        return events
    events = json.loads(raw)["events"]
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise ValueError("events is not a list of objects")
    return [dict({key: event.get(key) for key in RESET_FIELDS},
                 key=event.get("tweet_url") or f"{event.get('announced_at')}|{event.get('target_date')}") for event in events]


def refresh_resets(home, now, urls=None, fetch=None):
    """Poll the reset feed at most hourly (fallback on error) and keep its events in watcher state; never a model.

    TASK_RESET_FEEDS="<json url> <rss url>" replaces the sources; empty turns polling off. Never raises on a source.
    """
    urls = (os.environ["TASK_RESET_FEEDS"].split() if "TASK_RESET_FEEDS" in os.environ else RESET_FEEDS) if urls is None else urls
    path = _dir(home).parent / "codex-resets.json"
    state = _json(path)
    checked = _feed_time(state.get("checked_at"))
    if not urls or checked and now - checked < RESET_POLL:
        return state
    events, source, errors = None, "", []
    for url, rss in zip(urls, (False, True)):
        try:
            events, source = _parse_resets((fetch or _fetch)(url), rss), url
            break
        except (OSError, ValueError, KeyError, TypeError, ElementTree.ParseError, http.client.HTTPException) as error:
            # down, refused or unparseable: try the fallback, else fail closed
            errors.append(f"{url}: {type(error).__name__}")
    known = state.get("events") if isinstance(state.get("events"), dict) else {}
    for event in events or []:
        known[str(event.pop("key"))] = event
    state = {"checked_at": now.isoformat(), "ok": events is not None, "source": source,
             "error": "; ".join(errors), "events": dict(list(known.items())[-200:])}
    try:
        _write(path, state)
    except OSError as error:
        print(f"task-watch: codex reset feed state not written ({error.strerror}); task usage warns", file=sys.stderr)
    return state


def reset_feed(home, now):
    """Spend-down target (an announced reset ahead), newest landed reset and a feed warning, from watcher state."""
    state = _json(_dir(home).parent / "codex-resets.json")
    checked = _feed_time(state.get("checked_at"))
    warning = ("" if state.get("ok") is True and checked and now - checked <= RESET_FRESH else
               "pace warning: codex reset feed " + ("has no reading yet" if not state else "down or unparseable"
               if state.get("ok") is not True else f"not checked since {checked:%Y-%m-%d %H:%M} UTC" if checked
               else "unparseable") + "; no spend-down, the codex ramp applies")
    landed, ahead = None, []
    for event in (state.get("events") or {}).values() if isinstance(state.get("events"), dict) else ():
        if not isinstance(event, dict) or any(word in f"{event.get('timing_kind')} {event.get('event_status')}".lower()
                                              for word in NOT_ANNOUNCED):
            continue
        target, done = _feed_time(event.get("target_date")), _feed_time(event.get("completed_at"))
        done = done or (target if target and target <= now else None)
        if done and done <= now:
            landed = max(landed or done, done)
        elif target and target > now and _feed_time(event.get("announced_at")):
            ahead.append((target, _feed_time(event.get("announced_at"))))
    targets = [target for target, announced in ahead if not landed or landed < announced]
    return (None if warning else min(targets, default=None)), landed, warning


def _landed(reading, landed):
    """T-54's early-reset rule for a reset the feed saw land after the reading: the window restarts at day 1."""
    if not landed or reading["reason"] or reading["at"] >= landed or landed <= reading["resets_at"] - WEEK:
        return reading
    return dict(reading, weekly=0.0, resets_at=landed + WEEK, five_hour=None, five_resets=None, landed=landed)


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


def record_override(home, repo, task, model, tier, text, now=None, **fields):
    """Append a policy exception or risk change to the shared overrides ledger."""
    entry = dict(time=(now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
                 repo=repo, task=task, model=model, tier=tier, override=text, **fields)
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
                label = f"risk {entry['old_risk']} to {entry['risk']}" if entry.get("kind") == "risk_change" else entry.get("kind") or entry['model']
                found.append(f"{entry['repo']}/{entry['task']} {label} on {entry['tier']} "
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


@contextmanager
def command_readings():
    """Share one lazy assessment per HOME for command decisions; run logging still reads afresh."""
    token = _ASSESSMENTS.set({})
    try:
        yield
    finally:
        _ASSESSMENTS.reset(token)


def assessments(home, now):
    """Policy and one assessment per vendor, plus warnings; a broken policy file falls back to defaults."""
    cache = _ASSESSMENTS.get()
    if cache is not None and home in cache:
        return cache[home]
    notes = []
    try:
        pol = policy(home, notes)
    except ValueError as error:
        pol = json.loads(json.dumps(DEFAULTS))
        notes.append(f"pace warning: {error}; built-in defaults used")
    seen = {}
    spend, landed, _ = reset_feed(home, now)
    for vendor, item in readings(home, now).items():
        item = _landed(item, landed) if vendor == "codex" else item
        since = item.get("at")
        last = _json(_dir(home) / f"{vendor}-last-start.json").get("at", "")
        ran = bool(since) and (running(home, vendor) > 0 or last > since.isoformat())
        seen[vendor] = found = dict(assess(item, pol, now, ran), spend_down=spend if vendor == "codex" else None)
        if found["stale"]:
            notes.append(f"pace warning: {vendor} usage reading is {(now - since) / timedelta(minutes=1):.0f} min old "
                         "while its lanes ran; treated as the 5-hour warning band (one lane or review at a time)")
        if found["missing"]:
            notes.append(f"pace warning: {vendor} has no current 5-hour reading; "
                         "treated as the 5-hour warning band (one lane or review at a time)")
        note(home, found, now)
    if cache is not None:
        cache[home] = pol, seen, notes
    return pol, seen, notes


def _ahead(item):
    return item["verdict"] == "ahead" and not item["spare"]


def _room(home, vendor, item):
    """Room on both windows: not weekly exhausted or ahead, and the 5-hour window allows one more."""
    five = item["five"] != FIVE_OUT and (item["five"] != FIVE_WARN or not running(home, vendor))
    return item["verdict"] != EXHAUSTED and not _ahead(item) and five and not ramp_hold(item, "lane")


def five_caps(home, now=None):
    """Parallel lanes the 5-hour rules allow per vendor: 0 when exhausted, 1 in the warning band, else None."""
    _, seen, _ = assessments(home, now or datetime.now(timezone.utc))
    return {vendor: {FIVE_OUT: 0, FIVE_WARN: 1}.get(item["five"]) for vendor, item in seen.items()}


def gate(home, kind, vendor, tier, repo, key, now=None, exempt=""):
    """Pace check for one lane or review: which vendor, model hint, hold reason, notes.

    Returns vendor (maybe switched), hold (reason text or ""), cheaper (use the cheap routine model),
    spend (ISO reset time when the vendor is under-used near its reset), notes to print, and
    ramp_exempt ("" or the exemption that let a Codex launch past its ramp; the caller ledgers it).
    """
    now = now or datetime.now(timezone.utc)
    pol, seen, notes = assessments(home, now)
    out = {"vendor": vendor, "hold": "", "cheaper": False, "spend": "", "notes": notes, "ramp_exempt": "", "spend_down": ""}
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
            out["hold"] = (f"{vendor} 5-hour window used up ({found['five_hour']:.0f} % of {100 - found['five_reserve']:g} %); "
                           f"no new {kind}s there until the reset at {_when(found['five_resets'])}")
        else:
            out["hold"] = (f"{vendor} automated share used up ({found['weekly']:.0f} % of {found['limit']:.0f} %); "
                           f"no new {kind}s there")
    if not out["hold"] and found["five"] == FIVE_WARN and running(home, out["vendor"]):
        out["hold"] = (f"{out['vendor']} 5-hour window in the warning band"
                       f"{' (reading stale)' if found['stale'] else ' (no 5-hour reading)' if found['missing'] else ''}: "
                       f"{running(home, out['vendor'])} lane or review running; one at a time")
    if not out["hold"]:
        out["hold"] = ramp_hold(found, kind, exempt)
        if found.get("spend_down") and found.get("ramp") and found["ramp"]["over"]:
            out["spend_down"] = found["spend_down"].isoformat()
            notes.append(f"pace: codex in {SPEND_DOWN} until {_when(found['spend_down'])}; ramp and ceiling lifted (ledgered)")
        elif exempt and found.get("ramp") and found["ramp"]["over"]:
            out["ramp_exempt"] = exempt
            notes.append(f"pace: codex past its daily ramp ({found['weekly']:.0f} % > {found['ramp']['allowance']:g} %); "
                         f"launch allowed as ramp-exempt {exempt} work (ledgered)")
    if not out["hold"] and _ahead(found):
        out["cheaper"] = kind == "lane" and tier == "routine"
        top = _top(home, pol)
        if kind == "lane" and top and top != repo:
            out["hold"] = f"{vendor} is ahead of pace; only {top} gets new lanes"
    if not out["hold"] and found["verdict"] == "under-used-near-reset":
        if found["boost_pause"]:
            notes.append(found["boost_pause"])
        else:
            out["spend"] = found["resets_at"].isoformat()
    if out["hold"]:
        note(home, seen[vendor], now, f"{repo}/{key}")
        log(home, f"held {kind} {repo}/{key}: {out['hold']}", now)
    return out


def spend_down(home, vendor, now=None):
    """Decision-24: the announced reset's target while Codex is in spend-down, else None; never for Claude."""
    return assessments(home, now or datetime.now(timezone.utc))[1]["codex"]["spend_down"] if vendor == "codex" else None


def ramp_gate(home, kind, vendor, repo, key, exempt="", now=None):
    """Only the Codex ramp, for launches without the full pace gate (red-team): hold text and exemption used."""
    now = now or datetime.now(timezone.utc)
    found = assessments(home, now)[1][vendor]
    hold = ramp_hold(found, kind, exempt)
    if hold:
        note(home, found, now, f"{repo}/{key}")
        log(home, f"held {kind} {repo}/{key}: {hold}", now)
    return hold, exempt if exempt and found.get("ramp") and found["ramp"]["over"] else ""


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
    used_up = f"{reserve} used up"
    if item["released"]:
        until = f"reserve released until {_when(item['resets_at'])}"
        reserve, used_up = f"100 % weekly limit, {until}", f"100 % limit used up, {until}"
    if item["spare"]:
        first = f"{used_up if item['verdict'] == EXHAUSTED else reserve}; " if item["released"] else ""
        return f"{first}{SPARE}; no reserve, lanes not held back for pace"
    return {
        EXHAUSTED: f"{used_up}: no new lanes or reviews on {vendor}; routine lanes move to the other vendor if it has room",
        "ahead": f"{reserve}; routine lanes use {CHEAPER[vendor]}, no new batch starts on its own, "
                 f"new lanes only for {top or 'the top-priority repo with work'}",
        "under-used-near-reset": f"{reserve}; spend before the reset: "
                                 f"{item['boost_pause'] or 'boost on until it ends (stronger models on queued work)'}; "
                                 f"deeper reviews ({DEEPER[vendor]}) only for high risk after a recorded miss (--miss, decision-32); research backlog: "
                                 f"{', '.join(pol['research_backlog']) or 'none named'}",
    }.get(item["verdict"], f"{reserve}; normal")


def report(home, now=None):
    """Lines for `task usage`."""
    now = now or datetime.now(timezone.utc)
    pol, seen, lines = assessments(home, now)
    lines = list(lines)
    boosts = saved_boosts(home)
    for vendor in VENDORS:
        item = seen[vendor]
        if item["boost_pause"] and (vendor in boosts or item["verdict"] == "under-used-near-reset"):
            lines.append(item["boost_pause"])
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
        reserve, full = item["five_reserve"], pol["reserve"][vendor]
        scaled = f" (reserve {reserve:g} % of {full} %, scaled by the time left)" if reserve != full else ""
        limits = (f"exhausted at {100 - reserve:g} %, warning at {100 - reserve - FIVE_BAND:g} %{scaled}; {effect}" if item["has_five"]
                  else "no 5-hour window in the readings of the last 7 days; only the weekly rules apply")
        lines += [f"{vendor}: weekly used {item['weekly']:.0f} %, 5-hour used {five}",
                  f"  5-hour: {item['five'] if item['has_five'] else 'none'}{state}{reset}; {limits}",
                  f"  resets at {_when(item['resets_at'])}",
                  f"  reading {item['at']:%Y-%m-%d %H:%M} UTC, {age:.0f} min old; week elapsed {item['elapsed']:.0f} %",
                  f"  pace: {item['verdict']}",
                  f"  policy: {_applied(vendor, item, pol, _top(home, pol))}"]
        if item["spend_down"]:
            lines.append(f"  spend-down until {_when(item['spend_down'])}: announced reset ahead ({SPEND_DOWN}); ramp and "
                         "ceiling lifted for codex launches, every codex model and effort allowed (ledgered)")
        elif item["ramp"]:
            found = item["ramp"]
            lines.append(f"  ramp: day {found['day']}, allowance {found['allowance']:g} % ({found['per_day']:g} %/day, "
                         f"ceiling {found['ceiling']:g} %); {'past ramp: no new codex launches unless --ramp-exempt' if found['over'] else 'within ramp'}")
    if pol["codex_ramp"] and seen["codex"]["verdict"] == "unknown":
        lines.append("codex ramp: unknown without a current codex reading; not applied")
    return lines + [warning for warning in reset_feed(home, now)[2:] if warning] + fallback_lines(pol, seen)


def _states(vendor, item, pol):
    """Why a vendor is not normal for new work: past ramp, braked, near its 5-hour window, blocked."""
    found = []
    if item.get("ramp") and item["ramp"]["over"] and not item.get("spend_down"):
        found.append(f"past ramp (day {item['ramp']['day']}, {item['ramp']['allowance']:g} %)")
    if item["verdict"] == EXHAUSTED or _ahead(item):
        found.append("braked (" + ("automated share used up" if item["verdict"] == EXHAUSTED else "ahead of pace") + ")")
    if item["five"] == FIVE_OUT:
        found.append("5-hour window used up")
    elif item["five"] == FIVE_WARN:
        found.append("near its 5-hour window" + (" (reading stale)" if item["stale"] else " (no 5-hour reading)" if item["missing"] else ""))
    if pol["blocked"].get(vendor):
        found.append(f"blocked by a foundation defect ({pol['blocked'][vendor]})")
    return found


def fallback_lines(pol, seen):
    """One fallback line per vendor that is not normal; nothing when every vendor is normal."""
    states = {vendor: _states(vendor, seen[vendor], pol) for vendor in VENDORS}
    lines = []
    for vendor in VENDORS:
        if not states[vendor]:
            continue
        other = "codex" if vendor == "claude" else "claude"
        only_band = all(state.startswith("near its 5-hour") for state in states[vendor])
        builds = f"{other.capitalize()} lane" if not states[other] else "wait"
        reviews = "one at a time" if only_band else "wait"
        lines.append(f"{vendor} {', '.join(states[vendor])}: builds -> {builds}; reviews of {other.capitalize()} work -> {reviews}")
    return ["fallback:"] + ["  " + line for line in lines] if lines else []


def weekly_lines(home, now=None):
    return run_summary(home, 7, now) + [override_summary(home, now)]


def run_log(home, run=None, outcome=None, **fields):
    now = datetime.now(timezone.utc)
    root = _dir(home).parent
    reading = readings(home, now).get((run or fields).get("vendor"))
    reading = json.loads(json.dumps(reading, default=str)) if reading and not reading.get("reason") else None
    if run is None:
        run = dict(id=os.urandom(12).hex(), start=now.isoformat(), end=None, plan=os.environ.get("TASK_PLAN"),
                   effort="high", tier="routine", round=1 + sum(r.get("repo") == fields.get("repo") and r.get("task") == fields.get("task") and r.get("kind") == fields.get("kind") and r.get("outcome") in {"finished", "failed", "interrupted"} for r in map(json.loads, _text(root / "runs.jsonl").splitlines())), ccusage_tokens=None, before=reading) | fields
        _write(root / "pending-runs" / (run["id"] + ".json"), run)
        return run
    terminal = outcome in {"finished", "failed", "interrupted"}
    after = reading if terminal else run.get("after")
    before = run.get("before") or {}
    delta = {k: after[k] - before[k] if after and before.get(k) is not None and after.get(k) is not None and before.get(reset) == after.get(reset) else None
             for k, reset in (("weekly", "resets_at"), ("five_hour", "five_resets"))}
    row = dict(run, id=run["id"] if terminal else run["id"] + ":" + outcome, run_id=run["id"], type="run" if terminal else "outcome", end=now.isoformat(), after=after, usage_delta=delta, outcome=outcome, **fields)
    with (root / "runs.jsonl").open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        if not any(r.get("run_id", r["id"]) == run["id"] and (r["outcome"] in {"finished", "failed", "interrupted"} if terminal else r["outcome"] == outcome) for r in map(json.loads, stream)):
            stream.write(json.dumps(row) + "\n")
            stream.flush()
        if terminal:
            (root / "pending-runs" / (run["id"] + ".json")).unlink(missing_ok=True)

def run_outcome(home, repo, task, outcome):
    for row in list(map(json.loads, _text(_dir(home).parent / "runs.jsonl").splitlines())) + [_json(p) for p in (_dir(home).parent / "pending-runs").glob("*.json")]:
        if row.get("repo") == repo and row.get("task") == task and row.get("outcome") in {None, "finished", "failed", "interrupted"}:
            run_log(home, row, outcome.split(":", 1)[0], reason=outcome)

def run_summary(home, days=1, now=None):
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()
    rows = list(map(json.loads, _text(_dir(home).parent / "runs.jsonl").splitlines()))
    ended = {r["id"] for r in rows if r.get("outcome") in {"finished", "failed", "interrupted"}}
    rows += [dict(_json(p), outcome="unfinished") for p in (_dir(home).parent / "pending-runs").glob("*.json") if p.stem not in ended]
    lines = []
    for vendor in VENDORS:
        found = [r for r in rows if r.get("vendor") == vendor and (r.get("end") or r["start"]) >= cutoff]
        deltas = [(r.get("usage_delta") or {}).get("weekly") for r in found if r["outcome"] in {"finished", "failed", "interrupted"}]
        total = sum(d for d in deltas if d is not None) if any(d is not None for d in deltas) else None
        lines.append(f"{vendor} last {days}d: runs {len({r.get('run_id', r['id']) for r in found})}, weekly usage delta {total}, outcomes {dict(Counter(r['outcome'] for r in found))}")
    return lines
