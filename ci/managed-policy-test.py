#!/usr/bin/env python3
"""Hosted checks: agent guardrails live in root-owned managed config, and only guardrails.

Claude Code: system/claude-managed-guardrails.json -> /etc/claude-code/managed-settings.d/
10-agent-guardrails.json (deny rules, policy hooks). Codex: system/codex-requirements.toml ->
/etc/codex/requirements.toml (hooks feature, policy hooks, forbidden/prompt rules). Nothing in
either may limit bypass/auto mode, approval policy, sandbox choice or the user's own hooks.
"""
import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
OPT = "/opt/machine-bootstrap/current"
SCRIPTS = "/usr/local/lib/agent-policy"
# Exec form for Claude, the same argv as a string for Codex: absolute root-owned binaries,
# a fixed PATH (root-owned git and gitleaks) and an isolated interpreter (-I: no PYTHON*
# variables, user site-packages or script directory on sys.path).
PREFIX = ["/usr/bin/env", "-u", "LD_PRELOAD", "-u", "LD_LIBRARY_PATH", "PATH=/usr/local/bin:/usr/bin:/bin", "/usr/bin/python3", "-I"]
# Keys that would restrict modes, the sandbox choice or the user's own hooks/rules.
RESTRICTING = {"disableBypassPermissionsMode", "disableAutoMode", "defaultMode",
               "skipDangerousModePermissionPrompt", "allowManagedHooksOnly",
               "allowManagedPermissionRulesOnly", "disableAllHooks", "strictPluginOnlyCustomization",
               "managedSourcesBehavior", "allow", "ask", "additionalDirectories", "sandbox",
               "allow_managed_hooks_only", "allowed_approval_policies", "allowed_sandbox_modes",
               "allowed_permission_profiles", "default_permissions", "allowed_approvals_reviewers",
               "permissions", "remote_sandbox_config"}


def keys(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield path + (key,)
            yield from keys(item, path + (key,))
    elif isinstance(value, list):
        for item in value:
            yield from keys(item, path)


def repo_script(path: str) -> Path:
    assert path.startswith(SCRIPTS + "/"), path
    local = ROOT / "system/agent-policy" / Path(path).name
    assert local.is_file(), local
    return local


# --- Claude Code -------------------------------------------------------------------------
managed = json.loads((ROOT / "system/claude-managed-guardrails.json").read_text(encoding="utf-8"))
user = json.loads((ROOT / "home/dot_claude/settings.json").read_text(encoding="utf-8"))
assert set(managed) == {"permissions", "hooks"}, set(managed)
assert set(managed["permissions"]) == {"deny"}
for path in keys(managed):  # "permissions" only as the container of the deny list
    assert path == ("permissions",) or path[-1] not in RESTRICTING, path
deny = managed["permissions"]["deny"]
assert len(deny) == 88 and len(set(deny)) == 88, len(deny)
for rule in ("Bash(gh pr merge *)", "Bash(wsl.exe *)", "Read(~/.ssh/**)", "Read(~/.claude.json)",
             "Edit(//etc/**)", "Bash(git push --force*)", "Bash(/usr/bin/gh api * --method=DELETE*)"):
    assert rule in deny, rule
# One source of truth: the user file keeps preferences only.
assert "deny" not in user["permissions"] and "hooks" not in user, "guardrails left in user settings"
assert user["permissions"]["defaultMode"] == "acceptEdits"
assert not {"disableAllHooks", "disableBypassPermissionsMode"} & (set(user) | set(user["permissions"]))

claude_hooks = {}
for event, groups in managed["hooks"].items():
    for group in groups:
        for handler in group["hooks"]:
            assert set(handler) == {"type", "command", "args", "timeout"}, handler  # no shell field
            assert handler["type"] == "command"
            argv = [handler["command"], *handler["args"]]
            assert argv[:8] == PREFIX and len(argv) == 9, argv
            claude_hooks[(event, group.get("matcher"))] = repo_script(argv[8]).name
assert claude_hooks == {
    ("PreToolUse", "Bash|PowerShell|Write|Edit|MultiEdit"): "pre_tool_use.py",
    ("PreToolUse", "Bash|Write|Edit|MultiEdit"): "context_reminder.py",
    ("UserPromptSubmit", None): "context_reminder.py",
}, claude_hooks

# --- Codex ---------------------------------------------------------------------------------
requirements = tomllib.loads((ROOT / "system/codex-requirements.toml").read_text(encoding="utf-8"))
assert set(requirements) == {"features", "hooks", "rules"}, set(requirements)
for path in keys(requirements):
    assert path[-1] not in RESTRICTING, path
assert requirements["features"] == {"hooks": True}  # pins the hooks feature on, nothing else
codex_events = dict(requirements["hooks"])
assert codex_events.pop("managed_dir") == SCRIPTS
codex_hooks = {}
for event, groups in codex_events.items():
    for group in groups:
        assert set(group) <= {"matcher", "hooks"}, group
        for handler in group["hooks"]:
            assert set(handler) == {"type", "command", "timeout"} and handler["type"] == "command"
            argv = handler["command"].split(" ")
            assert argv[:8] == PREFIX and len(argv) == 9, argv
            codex_hooks[(event, group.get("matcher"))] = repo_script(argv[8]).name
assert codex_hooks == {
    ("PreToolUse", "^Bash$"): "pre_tool_use.py",
    ("PreToolUse", "^(apply_patch|Edit|Write)$"): "pre_tool_use.py",
    ("PermissionRequest", "^Bash$"): "pre_tool_use.py",
    ("UserPromptSubmit", None): "context_reminder.py",
}, codex_hooks
assert not (ROOT / "home/dot_codex/hooks.json").exists()
codex_user = tomllib.loads((ROOT / "home/dot_codex/config.toml").read_text(encoding="utf-8"))
assert "hooks" not in codex_user and "hooks" not in codex_user.get("features", {})

# Requirements rules: the schema Codex 0.159 accepts (codex-rs/config/src/requirements_exec_policy.rs).
prefix_rules = requirements["rules"]["prefix_rules"]
for rule in prefix_rules:
    assert set(rule) == {"pattern", "decision", "justification"}, rule
    assert rule["decision"] in {"prompt", "forbidden"}, rule  # "allow" is rejected there
    assert rule["justification"].strip() and rule["pattern"]
    for token in rule["pattern"]:
        assert len(token) == 1 and (token.get("token", "").strip() or
                                    (token.get("any_of") and all(t.strip() for t in token["any_of"]))), token

# The user's rules are allow-only preferences.
user_rules = []
text = (ROOT / "home/dot_codex/rules/default.rules").read_text(encoding="utf-8")
for match in re.finditer(r'prefix_rule\(pattern = (\[.*?\]), decision = "(\w+)"', text):
    pattern = ast.literal_eval(match.group(1))
    user_rules.append({"pattern": [{"any_of": t} if isinstance(t, list) else {"token": t} for t in pattern],
                       "decision": match.group(2)})
assert [r["decision"] for r in user_rules] == ["allow"] * 4, user_rules
RANK = {"allow": 0, "prompt": 1, "forbidden": 2}


def decide(command: list[str]) -> str | None:
    """Codex's merge: every matching prefix rule counts, the most restrictive one wins."""
    found = [rule["decision"] for rule in user_rules + prefix_rules
             if len(command) >= len(rule["pattern"]) and all(
                 word == token.get("token") or word in token.get("any_of", [])
                 for word, token in zip(command, rule["pattern"]))]
    return max(found, key=RANK.get) if found else None


# Same table the installed-Codex check (ci/git-gh-approval-test.py) uses after install.
for executable in ("git", "/usr/bin/git"):
    for args in ("status --short", "push -u origin ci-fixture", "fetch origin", "merge origin/main",
                 "stash list", "switch -c task-fixture", "push origin HEAD --force"):
        assert decide([executable, *args.split()]) == "allow", args
    for args in ("reset --hard", "rebase --abort", "cherry-pick --abort", "revert --abort",
                 "stash pop", "stash drop", "stash clear"):
        assert decide([executable, *args.split()]) == "prompt", args
    for args in ("push --force origin HEAD", "push --force-with-lease origin HEAD", "push -f origin HEAD",
                 "push --delete origin task", "push --mirror origin", "push --prune origin"):
        assert decide([executable, *args.split()]) == "forbidden", args
for executable in ("gh", "/usr/bin/gh"):
    for args in ("issue list", "pr create", "auth status", "api repos/fixture", "repo view", "release list"):
        assert decide([executable, *args.split()]) == "allow", args
    for args in ("pr merge 1", "repo delete fixture", "repo edit fixture", "release delete fixture",
                 "secret list", "ruleset list", "api -X DELETE repos/fixture", "api --method DELETE repos/fixture"):
        assert decide([executable, *args.split()]) == "forbidden", args

# --- The managed command line runs the policy (repo copy in place of /opt) -----------------
pre_argv = [*PREFIX, str(ROOT / "system/agent-policy/pre_tool_use.py")]
reminder_argv = [*PREFIX, str(ROOT / "system/agent-policy/context_reminder.py")]
with tempfile.TemporaryDirectory() as temp:
    cwd = Path(temp)
    # Negative control for -I: a planted module on PYTHONPATH must not load.
    planted = cwd / "planted"
    planted.mkdir()
    (planted / "json.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | {"PYTHONPATH": str(planted)}
    for command in ("wsl.exe -d AgentDev", "sudo true"):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                 "tool_input": {"command": command}, "cwd": str(cwd)}
        result = subprocess.run(pre_argv, input=json.dumps(event), capture_output=True, text=True,
                                cwd=cwd, env=env, check=True, timeout=30)
        assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny", command
    event = {"hook_event_name": "UserPromptSubmit", "prompt": "hello", "cwd": str(cwd)}
    result = subprocess.run(reminder_argv, input=json.dumps(event), capture_output=True, text=True,
                            cwd=cwd, env=env, check=True, timeout=30)
    assert "slim-workflow" in result.stdout, result.stdout

# --- Installers write root-owned files from the published revision ------------------------
claude_installer = (ROOT / "system/claude-managed.sh").read_text(encoding="utf-8")
codex_installer = (ROOT / "system/codex-managed.sh").read_text(encoding="utf-8")
for text, source, target in (
        (claude_installer, "system/claude-managed-guardrails.json", '"$dropins/10-agent-guardrails.json"'),
        (codex_installer, "system/codex-requirements.toml", '"$dir/requirements.toml"')):
    assert "require_root\nrequire_root_owned_policy\n" in text
    assert f'atomic_policy_install "$BOOTSTRAP_ROOT/{source}" \\\n  {target}' in text, source
common = (ROOT / "system/common.sh").read_text(encoding="utf-8")
assert 'mktemp "$(dirname -- "$target")/.agent-policy.XXXXXX"' in common
assert 'install -o root -g root -m 0644 "$source" "$temp" && mv -f -- "$temp" "$target"' in common
for path in ("/usr/bin/env", "/usr/bin/python3", "/usr/bin/git", "/usr/local/bin/gitleaks",
             "/usr/local/lib/agent-policy/pre_tool_use.py", "/usr/local/lib/agent-policy/context_reminder.py"):
    assert path in common, path
install = (ROOT / "install.sh").read_text(encoding="utf-8")
published = install.index('sudo mv -T -- "$current_temp/current" /opt/machine-bootstrap/current')
applied = install.index("chezmoi --source /opt/machine-bootstrap/current init --apply")
for script in ("claude-managed.sh", "codex-managed.sh"):
    line = f'sudo "$bash_cmd" {OPT}/system/{script}'
    assert install.count(script) == 1 and published < install.index(line) < applied, script
assert install.index("/usr/local/bin/gitleaks") < published
assert 'sudo ln -s "$public_source" "$current_temp/current"' in install
assert 'sudo install -d -o root -g root -m 0755 "$policy_dir"' in install
assert 'sudo mktemp "$policy_dir/.agent-policy.XXXXXX"' in install
assert 'sudo install -o root -g root -m 0644 "$public_source/system/agent-policy/$script" "$policy_temp"' in install
assert install.index('sudo mv -f -- "$policy_temp" "$policy_dir/$script"') < published

# Git environment overrides cannot redirect policy subprocesses; other environment stays.
spec = importlib.util.spec_from_file_location("managed_policy", ROOT / "system/agent-policy/pre_tool_use.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
with patch.dict(os.environ, {"GIT_DIR": "/untrusted", "GIT_CONFIG_COUNT": "1", "POLICY_CONTROL": "kept"}):
    with patch.object(policy.subprocess, "run") as run:
        policy.policy_subprocess(["git", "status"], check=True)
        assert not any(k.startswith("GIT_") for k in run.call_args.kwargs["env"])
        assert run.call_args.kwargs["env"]["POLICY_CONTROL"] == "kept"
        assert run.call_args.args == (["git", "status"],) and run.call_args.kwargs["check"] is True

# Denial logging is best-effort, records only the executable category, and cannot
# change a deny when the state path is broken.
assert {"review", "note", "cancel"} <= policy.PM_TASK_COMMANDS
with tempfile.TemporaryDirectory() as temp:
    home = Path(temp) / "home"
    home.mkdir()
    event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
             "tool_input": {"command": "sudo rm -rf /sensitive/argument"}, "cwd": temp}
    with patch.object(policy.Path, "home", return_value=home):
        policy.log_denial(event)
    log = home / ".local/state/agent-policy/denials.jsonl"
    entry = json.loads(log.read_text())
    assert entry["category"] == "sudo" and entry["rule_id"] in {
        "host.boundary", "git.policy", "path.boundary", "shell.policy", "secret.policy", "policy.denied"}
    assert "/sensitive/argument" not in log.read_text() and "-rf" not in log.read_text()
    broken_home = Path(temp) / "broken-home"
    broken_home.mkdir()
    (broken_home / ".local").write_text("not a directory")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | {"HOME": str(broken_home)}
    result = subprocess.run(pre_argv, input=json.dumps(event), capture_output=True, text=True,
                            cwd=temp, env=env, check=True, timeout=30)
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"

# chezmoi removes the agent's stale copies (empty remove_ entries), so one copy remains.
assert (ROOT / "home/dot_codex/remove_hooks.json").read_bytes() == b""
stale = ROOT / "home/dot_local/share/bootstrap/agent-policy"
assert sorted(p.name for p in stale.iterdir()) == ["remove_context_reminder.py", "remove_pre_tool_use.py"]
assert all(p.read_bytes() == b"" for p in stale.iterdir())
print("PASS: guardrails only in root-owned managed config; hooks run root-owned scripts; no mode limits")
