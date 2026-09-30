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

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/WSL"
if has_agent_session_interop '' "$tmp/WSL" '123 456'; then
  echo 'FAIL: absent session socket detected.' >&2; exit 1;
fi
touch "$tmp/WSL/123_interop"
if has_agent_session_interop '' "$tmp/WSL" 123 1000; then
  echo 'FAIL: regular file detected as session socket.' >&2; exit 1;
fi
rm "$tmp/WSL/123_interop"
if command -v socat >/dev/null; then
  socat UNIX-LISTEN:"$tmp/WSL/123_interop",fork /dev/null >/dev/null 2>&1 &
elif command -v nc >/dev/null; then
  nc -lU "$tmp/WSL/123_interop" >/dev/null 2>&1 &
else
  python3 -c 'import socket,sys,time; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); time.sleep(5)' "$tmp/WSL/123_interop" &
fi
socket_pid=$!
for _ in {1..50}; do [[ -S "$tmp/WSL/123_interop" ]] && break; sleep .02; done
has_agent_session_interop '' "$tmp/WSL" '999 123 456' || { echo 'FAIL: root launcher ancestor socket missed.' >&2; kill "$socket_pid" 2>/dev/null || true; exit 1; }
if has_agent_session_interop '' "$tmp/WSL" 456; then
  echo 'FAIL: unrelated session socket flagged.' >&2; kill "$socket_pid" 2>/dev/null || true; exit 1;
fi
kill "$socket_pid" 2>/dev/null || true
wait "$socket_pid" 2>/dev/null || true
has_agent_session_interop x "$tmp/WSL" 456 || { echo 'FAIL: set WSL_INTEROP environment missed.' >&2; exit 1; }
echo 'PASS: WSL_INTEROP environment, root ancestor socket and unrelated socket checks'
