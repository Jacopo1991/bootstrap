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
}


def cli_limit(record, output):
    """Only a failed, completed CLI run's terminal diagnostic can be a limit."""
    builder, code = record.get("builder"), record.get("exit_code")
    if record.get("state") != "finished" or type(code) is not int or code == 0:
        return ""
    pattern = CLI_LIMIT.get(builder)
    lines = [line for line in output.splitlines() if line.strip()]
    # codex exec emits this after an error when --output-last-message is set.
    if builder == "codex" and lines and lines[-1].startswith("Warning: no last agent message; wrote empty content to "):
        lines.pop()
    last = lines[-1] if lines else ""
    if not pattern:
        return ""
    match = pattern.match(last) if builder == "claude" else pattern.fullmatch(last)
    return last if match else ""


def lane_limit(record, work):
    for name in ("final.md", "lane.log"):
        try:
            limit = cli_limit(record, (work / name).read_text(errors="replace"))
        except OSError:
            continue
        if limit:
            return limit
    return ""
