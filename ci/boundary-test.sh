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
if has_enabled_wsl_interop "$tmp"; then
  echo 'FAIL: absent interop entries detected as enabled.' >&2; exit 1;
fi
printf 'disabled\n' > "$tmp/WSLInterop"
printf 'disabled\n' > "$tmp/WSLInterop-late"
if has_enabled_wsl_interop "$tmp"; then
  echo 'FAIL: disabled interop entries detected as enabled.' >&2; exit 1;
fi
printf 'enabled\ninterpreter /init\n' > "$tmp/WSLInterop-late"
has_enabled_wsl_interop "$tmp" || { echo 'FAIL: enabled WSLInterop-late missed.' >&2; exit 1; }
printf 'disabled\n' > "$tmp/WSLInterop-late"
printf 'enabled\ninterpreter /init\n' > "$tmp/WSLInterop"
has_enabled_wsl_interop "$tmp" || { echo 'FAIL: enabled WSLInterop missed.' >&2; exit 1; }
echo 'PASS: absent, disabled, enabled and late interop entries'
