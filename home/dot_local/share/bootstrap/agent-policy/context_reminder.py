#!/usr/bin/env python3
"""Add short reminders to the agent's context through the native hook output.

UserPromptSubmit (Claude Code and Codex): a fixed block of at most eight lines.
PreToolUse (Claude Code): before a tool call that touches .github/workflows or changes
GitHub repository settings, tell the agent to read the github-ci skill first.

It only adds context; it never blocks or changes a call, and it reads nothing but the
hook event on stdin.
"""
from __future__ import annotations

import json
import re
import shlex
import sys

PROMPT_REMINDER = "\n".join((
    "Reminders (bootstrap hook):",
    "- Follow the slim-workflow skill.",
    "- Stop after two failed attempts at the same problem or at the time box; report what you have.",
    "- Report in chat and in the PR description.",
    "- Run git/gh network commands (fetch, pull, push, gh ...) on their own, never chained.",
    "- Verification: use the verify-ui skill.",
    "- Anything under .github or GitHub settings: read the github-ci skill first.",
))

WORKFLOW_REMINDER = (
    "This touches .github/workflows or GitHub repository settings. Read the github-ci skill "
    "first (triggers, concurrency, timeouts, minutes budget) before going on."
)

WORKFLOW_PATH = re.compile(r"(^|[\s/\\\"'=:])\.github[/\\]workflows([/\\]|$)")
# gh subcommands that change repository, Actions or ruleset settings, whatever their arguments.
SETTINGS_SUBCOMMANDS = {("repo", "edit"), ("repo", "archive"), ("repo", "rename"),
                        ("repo", "delete"), ("secret", "set"), ("secret", "delete"),
                        ("variable", "set"), ("variable", "delete"),
                        ("workflow", "enable"), ("workflow", "disable"), ("workflow", "run"),
                        ("ruleset", "*"), ("cache", "delete")}
GH_API_WRITE_FLAGS = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}
SETTINGS_API_PATH = re.compile(r"/?repos/[^/\s]+/[^/\s]+/(actions|branches|rulesets|hooks|"
                               r"collaborators|environments|pages|keys)|^/?(user|users/[^/]+)/settings|"
                               r"^/?repos/[^/\s]+/[^/\s]+$")


def prompt_event(event: dict) -> str | None:
    return PROMPT_REMINDER if event.get("hook_event_name") == "UserPromptSubmit" else None


VALUE_FLAGS = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "-H", "--header", "--input",
               "-q", "--jq", "-t", "--template", "--hostname", "--cache", "-R", "--repo"}


def positionals(tokens: list[str]) -> list[str]:
    """Non-flag tokens, skipping the separate value of flags that take one."""
    result, skip = [], False
    for token in tokens:
        if skip:
            skip = False
        elif token in VALUE_FLAGS:
            skip = True
        elif not token.startswith("-"):
            result.append(token)
    return result


def gh_changes_settings(tokens: list[str]) -> bool:
    """tokens start at the gh executable."""
    args = positionals(tokens[1:])
    if len(args) >= 2 and ((args[0], args[1]) in SETTINGS_SUBCOMMANDS
                           or (args[0], "*") in SETTINGS_SUBCOMMANDS):
        return True
    if args[:1] != ["api"]:
        return False
    methods = [tokens[i + 1].upper() for i, token in enumerate(tokens[:-1])
               if token in ("-X", "--method")]
    methods += [token.split("=", 1)[1].upper() for token in tokens
                if token.startswith(("--method=", "-X=")) and "=" in token]
    methods += [token[2:].upper() for token in tokens if re.fullmatch(r"-X[A-Za-z]+", token)]
    writes = any(m in ("POST", "PUT", "PATCH", "DELETE") for m in methods) or (
        not methods and any(token in GH_API_WRITE_FLAGS or token.startswith(("--field=", "--raw-field=",
                                                                              "--input="))
                            for token in tokens[2:]))
    endpoint = args[1] if len(args) > 1 else ""
    return writes and bool(SETTINGS_API_PATH.search(endpoint))


def bash_touches_github_ci(command: str) -> bool:
    if WORKFLOW_PATH.search(command):
        return True
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return False
    for index, token in enumerate(tokens):
        if token.rsplit("/", 1)[-1] == "gh" and gh_changes_settings(tokens[index:]):
            return True
    return False


def tool_event(event: dict) -> str | None:
    if event.get("hook_event_name") != "PreToolUse":
        return None
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    name = event.get("tool_name")
    if name in ("Write", "Edit", "MultiEdit"):
        path = tool_input.get("file_path")
        if isinstance(path, str) and WORKFLOW_PATH.search(path):
            return WORKFLOW_REMINDER
    elif name == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str) and bash_touches_github_ci(command):
            return WORKFLOW_REMINDER
    return None


def reminder_for(event: dict) -> str | None:
    return prompt_event(event) or tool_event(event)


def output_for(event: dict) -> dict | None:
    text = reminder_for(event)
    if text is None:
        return None
    return {"hookSpecificOutput": {"hookEventName": event["hook_event_name"],
                                   "additionalContext": text}}


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            return 0
        result = output_for(event)
    except Exception:
        return 0  # a reminder must never get in the way
    if result is not None:
        print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
