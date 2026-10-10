#!/usr/bin/env bash
set -euo pipefail

scanner=$1
repo=$2
scan_root=$3
mkdir -p "$scan_root"
while IFS= read -r -d '' path; do
  case "$path" in
    .work|.work/*) continue ;;
  esac
  [[ -f $repo/$path || -L $repo/$path ]] || continue
  parent=${path%/*}
  if [[ $parent != "$path" ]]; then
    mkdir -p -- "$scan_root/$parent"
  fi
  cp -a -- "$repo/$path" "$scan_root/$path"
done < <(git -C "$repo" ls-files --cached --others --exclude-standard -z)

"$scanner" dir --redact --no-banner "$scan_root"
