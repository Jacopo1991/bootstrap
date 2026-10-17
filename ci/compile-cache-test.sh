#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

status=$(git -C "$ROOT" status --porcelain)
[[ -z $status ]] || { echo "FAIL: checkout is not clean before compile:"; echo "$status"; exit 1; }
python3 -I -X pycache_prefix="$tmp/pycache" -m compileall -q "$ROOT/system/agent-policy"
cache_dirs=$(find "$ROOT/system/agent-policy" -type d -name __pycache__ -print)
[[ -z $cache_dirs ]] || { echo "FAIL: policy tree contains __pycache__ directories:"; echo "$cache_dirs"; exit 1; }
status=$(git -C "$ROOT" status --porcelain)
[[ -z $status ]] || { echo "FAIL: compile changed checkout status:"; echo "$status"; exit 1; }
echo "PASS: compileall leaves checkout clean"
