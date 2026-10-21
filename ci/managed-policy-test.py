#!/usr/bin/env python3
"""Hosted checks: agent guardrails live in root-owned managed config, and only guardrails.

Claude Code: system/claude-managed-guardrails.json -> /etc/claude-code/managed-settings.d/
10-agent-guardrails.json (deny rules, policy hooks). Codex: system/codex-requirements.toml ->
/etc/codex/requirements.toml (hooks feature, policy hooks, forbidden/prompt rules). Nothing in
either may limit bypass/auto mode, approval policy, sandbox choice or the user's own hooks.
"""
import ast
import atexit
import io
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib
from unittest.mock import patch

from policy_test_env import isolate_policy_environment

isolate_policy_environment()

audit_state = tempfile.TemporaryDirectory(prefix="agent-policy-test-")
atexit.register(audit_state.cleanup)
os.environ["AGENT_POLICY_STATE_DIR"] = audit_state.name

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
            assert argv[:8] == PREFIX, argv
            if argv[8].endswith("pre_tool_use.py"):
                assert argv[9:] == ["--vendor", "claude"], argv
            else:
                assert len(argv) == 9, argv
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
            assert argv[:8] == PREFIX, argv
            if argv[8].endswith("pre_tool_use.py"):
                assert argv[9:] == ["--vendor", "codex"], argv
            else:
                assert len(argv) == 9, argv
            codex_hooks[(event, group.get("matcher"))] = repo_script(argv[8]).name
assert codex_hooks == {
    ("PreToolUse", "^Bash$"): "pre_tool_use.py",
    ("PreToolUse", "^(apply_patch|Edit|Write)$"): "pre_tool_use.py",
    ("PermissionRequest", "^Bash$"): "pre_tool_use.py",
    ("UserPromptSubmit", None): "context_reminder.py",
}, codex_hooks
assert not (ROOT / "home/dot_codex/hooks.json").exists()
codex_user = tomllib.loads((ROOT / "home/.chezmoitemplates/codex-config.toml").read_text(encoding="utf-8"))
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
codex_pre_argv = [*pre_argv, "--vendor", "codex"]
reminder_argv = [*PREFIX, str(ROOT / "system/agent-policy/context_reminder.py")]
with tempfile.TemporaryDirectory() as temp:
    cwd = Path(temp)
    # Negative control for -I: a planted module on PYTHONPATH must not load.
    planted = cwd / "planted"
    planted.mkdir()
    (planted / "json.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    hook_home = cwd / "hook-home"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | {
        "PYTHONPATH": str(planted), "HOME": str(hook_home),
        "AGENT_POLICY_STATE_DIR": str(hook_home / ".local/state/agent-policy")}
    for command in ("wsl.exe -d AgentDev", "sudo true"):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                 "tool_input": {"command": command}, "cwd": str(cwd)}
        result = subprocess.run(pre_argv, input=json.dumps(event), capture_output=True, text=True,
                                cwd=cwd, env=env, check=True, timeout=30)
        assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny", command
    codex_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": "sudo true"}, "cwd": str(ROOT)}
    result = subprocess.run(codex_pre_argv, input=json.dumps(codex_event), capture_output=True,
                            text=True, cwd=cwd, env=env, check=True, timeout=30)
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    codex_entry = json.loads((hook_home / ".local/state/agent-policy/denials.jsonl").read_text().splitlines()[-1])
    assert codex_entry["vendor"] == "codex" and codex_entry["rule_id"] == "host.command_boundary"
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

# Script and recipe controls inspect inert fixture text; no denied command runs.
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    def judge(command):
        return policy.evaluate({"tool_name": "Bash", "cwd": temp,
                                "tool_input": {"command": command}}, (root,))

    def fixture(name, body):
        path = root / name
        path.write_text(body)
        path.chmod(0o755)
        return path

    def fixture_git(*args):
        return subprocess.run(["git", *args], cwd=root, check=True,
                              capture_output=True, text=True).stdout.strip()

    fixture_git("init", "-q", "-b", "main")
    fixture_git("config", "user.name", "Policy Fixture")
    fixture_git("config", "user.email", "policy@example.invalid")
    fixture("justfile", "check:\n    env -i PATH=\"$PATH\" bash ci/lint.sh\n")
    fixture("Makefile", "check:\n\t$(CC) app.c\n")
    fixture("package.json", '{"scripts":{"check":"node build.js"}}\n')
    (root / "ci").mkdir()
    fixture("ci/lint.sh", "#!/bin/sh\necho safe\n")
    fixture("build.js", "console.log('safe');\n")
    fixture_git("add", ".")
    fixture_git("commit", "-qm", "base recipes")
    fixture_git("update-ref", "refs/remotes/origin/main", "HEAD")
    fixture_git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    fixture_git("checkout", "-qb", "lane")
    for command in ("just check", "make check", "npm run check", "pnpm run check"):
        assert judge(command), command  # Unchanged is not an execution exemption.
    assert judge("task check") is None
    fixture("allowed.sh", "#!/bin/sh\n# Plain commands keep working.\necho safe\npwd\n")
    fixture("denied.sh", "#!/bin/bash\ngit clone https://example.invalid/repo stolen\n")
    fixture("nested.sh", "bash denied.sh\n")
    fixture("sourced.sh", ". ./denied.sh\n")
    for command in ("bash allowed.sh", "sh -- allowed.sh", "zsh allowed.sh",
                    "dash -- allowed.sh", "./allowed.sh", str(root / "allowed.sh"),
                    "source ./allowed.sh", ". ./allowed.sh", "env timeout 3 bash allowed.sh"):
        assert judge(command) is None, (command, judge(command))
    for name in ("denied.sh", "nested.sh", "sourced.sh"):
        for command in (f"bash {name}", f"./{name}", f". ./{name}", f"source ./{name}"):
            assert judge(command), command
    for body in ("sudo true\n", "echo ok; git clone x y\n", "bash -c true\n",
                 "for x in a; do echo x; done\n", "echo $(id)\n", "$PROGRAM\n",
                 "echo ok\\\n; sudo true\n",
                 "pushd child\necho safe\n", "hash -p ./denied.sh echo\necho safe\n",
                 "noglob sudo true\n", "repeat 2 sudo true\n",
                 ">out sudo true\n",
                 "f() { sudo true; }\nf\n", "#!/usr/bin/python3\nprint('safe')\n",
                 ". ./cycle.sh\n"):
        fixture("cycle.sh", body)
        assert judge("bash cycle.sh"), body
    (root / "binary").write_bytes(b"\x7fELF\x00fixture")
    (root / "alias.sh").symlink_to(root / "denied.sh")
    fixture("bash", "sudo true\n")
    fixture("env", "sudo true\n")
    fixture("timeout", "sudo true\n")
    fixture("custom-shell.sh", f"#!{root}/bash\necho safe\n")
    (root / "git-alias").symlink_to("/usr/bin/git")
    os.mkfifo(root / "fifo")
    for command in ("bash missing.sh", "./missing.sh", "bash binary", "./binary",
                    "bash alias.sh", "bash fifo", "sh -s", "bash", "bash --rcfile allowed.sh",
                    "source allowed.sh", "./bash allowed.sh", "./custom-shell.sh",
                    "./git-alias clone x y", "./env echo safe", "./timeout 3 echo safe"):
        assert judge(command), command
    (root / "child").mkdir()
    fixture("cd.sh", "cd child\necho safe\n")
    assert judge("bash cd.sh") is None
    assert judge(". ./cd.sh")
    fixture("cd.sh", "cd child\nbash ../denied.sh\n")
    assert judge("bash cd.sh")
    for command in ("touch allowed.sh; bash allowed.sh",
                    "printf safe > allowed.sh; ./allowed.sh",
                    "cp denied.sh allowed.sh; source ./allowed.sh"):
        assert judge(command), command
    with patch.object(policy, "program_text", side_effect=PermissionError):
        assert judge("bash allowed.sh")
    fixture("huge.sh", "x" * (policy.PROGRAM_LIMIT + 1))
    assert judge("./huge.sh")
    fixture("justfile", "check:\n    git clone https://example.invalid/repo stolen\n")
    assert judge("just check")
    fixture_git("add", "justfile")
    assert judge("just check")  # staged
    fixture_git("commit", "-qm", "changed recipe")
    assert judge("just check")  # committed on lane, still changed from merge base
    fixture("justfile", "check: prep\n    bash allowed.sh\nprep:\n    echo safe\n")
    assert judge("just check") is None
    fixture("justfile", "check: prep\n    echo safe\nprep:\n    bash denied.sh\n")
    assert judge("just check")
    for body in ("check:\n    just check\n", "import 'extra.just'\ncheck:\n    echo safe\n",
                 "set shell := ['bash', '-c']\ncheck:\n    echo safe\n",
                 "check arg:\n    {{arg}}\n", "check:\n    echo `id`\n",
                 "check:\n    -sudo true\n", "check:\n    @-git clone x y\n",
                 "check:\n    #!/usr/bin/python3\n    print('safe')\n"):
        fixture("justfile", body)
        assert judge("just check"), body
    fixture("justfile", "check:\n    echo safe\n")
    for command in ("just --justfile other check", "just --shell evil check",
                    "just check x=y", "make -f other", "npm run --prefix other check",
                    "pnpm run --filter other check",
                    "npm --prefix other run check", "pnpm --dir other run check", "pnpm check",
                    "JUST_DOTENV_COMMAND=evil just check", "JUST_JUSTFILE=other just check",
                    "MAKEFLAGS=evil make check", "npm_config_script_shell=evil npm run check"):
        assert judge(command), command
    for name, command in ((".env", "just check"), (".npmrc", "npm run check"),
                          (".pnpmfile.cjs", "pnpm run check")):
        fixture(name, "fixture=changed\n")
        assert judge(command), (name, command)
        (root / name).unlink()
    fixture("Makefile", "check:\n\techo safe\n")
    assert judge("make check")  # No partial Make parser.
    fixture("package.json", '{"scripts":{"check":"echo safe","precheck":"bash denied.sh"}}')
    for command in ("npm run check", "pnpm run check"):
        assert judge(command), command
    fixture("package.json", '{"scripts":{"check":"bash allowed.sh","precheck":"echo ready"}}')
    for runner in ("npm", "pnpm", "yarn", "bun"):
        assert judge(f"{runner} run check") is None
    fixture_git("update-ref", "-d", "refs/remotes/origin/main")
    assert judge("just check") is None  # Local main is a valid fallback.

    # Assignment controls apply to typed input, env wrappers and judged files.
    for name in ("CDPATH", "BASH_ENV", "ENV", "PATH", "IFS", "GLOBIGNORE",
                 "PROMPT_COMMAND", "SHELLOPTS", "ZDOTDIR", "FPATH", "cdpath", "path"):
        for command in (f"{name}=evil", f"{name}+=evil", f"{name}[0]=evil",
                        f"{name}=evil bash allowed.sh",
                        f"env {name}=evil bash allowed.sh"):
            assert judge(command), command
        fixture("assignment.sh", f"{name}=evil\n")
        assert judge("bash assignment.sh"), name
        assert judge("source ./assignment.sh"), name
    (root / "sub").mkdir()
    (root / "evil/sub").mkdir(parents=True)
    fixture("sub/run.sh", "echo safe\n")
    fixture("evil/sub/run.sh", "git clone x y\n")
    fixture("assignment.sh", f"CDPATH={root}/evil\n")
    for command in (f"CDPATH={root}/evil; cd sub && bash run.sh",
                    "source ./assignment.sh && cd sub && bash run.sh"):
        assert judge(command), command
    assert judge(">out.txt") is None
    for command in ("echo hi >/dev/null && bash allowed.sh",
                    ">/dev/null && bash allowed.sh",
                    "echo hi 2>/dev/null && bash allowed.sh"):
        assert judge(command) is None, (command, judge(command))
    for command in ("bash allowed.sh && bash allowed.sh",
                    "just check && just check", "echo hi >out.txt && bash allowed.sh",
                    "touch allowed.sh && . ./allowed.sh"):
        assert judge(command), command
    # Package lifecycle and fetch-and-run controls apply across supported runners.
    for runner in ("npm", "pnpm", "yarn", "bun"):
        fixture("package.json", '{"scripts":{"preinstall":"bash denied.sh"}}')
        for action in ("install", "ci", "run check"):
            assert judge(f"{runner} {action}"), (runner, action)
        fixture("package.json", '{"scripts":{"check":"echo safe","prepare":"bash allowed.sh"}}')
        actions = ("install", "ci", "run check") if runner in {"npm", "pnpm"} else (
            "install", "run check")
        for action in actions:
            assert judge(f"{runner} {action}") is None, (runner, action)
        if runner in {"yarn", "bun"}:
            for action in ("ci", "test", "list", "update", "check"):
                assert judge(f"{runner} {action}"), (runner, action)
        for action in ("exec echo safe", "dlx anything", "x anything", "create anything"):
            assert judge(f"{runner} {action}"), (runner, action)
        for action in ("run denied.sh", "run missing", "run"):
            assert judge(f"{runner} {action}"), (runner, action)
    assert judge("bun test")
    assert judge("bunx anything")
    assert judge("pnpx anything")
    assert judge("yarnpkg dlx anything")
    assert judge("yarnpkg run check") is None
    (root / "node_modules/.bin").mkdir(parents=True)
    fixture("node_modules/.bin/local-tool", "#!/bin/sh\necho safe\n")
    assert judge("npx --no-install local-tool") is None
    assert judge("npx --no local-tool") is None
    for command in ("npx local-tool", "npx --no-install missing", "npx --yes local-tool",
                    "npx --no-install --package=other local-tool"):
        assert judge(command), command
    fixture("node_modules/.bin/local-tool", "#!/bin/sh\ngit clone x y\n")
    assert judge("npx --no-install local-tool")
    print("PASS: recursive scripts and changed recipes denied; allowed scripts and base recipes pass")

# Local-only bases, trusted recipes and transitive gate inputs.
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    fixture_git("init", "-q", "-b", "main")
    fixture_git("config", "user.name", "Policy Fixture")
    fixture_git("config", "user.email", "policy@example.invalid")
    fixture("justfile", 'check:\n    bash gate.sh\n')
    fixture("Makefile", "check:\n\tbash gate.sh\n")
    fixture("package.json", '{"scripts":{"check":"bash gate.sh","prepare":"bash gate.sh"}}')
    fixture("gate.sh", "#!/bin/sh\nbash child.sh\n")
    fixture("check.py", "#!/usr/bin/python3\nprint('safe')\n")
    fixture("child.sh", "#!/bin/sh\necho safe\n")
    fixture("source.sh", "#!/bin/sh\necho safe\n")
    fixture("direct.sh", "#!/bin/sh\necho safe\n")
    (root / "sub").mkdir()
    fixture("sub/run.sh", "#!/bin/sh\necho safe\n")
    fixture_git("add", ".")
    fixture_git("commit", "-qm", "trusted gate")
    fixture_git("checkout", "-qb", "lane")
    commands = ("just check", "npm run check", "pnpm run check",
                "yarn run check", "bun run check", "npm install", "pnpm ci")
    for command in commands:
        assert judge(command) is None, (command, judge(command))
    assert judge("make check")
    assert judge("task check") is None
    for script in ("gate.sh", "child.sh"):
        original = (root / script).read_text()
        fixture(script, "#!/bin/sh\ngit clone x y\n")
        for command in commands:
            assert judge(command), (script, command)
        fixture_git("add", script)
        assert judge("just check")  # staged changed script
        fixture_git("commit", "-qm", "changed script")
        assert judge("just check")  # committed changed script
        fixture(script, original)
    fixture_git("add", ".")
    fixture_git("commit", "-qm", "restore trusted scripts")
    fixture("check.py", "#!/usr/bin/python3\nprint('changed')\n")
    fixture("gate.sh", "#!/bin/sh\npython3 -I check.py\n")
    assert judge("just check")  # Interpreter flags must not hide changed inputs.
    fixture("gate.sh", "#!/bin/sh\nbash child.sh\n")
    fixture("check.py", "#!/usr/bin/python3\nprint('safe')\n")
    fixture_git("branch", "-m", "main", "master")
    assert judge("just check") is None  # second local default
    fixture_git("branch", "-m", "master", "trunk")
    fixture_git("checkout", "-qb", "source-lane", "trunk")
    assert judge("just check") is None  # branch creation reflog names trunk
    fixture_git("checkout", "trunk")
    fixture_git("checkout", "-qb", "head-lane")
    assert judge("just check") is None  # Created from HEAD, checkout names trunk
    fixture_git("branch", "-D", "trunk")
    reason = judge("just check")
    assert reason and "No recipe base found" in reason, reason
    print("PASS: local default/source bases and changed trusted gate inputs")

# Round 4: typed commands, judged files and unchanged recipes share strict checks.
# All dangerous bodies are inert strings; only the safe positive recipe executes.
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    fixture_git("init", "-q", "-b", "main")
    fixture_git("config", "user.name", "Policy Fixture")
    fixture_git("config", "user.email", "policy@example.invalid")
    (root / "sub").mkdir()
    (root / "evil" / "sub").mkdir(parents=True)
    fixture("sub/run.sh", "echo safe\n")
    fixture("evil/sub/run.sh", "git clone x y\n")
    bad = (
        f"printf -v CDPATH {root}/evil; cd sub && bash run.sh",
        f"read CDPATH <<< {root}/evil; cd sub && bash run.sh",
        "printf -v target value", "printf -vCDPATH evil", 'printf "$option" target value',
        "printf >/dev/null -v CDPATH evil", "printf 2>/dev/null -v CDPATH evil",
        ">/dev/null printf -v CDPATH evil", "2>/dev/null read CDPATH",
        ">/dev/null cd sub && bash run.sh", "let CDPATH=1",
        "read target", "read -a target", "mapfile target", "readarray target",
        "getopts a target", "declare CDPATH=evil", "typeset CDPATH=evil",
        "local CDPATH=evil", "export CDPATH=evil", "readonly CDPATH=evil",
        "declare -n target=CDPATH", "hash -p ./evil/bash bash", "hash -r",
        "enable -f evil.so evil", "alias cd=evil", "shopt -s cdable_vars",
        "set -o posix; echo safe", "set -o pipefail; echo safe",
        "builtin printf -v CDPATH evil", "command printf -v CDPATH evil",
        "cd -P sub && bash run", "cd -- sub && bash run.sh",
        "pushd sub; bash run.sh", "popd; bash run.sh", 'cd "$target" && bash run.sh',
        "cd && bash run.sh", "bash run", 'bash "$script"',
    )
    for body in bad:
        assert judge(body), body
        for wrapper in ("builtin", "command", "command --", "env"):
            assert judge(f"{wrapper} {body}"), (wrapper, body)
        fixture("probe.sh", body + "\n")
        assert judge("bash probe.sh"), body
        assert policy.trusted_script_changes(body, temp, root, (root,), 0), body
        fixture("justfile", "check:\n    " + body + "\n")
        fixture("package.json", json.dumps({"scripts": {"check": body}}))
        fixture_git("add", ".")
        fixture_git("commit", "-qm", "unchanged review scenario")
        assert not policy.recipe_changed(root / "justfile", root)
        for runner in ("just check", "npm run check", "pnpm run check"):
            assert judge(runner), (runner, body)
        assert judge("task check") is None
    for body in ("run bash sub/run.sh", "python3 check.py", "node build.js"):
        assert policy.trusted_script_changes(body, temp, root, (root,), 0), body
    for body in ("echo safe", "printf '%s' safe", "printf '%s' -value", "printf -- -v",
                 "printf '[%s]\\n' x", "printf '{\"ok\":true}\\n'",
                 "printf '* ? ~ { } [ ]\\n'", 'printf "[*?~{}]\\n"',
                 "echo '[*?~{}]'", 'echo "[*?~{}]"',
                 "cat sub/run.sh", "command -v bash"):
        assert judge(body) is None, body
        fixture("probe.sh", body + "\n")
        assert judge("bash probe.sh") is None, body
        fixture("justfile", "check:\n    " + body + "\n")
        fixture("package.json", json.dumps({"scripts": {"check": body}}))
        fixture_git("add", ".")
        fixture_git("commit", "-qm", "unchanged positive print scenario")
        for runner in ("just check", "npm run check", "pnpm run check"):
            assert judge(runner) is None, (runner, body)
    for body in ("printf [ab]", "printf {a,b}", "printf *", "printf ?", "printf ~"):
        assert judge(body), body
    # Round 6: escaped quotes outside quotes cannot hide an unquoted glob.
    # Inspect these payloads only; never run their redirects or expansions.
    for body in (r"printf >\' ?v 'PATH' \'", r'printf >\" ?v "PATH" \"',
                 r"printf <$'\'' ?v PATH \'", r"printf safe\ word",
                 "printf '%s' < input", "printf '%s' *",
                 r"echo safe\ word", "echo safe < input"):
        assert judge(body), body
        fixture("probe.sh", body + "\n")
        assert judge("bash probe.sh"), body
        fixture("justfile", "check:\n    " + body + "\n")
        fixture("package.json", json.dumps({"scripts": {"check": body}}))
        fixture_git("add", ".")
        fixture_git("commit", "-qm", "escaped quote printf negative")
        for runner in ("just check", "npm run check", "pnpm run check"):
            assert judge(runner), (runner, body)
    # Echo cannot assign, but judged bodies must still reject unquoted globs.
    for body in (r"echo >\' ?v 'PATH' \'", r'echo >\" ?v "PATH" \"'):
        fixture("probe.sh", body + "\n")
        assert judge("bash probe.sh"), body
        fixture("justfile", "check:\n    " + body + "\n")
        fixture("package.json", json.dumps({"scripts": {"check": body}}))
        fixture_git("add", ".")
        fixture_git("commit", "-qm", "escaped quote echo negative")
        for runner in ("just check", "npm run check", "pnpm run check"):
            assert judge(runner), (runner, body)
    # Round 7 PM ruling: later unquoted globs also fail closed.
    fixture("justfile", "check:\n    printf '%s\\n' safe\n")
    fixture_git("add", "justfile")
    fixture_git("commit", "-qm", "modelled positive recipe")
    assert judge("just check") is None
    result = subprocess.run(["just", "check"], cwd=root, check=True, capture_output=True, text=True)
    assert result.stdout == "safe\n", result
    print("PASS: Round 4 builtin/cwd controls, unchanged recipes fail closed, task check allowed")

# #76: entering a scratch Git repository must not move the task boundary.
with tempfile.TemporaryDirectory() as temp:
    base = Path(temp)
    root = base / "task"
    nested = root / ".work/new-product"
    sibling = base / "other"
    for directory in (root, nested, sibling):
        directory.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(directory)], check=True)
    (root / ".work/escape").symlink_to(sibling, target_is_directory=True)
    lane_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    lane_env["TASK_WORKTREE"] = str(root)
    with patch.dict(os.environ, lane_env, clear=True):
        for tool, data in (
                ("Bash", {"command": f"cd {root}"}),
                ("Bash", {"command": f"git -C {root} add file.txt"}),
                ("Bash", {"command": "git add file.txt"}),
                ("Edit", {"file_path": str(root / "file.txt")}),
                ("Write", {"file_path": str(root / "file.txt")})):
            event = {"tool_name": tool, "cwd": str(nested), "tool_input": data}
            reason = policy.evaluate(event, (base,))
            assert reason is None, (event, reason)
        for tool, data in (
                ("Bash", {"command": f"cd {sibling}"}),
                ("Bash", {"command": f"git -C {sibling} add file.txt"}),
                ("Write", {"file_path": str(sibling / "file.txt")}),
                ("Edit", {"file_path": str(root / ".work/escape/file.txt")}),
                ("Write", {"file_path": str(nested / ".git/config")})):
            assert policy.evaluate({"tool_name": tool, "cwd": str(nested),
                                    "tool_input": data}, (base,)), data
        assert policy.evaluate({"tool_name": "Write", "cwd": str(sibling),
                                "tool_input": {"file_path": str(sibling / "file.txt")}}, (base,))
with patch.dict(os.environ, {"GIT_DIR": "/untrusted", "GIT_CONFIG_COUNT": "1", "POLICY_CONTROL": "kept"}):
    with patch.object(policy.subprocess, "run") as run:
        policy.policy_subprocess(["git", "status"], check=True)
        assert not any(k.startswith("GIT_") for k in run.call_args.kwargs["env"])
        assert run.call_args.kwargs["env"]["POLICY_CONTROL"] == "kept"
        assert run.call_args.args == (["git", "status"],) and run.call_args.kwargs["check"] is True

# Denial logging is best-effort, records only the executable category, and cannot
# change a deny when the state path is broken.
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    for command in (
            "task merge proj 5",
            "python3 /home/agent/dev_workspace/bootstrap/home/dot_local/bin/executable_task close proj 5",
            "env task note proj 5 improve x",
            "task cancel proj 5", "task denials", "task lane proj 5",
            "task review proj 5 --record review.md"):
        assert policy.evaluate_command(command, temp, root, (root,)) is None, command
    # pure_lookup_forms: standalone and read-only chains; execution wrappers stay denied.
    for lookup in ("command -v task", "command -V task", "type task", "which task"):
        for command in (lookup, "pwd; " + lookup, "pwd && " + lookup):
            assert policy.evaluate_command(command, temp, root, (root,)) is None, command
    assert policy.evaluate_command("command git push", temp, root, (root,))
    for command in ("task status proj", "task check", "task runner-check proj runner.md"):
        assert policy.evaluate_command(command, temp, root, (root,)) is None, command
    assert policy.evaluate_command("echo see task start for details", temp, root, (root,)) is None
    assert policy.evaluate_command("sh /usr/local/bin/task start proj 5", temp, root, (root,))
    state_target = str(Path.home() / ".local/state/task/proj/t5/review.json")
    assert policy.evaluate_command(f"printf x > {state_target}", temp, root, (root,))
    assert policy.evaluate({"tool_name": "Write", "cwd": temp,
                            "tool_input": {"file_path": state_target}}, (root,))
with tempfile.TemporaryDirectory() as temp:
    home = Path(temp) / "home"
    home.mkdir()
    event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "vendor": "claude",
             "tool_input": {"command": "GITHUB_TOKEN=ghp_x sudo ls /sensitive/argument"}, "cwd": temp}
    clean_env = {key: value for key, value in os.environ.items()
                 if not key.startswith("GIT_") and key != "AGENT_POLICY_STATE_DIR"}
    with patch.object(policy.Path, "home", return_value=home), patch.dict(os.environ, clean_env, clear=True):
        denial = policy.evaluate(event, (Path(temp),))
        policy.log_denial(event, denial, "claude")
    log = home / ".local/state/agent-policy/denials.jsonl"
    entry = json.loads(log.read_text())
    assert entry["category"] == "sudo" and entry["vendor"] == "claude"
    assert entry["rule_id"] == "command.unsafe_environment"
    assert "ghp_x" not in log.read_text()
    assert entry["reason"] and "[REDACTED]" in entry["command"]
    assert len(entry["reason"]) <= 200 and len(entry["command"]) <= 200
    assert policy.first_command_category("GITHUB_TOKEN=ghp_x sudo ls") == "sudo"
    assert policy.event_vendor({"hook_event_name": "PreToolUse", "codex_event": "PreToolUse"}) == "unknown"
    assert policy.event_vendor({}, "codex") == "codex"
    task_event = {"tool_name": "Bash", "cwd": temp,
                  "tool_input": {"command": "env task merge proj 5"}}
    with patch.dict(os.environ, clean_env, clear=True):
        task_denial = policy.evaluate(task_event, (Path(temp),))
    assert task_denial is None
    codex_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": temp,
                   "tool_input": {"command": "sudo true"}}
    with patch.object(policy.Path, "home", return_value=home), patch.dict(os.environ, clean_env, clear=True):
        policy.log_denial(codex_event, policy.evaluate(codex_event, (Path(temp),)), "codex")
    codex_log = [json.loads(line) for line in log.read_text().splitlines()][-1]
    assert codex_log["vendor"] == "codex"
    assert codex_log["rule_id"] == "host.command_boundary"
    # Synthetic fixtures only: every requested secret shape in both audit fields.
    fixtures = [prefix + "testvalue" for prefix in (
        "ghp_", "ghs_", "ghu_", "ghr_", "github_pat_", "gho_", "sk-", "sk-or-",
        "xoxb-", "xoxa-", "xoxp-", "xoxr-", "xoxs-", "xoxe-", "xoxc-", "xoxd-", "AKIA")]
    fixtures += ["Bearer testvalue", "a" * 32, "0123456789abcdef" * 2, "Ab9+" * 10 + "=="]
    for path in ("/home/agent/dev_workspace/bootstrap/system/agent-policy/pre_tool_use.py",
                 "/home/agent/dev_workspace/bootstrap/", "src/some/normal/path.py",
                 *(prefix + "a" * 40 for prefix in ("/home/agent/", "./src/lib/", "../src/", "~/src/", "$HOME/src/"))):
        assert policy.audit_text(path) == path
    for text in ("task-state", "disk-usage", "axoxb-value"):
        assert policy.audit_text(text) == text
    fixtures += ["/" + "a" * 40, "/" + "Ab9+" * 10,
                 "/aaaaaaaaaaaaaaaaaaaa/bbbbbbbbbbbbbbbbbbbb"]
    fixtures += ["relative/" + "a" * 40, "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                 "relative/" + "abc/" * 10, "'./" + "a" * 40 + "'"]
    assert policy.audit_text(None) == "None"
    assert policy.audit_text(123) == "123"
    assert policy.audit_text({"message": "ok"}) == str({"message": "ok"})
    fixtures += [name + separator + "testvalue" for name in (
        "token", "KEY", "Secret", "password", "passwd") for separator in ("=", ":", ": ")]
    fixtures += ["--" + name + " testvalue" for name in (
        "token", "api-key", "secret", "password", "passwd")]
    for fixture in fixtures:
        secret_event = {"tool_name": "Bash", "cwd": temp,
                        "tool_input": {"command": "sudo true " + fixture}}
        with patch.object(policy.Path, "home", return_value=home), patch.dict(os.environ, clean_env, clear=True):
            policy.log_denial(secret_event, policy.Denial("test.redaction", fixture), "codex")
        redacted = json.loads(log.read_text().splitlines()[-1])
        for field in ("reason", "command"):
            assert fixture not in redacted[field], (fixture, field)
            assert "testvalue" not in redacted[field], (fixture, field)
            assert "[REDACTED]" in redacted[field], (fixture, field)
    for command in (r"rg --foo C:\Users\x pat", r"grep --weird X \\srv\share"):
        assert policy.evaluate_command(command, temp, Path(temp), (Path(temp),)), command
    for executable in ("grep", "rg"):
        for option in ("--file=pats", "-fpats", "-nfpats"):
            for operand in (r"'C:\Users\x'", r"'\\srv\share'"):
                command = executable + " " + option + " " + operand
                assert policy.evaluate_command(command, temp, Path(temp), (Path(temp),)), command
    for command in (r"rg '\d+' src", r"grep -e '\bx\b' f",
                    r"rg -S '\w\s' src", r"grep -rIn '\s\d' .",
                    r"rg --unknown=value '\w\s' src"):
        assert policy.evaluate_command(command, temp, Path(temp), (Path(temp),)) is None, command
    for switch in ("-S", "--smart-case", "-I", "-s", "-L", "-u", "-uu", "-.",
                   "--no-ignore", "-z", "-U", "--multiline", "--json", "-a", "--text",
                   "--unknown=value"):
        assert policy.evaluate_command("rg " + switch + r" '\w\s' src", temp, Path(temp), (Path(temp),)) is None
        for operand in (r"C:\Users\x", r"\\srv\share"):
            command = "rg " + switch + r" '\w\s' " + operand
            assert policy.evaluate_command(command, temp, Path(temp), (Path(temp),)), command
    for switches in ("-rn", "-ni", "-Ei", "-rne", "-rnm 2", "-rnm2"):
        command = "grep " + switches + r" 'C:\\' src"
        assert policy.evaluate_command(command, temp, Path(temp), (Path(temp),)) is None, command
    command = r"grep -rneC:\\ src"
    assert policy.evaluate_command(command, temp, Path(temp), (Path(temp),)) is None, command
    command = r"grep '-rneC:\' 'C:\\' src"
    assert policy.evaluate_command(command, temp, Path(temp), (Path(temp),)), command
    for switches in ("-rn", "-ni", "-Ei", "-rne", "-rnefoo", "-rnf patterns", "-rnfpatterns", "-rnm2"):
        for path in (r"'C:\Users\x'", r"'\\srv\share'"):
            command = "grep " + switches + " needle " + path
            assert policy.evaluate_command(command, temp, Path(temp), (Path(temp),)), command
    broken_home = Path(temp) / "broken-home"
    broken_home.mkdir()
    (broken_home / ".local").write_text("not a directory")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | {"HOME": str(broken_home)}
    result = subprocess.run(pre_argv, input=json.dumps(event), capture_output=True, text=True,
                            cwd=temp, env=env, check=True, timeout=30)
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"

# Hardening regressions: reads stay frictionless; errors never escape the hook.
with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, clean_env, clear=True):
    root = Path(temp)
    loop_path = root / "loop"
    loop_path.symlink_to(loop_path)
    state = Path.home() / ".local/state/task/proj/check.json"
    def read_event(command):
        return {"tool_name": "Bash", "cwd": temp, "tool_input": {"command": command}}
    for executable in ("cat", "ls", "head", "grep", "wc", "stat"):
        for path in (str(state), "~/.local/state/task/proj/check.json",
                     "~/cortex/cortex-kb-proj/STATUS.md", "~/cortex/cortex-core/catalogs/TOOLS.md"):
            reason = policy.evaluate(read_event(f"{executable} {path}"), (root,))
            assert reason is None, (executable, path, reason)
    for command in (f"touch {state}", f"cat file > {state}", f"tee {state}"):
        assert policy.evaluate(read_event(command), (root,))
    for command in ("for x in README.md LICENSE; do cat $x; done",
                    'for x in README.md LICENSE; do head "$x"; done'):
        assert policy.evaluate(read_event(command), (root,)) is None, command
    for command in ("for x in README.md; do touch $x; done",
                    "for x in README.md; do cat $x > /outside; done",
                    "for x in README.md; do sudo cat $x; done"):
        assert policy.evaluate(read_event(command), (root,)), command
    for path in ("~unknownuser/file", str(loop_path)):
        event = read_event(f"touch {path}")
        with patch.object(policy, "root_for", return_value=root), patch.object(policy, "git_metadata", return_value=()):
            assert policy.evaluate(event, (root,))
    for function, hook_event in (("evaluate", "PreToolUse"), ("permission_reason", "PermissionRequest")):
        event = read_event("cat file") | {"hook_event_name": hook_event}
        with patch.object(policy.sys, "stdin", io.StringIO(json.dumps(event))), \
                patch.object(policy.sys, "stdout", new_callable=io.StringIO) as output, \
                patch.object(policy, function, side_effect=RuntimeError("test failure")), \
                patch.object(policy, "log_denial") as logged:
            assert policy.main([]) == 0
            assert "policy hook error: RuntimeError: test failure" in output.getvalue()
            response = json.loads(output.getvalue())["hookSpecificOutput"]
            message = (response["decision"]["message"] if hook_event == "PermissionRequest"
                       else response["permissionDecisionReason"])
            assert message == "policy hook error: RuntimeError: test failure" + policy.DENIAL_SUFFIX
            assert logged.call_args.args[1].rule_id == "hook.error"
    assert policy.deny("Original reason.")["hookSpecificOutput"]["permissionDecisionReason"] == (
        "Original reason." + policy.DENIAL_SUFFIX)
    for text in ("token=synthetic", "key=synthetic", "password=synthetic", "Authorization: synthetic"):
        assert "synthetic" not in policy.audit_text(text)
    assert policy.audit_text("x" * 300) == "[REDACTED]"
    assert len(policy.audit_text("word " * 100)) == 200
    assert policy.command_rule("Inline shell and interpreter programs are blocked.") == "shell.interpreter"
    assert policy.command_rule(policy.MIXED_CHAIN_MESSAGE) == "command.mixed_chain"
    assert policy.command_rule("An unmapped reason " * 20) == "command.other"
    with patch.object(policy.sys, "stdout", new_callable=io.StringIO) as output, \
            patch.object(policy.sys, "stderr", new_callable=io.StringIO), \
            patch.object(policy, "log_denial") as logged:
        assert policy.main(["--vendor", "invalid"]) == 0
        assert json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert logged.call_args.args[1].rule_id == "hook.error"
    for failure, rule in ((json.JSONDecodeError("bad", "", 0), "hook.input"),
                          (ValueError(), "hook.input"), (OSError("read failed"), "hook.error"),
                          (RuntimeError("parse failed"), "hook.error")):
        with patch.object(policy.json, "load", side_effect=failure), \
                patch.object(policy.sys, "stdout", new_callable=io.StringIO) as output, \
                patch.object(policy, "log_denial") as logged:
            assert policy.main(["--vendor", "codex"]) == 0
            assert json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"
            assert logged.call_args.args[1].rule_id == rule
            assert logged.call_args.args[2] == "codex"
    for path in ("~unknownuser/file", str(loop_path)):
        event = {"tool_name": "Bash", "cwd": str(ROOT), "tool_input": {"command": f"touch {path}"}}
        result = subprocess.run(pre_argv, input=json.dumps(event), capture_output=True, text=True,
                                env=clean_env | {"HOME": temp}, check=True, timeout=30)
        assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "Traceback" not in result.stderr
# Proof-batch friction: inspect commands, never execute their payloads.
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    subprocess.run(["git", "init", "-q", temp], check=True)
    (root / "outside-link").symlink_to(root.parent / "outside")
    (root / "--outside-link").symlink_to(root.parent / "outside")
    (root / "2" / ".git").mkdir(parents=True)
    (root / "a 2" / ".git").mkdir(parents=True)
    allowed = (
        "ls -la .work/ && echo --- && git --no-optional-locks check-ignore -v .work/report.md; echo --- && du -sh .work/uv-cache",
        "ls -la .work/context/ 2>&1; echo ---; cat docs/x.md 2>&1; echo ---; git status --short",
        "git status --short; ls missing 2>/dev/null",
        "ls 2>/dev/null",
        "ls 2> /dev/null",
        "git log 2>&1 | head",
        f"git -C {temp} status --short && sed -n '1,80p' {temp}/file",
        "if [ -d .work/context ]; then find .work/context -maxdepth 1 -type f -printf '%f\\n' | sort; else printf 'X\\n'; fi",
        "if test -f file; then cat file; fi",
        "set -eu; if test -f file; then cat file; fi",
        "git --no-optional-locks diff --no-index /dev/null docs/x.md",
        "git diff --no-index docs/x.md /dev/null",
        "git diff --no-index docs/x.md docs/y.md",
        "git status; ls > output", "git status; printf X >> output",
        "git status; ls 2> errors",
        "git status; ls 2>&2",
        "git status && sed -i 's/a/b/' file",
        "git status && sed -n -Ei 's/a/b/' file",
        "git status && sed -n --in-p 's/a/b/' file",
    )
    denied = (
        "set -o pipefail; cat file | tee output",
        "set -e; set -u; set -o pipefail; cat file | tee /tmp/hook-friction-output",
        "set -euo pipefail; cat file | tee output",
        r"rm -rf a\ 2>/dev/null",
        'rm -rf "a 2">/dev/null',
        r"chmod -R 777 a\ 2>/dev/null",
        "rm -rf 2", "rm -rf 2 >/dev/null",
        "chmod -R 777 2", "chmod -R 777 2 >/dev/null",
        "rm -rf '2' >/dev/null", "rm -rf 2 > /dev/null",
        "rm -rf 2 >/dev/null 2>/dev/null",
        "rm -rf 2>/dev/null 2 >/dev/null",
        "rm -rf '2>/dev/null' 2 >/dev/null",
        "git status && sed -n 'w output' file",
        "git status && sed -n 'e touch output' file",
        "set +e; cat file", "set -o history; cat file",
        "set -e; git push origin HEAD",
        "set -euo pipefail; cat file | tee /outside",
        "if test -f file; then touch /outside; else cat file; fi",
        "if test -f file; then cat file; else sudo true; fi",
        "if test -f file; then git add file; else git commit -m fixture; fi",
        "git add file; git commit -m fixture",
        "git diff --no-index /dev/null /outside",
        "git diff --no-index 'a@b:/hostname' /dev/null",
        "git diff --no-index 'file:///etc/hostname' /dev/null",
        "git diff --no-index /dev/null 'a@b:/hostname'",
        "git diff --no-index /dev/null 'https://example.invalid/hostname'",
        "git diff --no-index {docs/x.md,/etc/hostname} docs/x.md",
        "git diff --no-index {../../..,docs}/x docs/x.md",
        "git diff --no-index -- docs/x.md {docs/x.md,/etc/hostname}",
        "git diff --no-index docs/x.md {../../..,docs}/x",
        "git diff --no-index docs/*.md docs/x.md",
        "git diff --no-index docs/x.md docs/x?.md",
        "git diff --no-index docs/[xy].md docs/x.md",
        "git diff --no-index ~/x docs/x.md",
        "git diff --no-index $HOME/x docs/x.md",
        "git diff --no-index ${HOME}/x docs/x.md",
        "git diff --no-index docs/${X:-../../..}/etc/hostname docs/x.md",
        "git diff --no-index docs/$(pwd)/x docs/x.md",
        "git diff --no-index docs/`pwd`/x docs/x.md",
        "git diff --no-index -- docs/x.md docs/${X:-../../..}/etc/hostname",
        "git diff --no-index docs/x.md docs/$(pwd)/x",
        "git diff --no-index docs/x.md docs/`pwd`/x",
        "git diff --no-index docs/x.md ~/x",
        "git diff --no-index -- docs/x.md $HOME/x",
        "git diff --no-index /dev/null ../outside",
        "git diff --no-index /dev/null outside-link",
        "git diff --no-index -- /dev/null --outside-link",
        "set -e; cat <<EOF",
    )
    for command in allowed:
        reason = policy.evaluate_command(command, temp, root, (root,))
        assert reason is None, (command, reason)
    for command in denied:
        assert policy.evaluate_command(command, temp, root, (root,)), command
    for helper in policy.MIXED_CHAIN_TEXT_TOOLS:
        assert helper in policy.MIXED_CHAIN_MESSAGE, helper

# Exercise every response route against the actual audit writer, including failures.
with tempfile.TemporaryDirectory() as audit_temp:
    with patch.dict(os.environ, {"AGENT_POLICY_STATE_DIR": audit_temp}):
        cases = [
            ("{", None, None),
            ("[]", None, None),
            (json.dumps({"tool_name": "Bash", "tool_input": {"command": "sudo true password=fixture"}}),
             "evaluate", policy.Denial("test.command", "Command denied.")),
            (json.dumps({"hook_event_name": "PermissionRequest"}), "permission_reason", "Permission denied."),
        ]
        for payload, function, reason in cases:
            with patch.object(policy.sys, "stdin", io.StringIO(payload)), \
                    patch.object(policy.sys, "stdout", new_callable=io.StringIO):
                if function:
                    with patch.object(policy, function, return_value=reason):
                        policy.main([])
                    with patch.object(policy.sys, "stdin", io.StringIO(payload)), \
                            patch.object(policy, function, side_effect=RuntimeError("fixture")):
                        policy.main([])
                else:
                    policy.main([])
        rows = [json.loads(line) for line in (Path(audit_temp) / "denials.jsonl").read_text().splitlines()]
        assert len(rows) == 6
        assert {row["rule_id"] for row in rows} == {"hook.input", "hook.error", "test.command", "permission.policy"}
        assert all(row["rule_id"] and row["reason"] and "command" in row for row in rows)
        assert all("fixture" not in row["command"] for row in rows)
compile_check = 'python3 -I -X pycache_prefix="$tmp/pycache" -m compileall -q "$ROOT/system/agent-policy"'
assert install.index(compile_check) < install.index('sudo install -d -m 0755 "$public_source"')

# chezmoi removes the agent's stale copies (empty remove_ entries), so one copy remains.
assert (ROOT / "home/dot_codex/remove_hooks.json").read_bytes() == b""
stale = ROOT / "home/dot_local/share/bootstrap/agent-policy"
assert sorted(p.name for p in stale.iterdir()) == ["remove_context_reminder.py", "remove_pre_tool_use.py"]
assert all(p.read_bytes() == b"" for p in stale.iterdir())
print("PASS: guardrails only in root-owned managed config; hooks run root-owned scripts; no mode limits")
