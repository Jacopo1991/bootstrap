#!/usr/bin/env bash
# The global ignore list hides the sandbox placeholders and Claude Code project files,
# and does not hide ordinary project files or already tracked files.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
export HOME="$tmp/home" XDG_CONFIG_HOME="" GIT_CONFIG_NOSYSTEM=1
mkdir -p "$HOME/.config/git"
# Start from what Claude Code writes itself; the modify script must keep it and add the block.
printf '**/.claude/.cc-writes/\n' > "$HOME/.config/git/ignore.before"
bash "$ROOT/home/dot_config/git/modify_ignore" < "$HOME/.config/git/ignore.before" > "$HOME/.config/git/ignore"
grep -qx '\*\*/.claude/.cc-writes/' "$HOME/.config/git/ignore" || { echo "FAIL: existing line dropped"; exit 1; }
# Idempotent: a second run changes nothing.
bash "$ROOT/home/dot_config/git/modify_ignore" < "$HOME/.config/git/ignore" > "$HOME/.config/git/ignore.again"
cmp -s "$HOME/.config/git/ignore" "$HOME/.config/git/ignore.again" || { echo "FAIL: not idempotent"; exit 1; }
repo="$tmp/repo"; git init -q "$repo"; cd "$repo"
git -c user.name=t -c user.email=t@t commit -q --allow-empty -m init
for f in .bashrc .bash_profile .profile .zshrc .zprofile .gitconfig .gitmodules .ripgreprc .idea .vscode .mcp.json .claude.json; do : > "$f"; done
mkdir -p .claude && : > .claude/settings.local.json
mkdir -p pkg/__pycache__ && : > pkg/__pycache__/mod.cpython-312.pyc
: > README.md; : > .gitignore; : > .pre-commit-config.yaml
status=$(git status --porcelain --untracked-files=all)
expected=$'?? .gitignore\n?? .pre-commit-config.yaml\n?? README.md'
[ "$status" = "$expected" ] || { echo "FAIL: unexpected status:"; echo "$status"; exit 1; }
# A tracked file stays tracked and visible when changed.
git add -f .mcp.json && git -c user.name=t -c user.email=t@t commit -q -m tracked
echo '{}' > .mcp.json
git status --porcelain | grep -qx ' M .mcp.json' || { echo "FAIL: tracked .mcp.json change hidden"; exit 1; }
echo "PASS: global git ignore hides sandbox placeholders, Claude Code files and __pycache__/, not project or tracked files"
