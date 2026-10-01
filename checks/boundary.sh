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
  local interop_is_set=${1:-} socket_dir=${2:-/run/WSL} pid_list=${3-}
  local proc_dir=${4:-/proc} pid exe
  [[ -n $interop_is_set ]] && return 0
  for pid in $pid_list; do
    [[ $pid =~ ^[0-9]+$ ]] || continue
    # WSL init sockets persist even with interop disabled; they are not evidence
    # of an enabled user session. Keep checking every non-init ancestor.
    [[ $pid == 1 || $pid == 2 ]] && continue
    exe=$(readlink -- "$proc_dir/$pid/exe" 2>/dev/null || true)
    [[ $exe == /init ]] && continue
    [[ -S "$socket_dir/${pid}_interop" ]] && return 0
  done
  return 1
}

has_ssh_conditional_or_include() {
  grep -Eiq '^[[:space:]]*(Match|Include)[[:space:]]' "$1"
}

check_vscode_ssh_boundary() {
  local config=/etc/ssh/sshd_config.agentdev effective listeners
  [[ -r $config ]] || { echo 'FAIL: AgentDev SSH policy is missing.' >&2; return 1; }
  if has_ssh_conditional_or_include "$config"; then
    echo 'FAIL: AgentDev SSH policy may not use Match or Include directives.' >&2; return 1
  fi
  effective=$(/usr/sbin/sshd -G -f "$config" 2>/dev/null) || {
    echo 'FAIL: AgentDev SSH policy cannot be evaluated.' >&2; return 1;
  }
  for setting in \
    'port 2222' 'passwordauthentication no' 'kbdinteractiveauthentication no' \
    'authenticationmethods publickey' 'permitrootlogin no' 'allowusers agent' \
    'allowtcpforwarding local' 'permitopen 127.0.0.1:*' 'gatewayports no' \
    'allowstreamlocalforwarding no' 'x11forwarding no' 'allowagentforwarding no'; do
    grep -Fxq -- "$setting" <<< "$effective" || {
      echo "FAIL: AgentDev SSH policy lacks required setting: $setting" >&2; return 1;
    }
  done
  if [[ $(grep -c '^listenaddress ' <<< "$effective") != 1 ]] ||
    ! grep -Fxq 'listenaddress 127.0.0.1:2222' <<< "$effective"; then
    echo 'FAIL: AgentDev SSH must have one IPv4 loopback listener on port 2222.' >&2
    return 1
  fi
  [[ -L /etc/systemd/system/ssh.socket && $(readlink /etc/systemd/system/ssh.socket) == /dev/null ]] || {
    echo 'FAIL: Ubuntu SSH socket activation is not masked.' >&2; return 1;
  }
  [[ -L /etc/systemd/system/multi-user.target.wants/ssh.service ]] || {
    echo 'FAIL: AgentDev SSH service is not enabled.' >&2; return 1;
  }
  listeners=$(ss -H -ltn | awk '$1 == "LISTEN" && $4 ~ /:2222$/ { print $4 }')
  [[ $listeners == 127.0.0.1:2222 ]] || {
    echo 'FAIL: AgentDev SSH is not listening only on 127.0.0.1:2222.' >&2; return 1;
  }
  echo 'PASS: AgentDev SSH policy, service activation and loopback listener enforced.'
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
check_vscode_ssh_boundary
echo 'PASS: sudo denied, groups restricted, admin home/gh credentials inaccessible.'
