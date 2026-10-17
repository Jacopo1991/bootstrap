#!/usr/bin/env bash
set -euo pipefail
source_root=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# Earlier gate tests import policy modules. Copy only source files so their
# existing caches cannot affect this test's result.
ROOT="$tmp/source"
mkdir -p "$ROOT/system/agent-policy"
tar -C "$source_root/system/agent-policy" --exclude=__pycache__ --exclude='*.pyc' -cf - . |
  tar -C "$ROOT/system/agent-policy" -xf -
python3 -I -X pycache_prefix="$tmp/pycache" -m compileall -q "$ROOT/system/agent-policy"
cache_dirs=$(find "$ROOT/system/agent-policy" -type d -name __pycache__ -print)
[[ -z $cache_dirs ]] || { echo "FAIL: policy tree contains __pycache__ directories:"; echo "$cache_dirs"; exit 1; }
echo "PASS: compileall leaves policy copy without __pycache__ directories"
