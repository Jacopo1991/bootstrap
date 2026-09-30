#!/usr/bin/env bash
set -euo pipefail
# Read /proc/mounts format from stdin; return success only for Windows drives.
has_windows_drive_mount() {
  awk '
    $3 == "drvfs" || $2 ~ /^\/mnt\/[a-z](\/|$)/ ||
    ($3 == "9p" && ($4 ~ /(^|[,;])aname=drvfs([,;]|$)/ ||
                   $4 ~ /(^|[,;=])[[:alpha:]]:/)) { found=1 }
    END { exit !found }
  '
}

# Binfmt registration can remain enabled even when Windows process launch is
# blocked. Check only this agent login's environment and its own session socket.
has_agent_session_interop() {
  local interop_is_set=${1:-} socket_dir=${2:-/run/WSL} pid_list=${3-} pid
  [[ -n $interop_is_set ]] && return 0
  for pid in $pid_list; do
    [[ $pid =~ ^[0-9]+$ ]] || continue
    [[ -S "$socket_dir/${pid}_interop" ]] && return 0
  done
  return 1
}

# Unit tests source the same predicates without running account checks.
if [[ ${BASH_SOURCE[0]} != "$0" ]]; then
  return 0
fi

[[ $(id -un) == agent && $EUID != 0 ]] || { echo 'Run as agent.' >&2; exit 1; }
if sudo -n true 2>/dev/null; then
  echo 'FAIL: agent can sudo.' >&2; exit 1;
fi
for group in sudo docker; do
  if id -nG | tr ' ' '\n' | grep -qx "$group"; then
    echo "FAIL: agent is in $group." >&2; exit 1;
  fi
done
admin_home=$(cat /etc/bootstrap/admin-home)
[[ $admin_home == /home/* && $admin_home != /home/agent ]]
if [[ -r $admin_home || -x $admin_home ]] || ls "$admin_home" >/dev/null 2>&1; then
  echo 'FAIL: admin home is accessible.' >&2; exit 1;
fi
# Opening, rather than printing, also catches readable credential files.
if (exec 3< "$admin_home/.config/gh/hosts.yml") 2>/dev/null; then
  echo 'FAIL: admin gh credentials are readable.' >&2; exit 1;
fi
if [[ -S /var/run/docker.sock && ( -r /var/run/docker.sock || -w /var/run/docker.sock ) ]]; then
  echo 'FAIL: agent has Docker socket access.' >&2; exit 1;
fi
if grep -qiE 'microsoft|wsl' /proc/version; then
  if has_windows_drive_mount < /proc/mounts; then
    echo 'FAIL: Windows drive is mounted.' >&2; exit 1;
  fi
  if [[ -e /mnt/c/Windows ]]; then
    echo 'FAIL: /mnt/c/Windows is reachable.' >&2; exit 1;
  fi
  ancestor_pids=()
  pid=$$
  while [[ $pid =~ ^[0-9]+$ && -r /proc/$pid/status ]]; do
    ancestor_pids+=("$pid")
    pid=$(awk '/^PPid:/ {print $2}' "/proc/$pid/status")
    [[ $pid == 0 ]] && break
  done
  if has_agent_session_interop "${WSL_INTEROP+x}" /run/WSL "${ancestor_pids[*]}"; then
    echo 'FAIL: Windows interop is available in the agent login/session.' >&2; exit 1;
  fi
  echo 'PASS: Windows mounts, Windows directory access and agent-session interop denied.'
else
  echo 'SKIP: WSL-only checks (not WSL)'
fi
echo 'PASS: sudo denied, groups restricted, admin home/gh credentials inaccessible.'
