#!/usr/bin/env bash
# The new-project command creates both repositories on main with one commit each and refuses to overwrite.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
export HOME="$tmp" GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
mkdir -p "$HOME/dev_workspace" "$HOME/cortex"
cmd="$ROOT/home/dot_local/bin/executable_new-project"
bash "$cmd" create demo-site --local-only >/dev/null
for repo in "$HOME/dev_workspace/demo-site" "$HOME/cortex/cortex-kb-demo-site"; do
  [ "$(git -C "$repo" branch --show-current)" = main ]
  [ "$(git -C "$repo" rev-list --count HEAD)" = 1 ]
  [ -z "$(git -C "$repo" status --porcelain)" ]
done
for f in INTENT.md STATUS.md decisions.md tasks/TEMPLATE.md AGENTS.md README.md; do
  [ -f "$HOME/cortex/cortex-kb-demo-site/$f" ]
done
grep -q "local only" "$HOME/cortex/cortex-kb-demo-site/README.md"
if bash "$cmd" create demo-site >/dev/null 2>&1; then echo "FAIL: overwrite allowed"; exit 1; fi
if bash "$cmd" create Bad_Name >/dev/null 2>&1; then echo "FAIL: bad name accepted"; exit 1; fi
echo "PASS: new-project creates both repositories on main and refuses overwrites and bad names"
