#!/usr/bin/env python3
"""Hosted tests for native settings and policy-hook negative controls."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import atexit

from policy_test_env import isolate_policy_environment

isolate_policy_environment()

audit_state = tempfile.TemporaryDirectory(prefix="agent-policy-test-")
atexit.register(audit_state.cleanup)
os.environ["AGENT_POLICY_STATE_DIR"] = audit_state.name

ROOT = Path(__file__).resolve().parents[1]
assert (ROOT / ".chezmoiroot").read_text(encoding="utf-8").strip() == "home"
desktop_package = json.loads((ROOT / "home/dot_local/share/bootstrap/npm/package.json").read_text())
desktop_lock = json.loads((ROOT / "home/dot_local/share/bootstrap/npm/package-lock.json").read_text())
desktop_launcher = (ROOT / "home/dot_local/bin/executable_agentdev-shell-mcp").read_text()
assert desktop_package["dependencies"]["@wonderwhy-er/desktop-commander"] == "0.2.52"
assert desktop_lock["packages"]["node_modules/@wonderwhy-er/desktop-commander"]["version"] == "0.2.52"
assert "@wonderwhy-er/desktop-commander" in desktop_launcher and "@latest" not in desktop_launcher
windows_config_refs = []
for source in (ROOT / "windows", ROOT / "home"):
    for path in source.rglob("*"):
        if path.is_file() and path.suffix in {".ps1", ".sh", ".json", ".tmpl", ".toml"}:
            try:
                if "claude_desktop_config" in path.read_text(encoding="utf-8"):
                    windows_config_refs.append(str(path.relative_to(ROOT)))
            except UnicodeDecodeError:
                pass
assert not windows_config_refs, f"bootstrap unexpectedly manages Windows Claude Desktop config: {windows_config_refs}"
network_domains = ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "github.com",
                   "objects.githubusercontent.com", "codeload.github.com", "raw.githubusercontent.com",
                   "release-assets.githubusercontent.com", "openrouter.ai"]
codex = tomllib.loads((ROOT / "home/.chezmoitemplates/codex-config.toml").read_text(encoding="utf-8"))
assert codex["approval_policy"] == "on-request"
assert codex["approvals_reviewer"] == "user"
assert "sandbox_mode" not in codex  # Managed permission profiles select the sandbox.
# The network proxy injects variables after shell_environment_policy filtering; do not regress
# to that ineffective workaround for sandbox proxy variables.
assert not {"ALL_PROXY", "all_proxy"}.intersection(
    codex.get("shell_environment_policy", {}).get("exclude", []))
assert "sandbox_workspace_write" not in codex
# Proxy activation is separate from the selected profile's network policy.
assert codex["features"]["network_proxy"] is True
# Guardrails live in root-owned managed config (ci/managed-policy-test.py checks the shape):
# the hooks feature and hooks in /etc/codex/requirements.toml, none in the user config.
assert "hooks" not in codex and "hooks" not in codex["features"]
assert not (ROOT / "home/dot_codex/hooks.json").exists()
requirements = tomllib.loads((ROOT / "system/codex-requirements.toml").read_text(encoding="utf-8"))
assert requirements["permissions"]["lane"]["network"] == {
    "enabled": True, "allow_local_binding": True, "enable_socks5": False,
    "domains": {host: "allow" for host in network_domains}}, "lane network policy"
assert "network" not in requirements["permissions"]["review"], "review has no network"
assert requirements["features"] == {"hooks": True}
codex_hooks = {"hooks": {k: v for k, v in requirements["hooks"].items() if k != "managed_dir"}}
# The command-parsing PreToolUse/PermissionRequest hook was removed (decision-38); only the reminder stays.
assert set(codex_hooks["hooks"]) == {"UserPromptSubmit"}
assert "pre_tool_use" not in (ROOT / "system/codex-requirements.toml").read_text(encoding="utf-8")

claude = json.loads((ROOT / "home/dot_claude/settings.json").read_text(encoding="utf-8"))
# The deny rules and hooks are managed (root-owned drop-in); the user file keeps preferences.
assert "deny" not in claude["permissions"] and "hooks" not in claude
managed = json.loads((ROOT / "system/claude-managed-guardrails.json").read_text(encoding="utf-8"))
claude["permissions"]["deny"] = managed["permissions"]["deny"]
claude["hooks"] = managed["hooks"]
assert claude["permissions"]["defaultMode"] == "acceptEdits"
assert "ask" not in claude["permissions"]
assert claude["permissions"]["allow"] == ["Bash(git *)", "Bash(gh *)",
    "Bash(/usr/bin/git *)", "Bash(/usr/bin/gh *)"]
cache_paths = ["~/.cache/uv", "~/.cache/pre-commit", "~/.cache/pip", "~/.npm"]
assert claude["sandbox"] == {"enabled": True, "allowUnsandboxedCommands": False,
    # Bare names match only argument-less calls; keep the exact approved patterns.
    "failIfUnavailable": True,
    "excludedCommands": ["git", "gh", "git *", "gh *", "/usr/bin/git *", "/usr/bin/gh *"],
    "autoAllowBashIfSandboxed": False,
    # Package registries for `uv sync`; writable paths are the exact cache roots, nothing wider.
    "network": {"allowedDomains": network_domains},
    "filesystem": {"allowWrite": cache_paths}}
for forbidden in ("~/.local/state", "~/.codex", "~/.agents", "~/.ssh", "**/.git"):
    assert forbidden not in claude["sandbox"]["filesystem"]["allowWrite"]
tools_template = (ROOT / "home/.chezmoitemplates/tools.sh").read_text(encoding="utf-8")
assert 'mkdir -p "$HOME/.cache/uv" "$HOME/.cache/pre-commit" "$HOME/.cache/pip" "$HOME/.npm"' in tools_template
assert "Read(~/.config/gh/**)" in claude["permissions"]["deny"]
# T-48: the Antigravity sign-in folder is unreadable to Claude; Codex has no native deny here, so its
# guard is the policy hook on Bash and apply_patch (checked above), tested in ci/managed-policy-test.py.
assert "Read(~/.gemini/**)" in claude["permissions"]["deny"]
assert "Bash(git merge *)" not in claude["permissions"]["deny"]
assert "Bash(gh pr merge *)" in claude["permissions"]["deny"]
assert not any("*" in d for d in claude["sandbox"]["network"]["allowedDomains"])
# Documented Bash glob patterns cover destructive flags before and after
# ordinary remote/branch operands. Selector/quote variants are hook-tested below.
from fnmatch import fnmatchcase
def claude_denies(command):
    return any(fnmatchcase(command, entry[5:-1])
               for entry in claude["permissions"]["deny"] if entry.startswith("Bash("))
for executable in ("git", "/usr/bin/git"):
    for tail in ("push --force origin HEAD", "push origin HEAD --force-with-lease",
                 "push origin HEAD -f", "push origin -fextra",
                 "push --delete origin branch", "push origin branch --delete"):
        assert claude_denies(executable + " " + tail), tail
    # The native grammar cannot express "an argument starting with a colon", and a rule
    # such as 'git push * :* *' prints a warning in every session. The hook denies every
    # ':refspec' position instead (tested below), so no such native rule may exist.
    assert not claude_denies(executable + " push origin HEAD:refs/heads/task")
assert not any(entry.endswith(":*)") or " :" in entry for entry in claude["permissions"]["deny"]), \
    "refspec colon rules are enforced by the hook; native colon rules print warnings"
for executable in ("gh", "/usr/bin/gh"):
    for tail in ("repo delete fixture", "repo edit fixture", "release delete fixture",
                 "secret list", "ruleset list", "api -X DELETE repos/fixture",
                 "api repos/fixture -X DELETE", "api -XDELETE repos/fixture",
                 "api repos/fixture --method DELETE", "api repos/fixture --method=DELETE"):
        assert claude_denies(executable + " " + tail), tail
for command in ("git push -u origin HEAD", "gh issue list", "gh pr create",
                "/usr/bin/git push origin HEAD", "/usr/bin/gh api repos/fixture"):
    assert not claude_denies(command), command
rules = (ROOT / "home/dot_codex/rules/default.rules").read_text(encoding="utf-8")
assert rules.count('decision = "allow"') == 4
assert rules.count('decision = "prompt"') == 0
assert rules.count('decision = "forbidden"') == 0
# Each requirements rule covers git and /usr/bin/git (or gh and /usr/bin/gh) at once.
decisions = [rule["decision"] for rule in requirements["rules"]["prefix_rules"]]
assert decisions.count("prompt") == 2 and decisions.count("forbidden") == 6, decisions
# Read-deny: Codex runs with an admin permissions profile (not --sandbox, which cannot combine
# with one) that keeps the write scope of its built-in base and denies the Claude Read list plus
# credentials, private data and the excluded private repos. Only these profiles may be selected.
assert requirements["default_permissions"] == "lane"
assert requirements["allowed_permission_profiles"] == {"lane": True, "review": True}
assert {n: p["extends"] for n, p in requirements["permissions"].items()} == {"lane": ":workspace", "review": ":read-only"}
claude_reads = {x[len("Read("):-1].removesuffix("/**") for x in claude["permissions"]["deny"]
                if x.startswith("Read(~/")}
extra_denies = {"~/.claude/.credentials.json", "~/.gemini", "~/project-data",
                *(f"~/dev_workspace/{n}" for n in ("typo3-dkm", "typo3-dkm-plugin", "customer-harness", "photo-organizer")),
                *(f"~/cortex/cortex-kb-{n}" for n in ("typo3-dkm", "customer-harness", "photo-organizer"))}
assert {"~/.ssh", "~/.config/gh", "~/.codex/auth.json", "~/.npmrc"} <= claude_reads
for name, profile in requirements["permissions"].items():
    assert set(profile) == {"extends", "filesystem"} | ({"network"} if name == "lane" else set()), name
    assert profile["filesystem"] == dict.fromkeys(claude_reads | extra_denies, "deny"), name
launch = (ROOT / "home/dot_local/bin/executable_task").read_text(encoding="utf-8")
for cache in ("UV_CACHE_DIR", "PIP_CACHE_DIR", "PRE_COMMIT_HOME", "npm_config_cache"):
    assert cache in launch and "builder == \"codex\"" in launch
assert 'temporary / directory' in launch
smoke = (ROOT / "home/dot_local/bin/executable_task-smoke").read_text(encoding="utf-8")
uv_step = "uv run --no-project --with colorama==0.4.6 python smoke_check.py"
assert uv_step in smoke and "import colorama" in smoke
assert "uv run --no-project --with colorama==0.4.6 python smoke_nested.py" in smoke and '"--offline"' in smoke
assert "seed_uv_cache" in launch and "user_uv_cache" in launch
readme = (ROOT / "README.md").read_text(encoding="utf-8")
assert "private uv, pip, pre-commit and npm caches" in readme
# Codex launches use permission profiles, never Codex's --sandbox flag; only agy's own --sandbox (its
# terminal sandbox, decision-32 Gemini review route) may appear, and only inside agy_command.
launch_lines = launch.splitlines()
for index, line in enumerate(launch_lines):
    if "--sandbox" in line:
        owner = next(text for text in reversed(launch_lines[:index + 1]) if text.startswith("def "))
        assert owner.startswith("def agy_command("), (index + 1, line)
assert '"agy --mode plan --sandbox ' in launch
assert launch.count('default_permissions="lane"') == 1 and launch.count('default_permissions=\\"review\\"') == 2
assert launch.count('default_permissions="review"') == 1
assert "PreToolUse" in claude["hooks"]
assert not claude.get("mcpServers") and not claude.get("plugins")

assert not (ROOT / "system/agent-policy/pre_tool_use.py").exists()  # removed, decision-38
for path in ("home/dot_codex/AGENTS.md", "home/dot_claude/CLAUDE.md"):
    rules = (ROOT / path).read_text(encoding="utf-8").lower()
    for phrase in ("slim-workflow", "never push", "task start <repo> <branch>", "read the affected", "secret values",
                   "approved workspace roots", "scheduled project jobs remain deferred",
                   "customerharness and typo3"):
        assert phrase in rules, (path, phrase)
print("PASS: shared agent instructions carry the founder rules")

assert (ROOT / "home/dot_codex/AGENTS.md").read_text() == (ROOT / "home/dot_claude/CLAUDE.md").read_text()
modifier = subprocess.run(
    ["chezmoi", "--source", str(ROOT / "home"), "execute-template"],
    input=(ROOT / "home/dot_codex/modify_config.toml.tmpl").read_text(),
    text=True, capture_output=True, check=True).stdout
local = '[projects."/home/agent/dev_workspace/example"]\ntrust_level = "trusted"\n'
local += '[projects."/tmp/other"]\ntrust_level = "untrusted"\n'
local += 'enabled = false\n'
for old in ("", 'approval_policy = "never"\n' + local):
    result = subprocess.run(["python3", "-c", modifier], input=old, text=True,
                            capture_output=True, check=True).stdout
    parsed = tomllib.loads(result)
    assert {k: v for k, v in parsed.items() if k != "projects"} == codex
    assert parsed.get("projects", {}) == tomllib.loads(old).get("projects", {})
    again = subprocess.run(["python3", "-c", modifier], input=result, text=True,
                           capture_output=True, check=True).stdout
    assert again == result
print("PASS: identical rulebooks and idempotent Codex modifier preserving project trust entries")
