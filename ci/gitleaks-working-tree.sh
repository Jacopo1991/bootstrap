#!/usr/bin/env bash
set -euo pipefail

scanner=$1
repo=$2
scan_root=$3
mkdir -p "$scan_root"
copy_path() {
  [[ -f $repo/$path || -L $repo/$path ]] || return 0
  parent=${path%/*}
  if [[ $parent != "$path" ]]; then
    mkdir -p -- "$scan_root/$parent"
  fi
  cp -a -- "$repo/$path" "$scan_root/$path"
}
while IFS= read -r -d '' path; do
  copy_path
done < <(git -C "$repo" ls-files --cached -z)

while IFS= read -r -d '' path; do
  case "$path" in .work|.work/*) continue ;; esac
  copy_path
done < <(git -C "$repo" ls-files --others --exclude-standard -z)

while IFS= read -r -d '' path; do
  case "$path" in .work|.work/*) continue ;; esac
  copy_path
done < <(git -C "$repo" ls-files --others --exclude-standard --ignored -z)

args=(dir --redact --no-banner)
if [[ -f $repo/.gitleaks.toml ]]; then
  args+=(--config "$repo/.gitleaks.toml")
fi
"$scanner" "${args[@]}" "$scan_root"
