#!/usr/bin/env python3
"""Check allow/prompt/forbidden with installed Codex; execute no Git/gh command.

The user's allow rules come from ~/.codex/rules/default.rules; the forbidden/prompt rules from
the root-owned /etc/codex/requirements.toml. `codex execpolicy check` reads only .rules files,
so the requirements' [rules] are rendered to the equivalent prefix_rule() calls; Codex merges
both the same way (every matching rule counts, the most restrictive decision wins).
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib

agent_home = Path.home()
os.environ["PATH"] = f"{agent_home}/.local/bin:{agent_home}/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin"
codex = agent_home / ".local/bin/codex"
assert codex.is_file(), "pinned Codex installation missing"
rules = agent_home / ".codex/rules/default.rules"
requirements = tomllib.loads(Path("/etc/codex/requirements.toml").read_text(encoding="utf-8"))
scratch = tempfile.TemporaryDirectory()  # removed when the interpreter exits
rendered = Path(scratch.name) / "requirements.rules"
rendered.write_text("".join(
    "prefix_rule(pattern = {}, decision = {}, justification = {})\n".format(
        json.dumps([token.get("token", token.get("any_of")) for token in rule["pattern"]]),
        json.dumps(rule["decision"]), json.dumps(rule["justification"]))
    for rule in requirements["rules"]["prefix_rules"]), encoding="utf-8")

def check(command, expected):
    result = subprocess.run(
        [str(codex), "execpolicy", "check", "--rules", str(rules), "--rules", str(rendered),
         "--", *command],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=True, timeout=20,
    )
    assert json.loads(result.stdout)["decision"] == expected, (command, result.stdout)

for executable in ("git", "/usr/bin/git"):
    for arguments in (["status", "--short"], ["push", "-u", "origin", "ci-fixture"],
                      ["fetch", "origin"], ["add", "safe.txt"], ["commit", "-m", "fixture"],
                      ["merge", "origin/main"], ["merge", "--no-edit", "origin/main"],
                      ["stash", "list"], ["switch", "task-fixture"], ["switch", "-c", "task-fixture"]):
        check([executable, *arguments], "allow")
    for arguments in (["reset", "--hard"], ["rebase", "--abort"],
                      ["cherry-pick", "--abort"], ["revert", "--abort"],
                      ["stash", "pop"], ["stash", "drop"], ["stash", "clear"]):
        check([executable, *arguments], "prompt")
    for arguments in (["push", "--force", "origin", "HEAD"],
                      ["push", "--force-with-lease", "origin", "HEAD"],
                      ["push", "-f", "origin", "HEAD"], ["push", "--delete", "origin", "task"]):
        check([executable, *arguments], "forbidden")
for executable in ("gh", "/usr/bin/gh"):
    for arguments in (["issue", "list"], ["pr", "create"], ["auth", "status"],
                      ["api", "repos/fixture"], ["repo", "view"], ["release", "list"]):
        check([executable, *arguments], "allow")
    for arguments in (["pr", "merge", "1"], ["repo", "delete", "fixture"],
                      ["repo", "edit", "fixture"], ["release", "delete", "fixture"],
                      ["secret", "list"], ["ruleset", "list"],
                      ["api", "-X", "DELETE", "repos/fixture"],
                      ["api", "--method", "DELETE", "repos/fixture"]):
        check([executable, *arguments], "forbidden")
# Literal prefixes cannot inspect later operands. Shared hook CI independently
# denies these forms; do not misrepresent engine-only allow as execution permission.
for command in (["git", "push", "origin", "HEAD", "--force"],
                ["git", "-C", ".", "reset", "--hard"],
                ["gh", "api", "repos/fixture", "--method=DELETE"]):
    check(command, "allow")
print("PASS: installed Codex routine allow, destructive prompt and forbidden Git/gh rules")
