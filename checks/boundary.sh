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

has_enabled_wsl_interop() {
  local directory=${1:-/proc/sys/fs/binfmt_misc} entry
  for entry in "$directory"/WSLInterop*; do
    if [[ -r $entry ]] && grep -qx enabled "$entry"; then
      return 0
    fi
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
  if has_enabled_wsl_interop; then
    echo 'FAIL: WSLInterop is enabled.' >&2; exit 1;
  fi
  echo 'PASS: Windows mounts, Windows directory access and WSLInterop denied.'
else
  echo 'SKIP: WSL-only checks (not WSL)'
fi
echo 'PASS: sudo denied, groups restricted, admin home/gh credentials inaccessible.'
