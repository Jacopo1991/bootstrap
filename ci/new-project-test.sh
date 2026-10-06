#!/usr/bin/env bash
# The new-project command creates both repositories on main with one commit each and refuses to overwrite.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
for tool in backlog pre-commit gitleaks lychee osv-scanner; do
  command -v "$tool" >/dev/null || { echo "SKIP: $tool is not installed (installed by the chezmoi tools script)"; exit 0; }
done
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# backlog is a node script behind a mise shim; keep mise pointed at the real home.
export MISE_DATA_DIR="${MISE_DATA_DIR:-$HOME/.local/share/mise}" MISE_CONFIG_DIR="${MISE_CONFIG_DIR:-$HOME/.config/mise}" MISE_CACHE_DIR="${MISE_CACHE_DIR:-$HOME/.cache/mise}"
export HOME="$tmp" GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
mkdir -p "$HOME/dev_workspace" "$HOME/cortex"
cmd="$ROOT/home/dot_local/bin/executable_new-project"
out=$(bash "$cmd" create demo-site --local-only)
grep -q "run chezmoi apply to add it to the qmd index" <<<"$out"
for repo in "$HOME/dev_workspace/demo-site" "$HOME/cortex/cortex-kb-demo-site"; do
  [ "$(git -C "$repo" branch --show-current)" = main ]
  [ "$(git -C "$repo" rev-list --count HEAD)" = 1 ]
  [ -z "$(git -C "$repo" status --porcelain)" ]
  # The standard pre-commit gate is committed and its hook installed.
  cmp -s "$ROOT/home/dot_local/share/bootstrap/pre-commit/pre-commit-config.yaml" "$repo/.pre-commit-config.yaml"
  git -C "$repo" ls-files --error-unmatch .pre-commit-config.yaml >/dev/null
  grep -q pre-commit "$repo/.git/hooks/pre-commit"
done
# The code repository ignores .work/, the scratch folder for runtime state and pinned copies.
grep -qx ".work/" "$HOME/dev_workspace/demo-site/.gitignore"
kb="$HOME/cortex/cortex-kb-demo-site"
for f in INTENT.md STATUS.md AGENTS.md README.md backlog/config.yml; do
  [ -f "$kb/$f" ]
done
# Backlog.md replaces the old tasks/ and decisions.md skeleton.
[ ! -e "$kb/tasks" ] && [ ! -e "$kb/decisions.md" ]
[ -d "$kb/backlog/tasks" ] && [ -d "$kb/backlog/decisions" ]
config="$kb/backlog/config.yml"
grep -Eq '^(auto_commit|autoCommit): false' "$config"
grep -Eq '^(remote_operations|remoteOperations): false' "$config"
grep -Eq '^(check_active_branches|checkActiveBranches): false' "$config"
grep -Eqi '^(task_prefix|taskPrefix): "?T"?$' "$config"
# Integration mode none: Backlog.md writes no agent instruction or MCP files.
[ ! -e "$kb/CLAUDE.md" ] && [ ! -e "$kb/.mcp.json" ]
(cd "$kb" && backlog task create "Smoke" --plain >/dev/null)
[ -n "$(find "$kb/backlog/tasks" -iname 't-1 *.md')" ]
# auto_commit false: creating a task must not commit.
[ "$(git -C "$kb" rev-list --count HEAD)" = 1 ]
git -C "$kb" clean -fdq
grep -q "local only" "$HOME/cortex/cortex-kb-demo-site/README.md"
if bash "$cmd" create demo-site >/dev/null 2>&1; then echo "FAIL: overwrite allowed"; exit 1; fi
if bash "$cmd" create Bad_Name >/dev/null 2>&1; then echo "FAIL: bad name accepted"; exit 1; fi
echo "PASS: new-project creates both repositories on main with the pre-commit gate and refuses overwrites and bad names"
