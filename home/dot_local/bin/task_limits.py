"""Terminal CLI usage-limit diagnostics shared by task and task-batch."""
import re


# Formats checked against Codex 0.159.2 / Claude Code 2.1.296.
CLI_LIMIT = {
    "codex": re.compile(r"ERROR: (?:You've hit your usage limit(?:\..*| for .+)|quota exceeded|"
                        r"(?:unexpected status |exceeded retry limit, last status: )"
                        r"429 Too Many Requests(?:.*))"),
    "claude": re.compile(r"(?:You've hit your (?:session limit|weekly limit|Opus limit|"
                         r"Sonnet limit|Fable limit|usage credit limit|limit|fast limit|"
                         r"monthly spend limit)|API Error: 429)"),
    # agy changelog (pinned release): quota errors end a print run with an AGY_ERROR JSON line or the credits message.
    "agy": re.compile(r"Your AI credits balance is too low to continue\.|"
                      r"AGY_ERROR: .*(?:RESOURCE_EXHAUSTED|\b429\b|[Qq]uota).*"),
}


# T-71: transient vendor errors. A lane retries once with backoff, then falls back to FALLBACK's model;
# still failing, it records `vendor-unavailable`, which never trips a plan's lane breaker.
VENDOR_UNAVAILABLE = re.compile(r"Selected model is at capacity|model is (?:currently )?(?:at capacity|overloaded)"
                                r"|overloaded_error|API Error: 5(?:00|02|03|29)|503 Service Unavailable", re.I)
VENDOR_BACKOFF = 30  # seconds before the one retry; TASK_VENDOR_BACKOFF overrides it in tests
FALLBACK = {"codex": "gpt-6.1-sol", "claude": "opus"}  # route's next model; a lane already on it uses the cheaper one


def last_line(builder, output):
    lines = [line for line in output.splitlines() if line.strip()]
    # codex exec emits this after an error when --output-last-message is set.
    if builder == "codex" and lines and lines[-1].startswith("Warning: no last agent message; wrote empty content to "):
        lines.pop()
    return lines[-1] if lines else ""


def vendor_unavailable(builder, code, output):
    last = last_line(builder, output) if code else ""
    return last if VENDOR_UNAVAILABLE.search(last) else ""


def cli_limit(record, output):
    """Only a failed, completed CLI run's terminal diagnostic can be a limit."""
    builder, code = record.get("builder"), record.get("exit_code")
    if record.get("state") != "finished" or type(code) is not int or code == 0:
        return ""
    pattern = CLI_LIMIT.get(builder)
    last = last_line(builder, output)
    if not pattern:
        return ""
    match = pattern.match(last) if builder == "claude" else pattern.fullmatch(last)
    return last if match else ""


# Setup #11: Codex lane sandboxes cannot open the NETLINK_ROUTE socket bwrap needs.
# The whole line is the denied command plus the denial text; anything more is another blocker.
BWRAP_DENIAL = re.compile(r"BLOCKED: (?:`[^`]+`|[^`—;&|]+?) [—-] (?:bwrap|bubblewrap(?:[’']s)?) "
                          r"(?:socket creation )?`?NETLINK_ROUTE`?(?: socket creation)?(?: denied| failed)?\.?", re.I)


def bwrap_denial(line):
    return bool(BWRAP_DENIAL.fullmatch(line.strip()))


def lane_limit(record, work):
    for name in ("final.md", "lane.log"):
        try:
            limit = cli_limit(record, (work / name).read_text(errors="replace"))
        except OSError:
            continue
        if limit:
            return limit
    return ""


# T-62 (company decision-27): why a lane round ended. First match wins; the four setup causes cost
# no round, anything else is the lane's own code-or-test round. Lines of the lane's prompt are ignored.
ROUND_CAUSES = (
    ("policy-denial", r"(denied|blocked) by (the )?(policy|hook|guard)|policy hook|hook (denied|blocked)"),
    ("missing-tool-or-cache", r"command not found|is not installed|not on PATH|cache\b.*(read-only|not writable)"
                              r"|(failed to (initialize|create|open|write)|cannot write)\b.*cache"),
    ("sandbox-or-permission", r"read-only file system|permission denied|operation not permitted|sandbox\w* (denied|blocked)"),
    ("lane-infrastructure", r"Lane launch failed|stream (error|disconnected)|connection (reset|refused)|overloaded"
                            r"|service unavailable"),
)
SETUP_CAUSES = {cause for cause, _ in ROUND_CAUSES}


def round_cause(record, work):
    if record.get("state") in {"tampered", "timed_out"}:
        return "code-or-test"
    if record.get("state") == "vendor-unavailable":
        return "lane-infrastructure"
    if record.get("exit_code") == 127:
        return "missing-tool-or-cache"
    if lane_limit(record, work):
        return "lane-infrastructure"
    try:
        known = set((work / "prompt.md").read_text(errors="replace").splitlines())
    except OSError:
        known = set()
    text = ""
    for name in ("lane.log", "final.md"):
        try:
            text += "\n".join(line for line in (work / name).read_text(errors="replace").splitlines()[-400:] if line not in known) + "\n"
        except OSError:
            continue
    return next((cause for cause, pattern in ROUND_CAUSES if re.search(pattern, text, re.I)), "code-or-test")
