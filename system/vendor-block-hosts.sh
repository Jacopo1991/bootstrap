#!/usr/bin/env bash
# Converges a marked block of /etc/hosts entries that send Desktop Commander's vendor hosts to
# 0.0.0.0 and ::, run by system/base.sh. Found in the pinned 0.2.52 dist/: desktopcommander.app
# (feature flags, welcome page), telemetry.desktopcommander.app and its Cloud Run fallback (the
# GA4 Measurement Protocol proxy, /mp/collect), mcp.desktopcommander.app (remote device
# channel). Idempotent: the block is replaced, never duplicated, and everything outside it is
# left alone. Usage: vendor-block-hosts.sh [hosts-file] (default /etc/hosts, rewritten in place).
set -euo pipefail
hosts_file=${1:-/etc/hosts}
begin='# BEGIN bootstrap vendor-block'
end='# END bootstrap vendor-block'
blocked_hosts=(
  desktopcommander.app
  telemetry.desktopcommander.app
  mcp.desktopcommander.app
  dc-telemetry-proxy-83847352264.europe-west1.run.app
)
[[ -f $hosts_file && ! -L $hosts_file ]] || { echo "Refusing $hosts_file." >&2; exit 1; }
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
awk -v begin="$begin" -v end="$end" '
  $0 == begin { skip = 1; next }
  $0 == end { skip = 0; next }
  !skip { print }
' "$hosts_file" > "$tmp"
{
  echo "$begin"
  for host in "${blocked_hosts[@]}"; do
    echo "0.0.0.0 $host"
    echo ":: $host"
  done
  echo "$end"
} >> "$tmp"
# Write through the existing file so the WSL-managed inode and mode stay as they are.
cat "$tmp" > "$hosts_file"
