#!/usr/bin/env bash
set -euo pipefail
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
  if awk '$3 == "drvfs" || $3 == "9p" { found=1 } END { exit !found }' /proc/mounts; then
    echo 'FAIL: Windows filesystem is mounted (drvfs/9p).' >&2; exit 1;
  fi
  if [[ -e /mnt/c/Windows ]]; then
    echo 'FAIL: /mnt/c/Windows is reachable.' >&2; exit 1;
  fi
  if [[ -r /proc/sys/fs/binfmt_misc/WSLInterop ]] &&
      grep -qx enabled /proc/sys/fs/binfmt_misc/WSLInterop; then
    echo 'FAIL: WSLInterop is enabled.' >&2; exit 1;
  fi
  echo 'PASS: Windows mounts, Windows directory access and WSLInterop denied.'
else
  echo 'SKIP: WSL-only checks (not WSL)'
fi
echo 'PASS: sudo denied, groups restricted, admin home/gh credentials inaccessible.'
