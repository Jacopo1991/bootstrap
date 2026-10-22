#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=../checks/boundary.sh
source "$(dirname -- "$0")/../checks/boundary.sh"

check_mount() {
  local expected=$1 label=$2 mounts=$3 actual=allow
  if has_windows_drive_mount <<< "$mounts"; then
    actual=deny
  fi
  [[ $actual == "$expected" ]] || {
    echo "FAIL: $label: expected $expected, got $actual" >&2; exit 1;
  }
  echo "PASS: $label ($actual)"
}
check_mount allow 'WSL GPU drivers' 'drivers /usr/lib/wsl/drivers 9p ro,aname=drivers;fmask=222;dmask=222,trans=fd 0 0'
check_mount allow 'WSL system library' 'none /usr/lib/wsl/lib tmpfs ro 0 0'
check_mount deny 'drvfs C: mount' 'C:\134 /mnt/c drvfs rw 0 0'
check_mount deny '9p drvfs at custom mount point' 'C:\134 /windows 9p rw,aname=drvfs;path=C:\;uid=1000 0 0'
check_mount deny '9p drive path without drvfs marker' 'share /windows 9p rw,path=D:\;uid=1000 0 0'
check_mount deny 'mount under drive directory' 'share /mnt/z/work ext4 rw 0 0'
check_mount deny 'mixed system and drive mounts' $'drivers /usr/lib/wsl/drivers 9p ro,aname=drivers 0 0\nC:\\134 /windows 9p rw,aname=drvfs 0 0'
check_mount allow 'ordinary Linux mounts' $'/dev/sdc / ext4 rw 0 0\ntmpfs /mnt/cache tmpfs rw 0 0'

# Prefer TMPDIR; fall back to the permitted short gate temp root when the longest socket
# path plus its terminating NUL would exceed AF_UNIX's 108-byte limit.
socket_template=${TMPDIR:-/tmp}/s.XXXXXXXX
socket_path=$socket_template/WSL/123_interop
if (( $(LC_ALL=C printf '%s' "$socket_path" | wc -c) >= 108 )); then
  socket_root=/tmp/task-gate-$(id -u)
  (umask 077; mkdir -p "$socket_root")
  socket_template=$socket_root/s.XXXXXXXX
fi
tmp=$(mktemp -d "$socket_template")
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/WSL"
mkdir -p "$tmp/proc/789"
ln -s /init "$tmp/proc/789/exe"
if has_agent_session_interop '' "$tmp/WSL" '123 456'; then
  echo 'FAIL: absent session socket detected.' >&2; exit 1;
fi
touch "$tmp/WSL/123_interop"
if has_agent_session_interop '' "$tmp/WSL" 123 "$tmp/proc"; then
  echo 'FAIL: regular file detected as session socket.' >&2; exit 1;
fi
rm "$tmp/WSL/123_interop"
has_agent_session_interop x "$tmp/WSL" '789 2 1' "$tmp/proc" || { echo 'FAIL: set WSL_INTEROP environment missed.' >&2; exit 1; }
status=0
python3 "$(dirname -- "$0")/boundary-sockets.py" \
  "$tmp/WSL/123_interop" "$tmp/WSL/2_interop" "$tmp/WSL/789_interop" || status=$?
[[ $status == 0 ]] || exit "$status"
ln -s 2_interop "$tmp/WSL/1_interop"
for pid in 1 2 123 789; do
  [[ -S "$tmp/WSL/${pid}_interop" ]] || { echo "FAIL: socket fixture $pid missing." >&2; exit 1; }
done
if has_agent_session_interop '' "$tmp/WSL" '999 456 2 1' "$tmp/proc"; then
  echo 'FAIL: WSL init PID sockets flagged as session interop.' >&2; exit 1;
fi
if has_agent_session_interop '' "$tmp/WSL" '999 789 2 1' "$tmp/proc"; then
  echo 'FAIL: /init executable socket flagged as session interop.' >&2; exit 1;
fi
has_agent_session_interop '' "$tmp/WSL" '999 123 789 2 1' "$tmp/proc" || { echo 'FAIL: user-session ancestor socket missed.' >&2; exit 1; }
has_agent_session_interop '' "$tmp/WSL" '1 2 789 123' "$tmp/proc" || { echo 'FAIL: init ancestors stopped user-session socket detection.' >&2; exit 1; }
if has_agent_session_interop '' "$tmp/WSL" 456 "$tmp/proc"; then
  echo 'FAIL: unrelated session socket flagged.' >&2; exit 1;
fi
has_agent_session_interop x "$tmp/WSL" '789 2 1' "$tmp/proc" || { echo 'FAIL: set WSL_INTEROP environment missed.' >&2; exit 1; }
echo 'PASS: init sockets excluded, user-session socket and WSL_INTEROP detected, unrelated socket ignored'

# Rootless Docker socket: only the owner-only socket at the expected path, served by the expected
# user, is accepted; every other reachable Docker socket fails. Sockets are fixtures (no server).
me=$(id -un)
mkdir "$tmp/d" "$tmp/proc/4242"
python3 "$(dirname -- "$0")/boundary-sockets.py" "$tmp/d/docker.sock" "$tmp/d/other.sock"
ln -s docker.sock "$tmp/d/alias.sock"
serving() { printf 'u_str LISTEN 0 4096 %s 99999 * 0 users:(("dockerd",pid=%s,fd=7))\n' "$1" "$2"; }
docker_case() {  # docker_case allow|deny label want ss-output proc-dir socket...
  local expected=$1 label=$2 want=$3 ss_output=$4 proc_dir=$5 actual=deny
  shift 5
  if check_docker_sockets "$tmp/d/docker.sock" "$want" "$ss_output" "$proc_dir" "$@" >/dev/null 2>&1; then
    actual=allow
  fi
  [[ $actual == "$expected" ]] || { echo "FAIL: $label: expected $expected, got $actual" >&2; exit 1; }
  echo "PASS: $label ($actual)"
}
chmod 600 "$tmp/d/docker.sock"
good=$(serving "$tmp/d/docker.sock" 4242)
docker_case allow 'no Docker socket present' "$me" '' "$tmp/proc" "$tmp/d/missing.sock"
docker_case allow 'owner-only rootless socket served by its owner' "$me" "$good" "$tmp/proc" "$tmp/d/docker.sock"
docker_case deny 'socket owned by someone else' nobody "$good" "$tmp/proc" "$tmp/d/docker.sock"
docker_case deny 'socket served by root' "$me" "$(serving "$tmp/d/docker.sock" 1)" /proc "$tmp/d/docker.sock"
docker_case deny 'socket whose server is unknown' "$me" "$(serving "$tmp/d/docker.sock" 999999)" /proc "$tmp/d/docker.sock"
docker_case deny 'socket with no listener' "$me" '' "$tmp/proc" "$tmp/d/docker.sock"
docker_case deny 'listener on a different path' "$me" "$(serving "$tmp/d/other.sock" 4242)" "$tmp/proc" "$tmp/d/docker.sock"
docker_case deny 'any other Docker socket path' "$me" "$good" "$tmp/proc" "$tmp/d/other.sock"
docker_case deny 'alias of the rootless socket' "$me" "$good" "$tmp/proc" "$tmp/d/alias.sock"
docker_case deny 'valid socket plus another socket' "$me" "$good" "$tmp/proc" "$tmp/d/docker.sock" "$tmp/d/other.sock"
chmod 660 "$tmp/d/docker.sock"
docker_case deny 'group-accessible rootless socket' "$me" "$good" "$tmp/proc" "$tmp/d/docker.sock"
chmod 606 "$tmp/d/docker.sock"
docker_case deny 'world-accessible rootless socket' "$me" "$good" "$tmp/proc" "$tmp/d/docker.sock"
echo 'PASS: rootless Docker socket accepted only at its path, owner-only, served by its owner'
