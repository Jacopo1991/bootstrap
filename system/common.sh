#!/usr/bin/env bash
# Shared public installer helpers. No credentials are accepted or logged.
set -euo pipefail
BOOTSTRAP_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../home/.chezmoitemplates/pins.env
source "$BOOTSTRAP_ROOT/home/.chezmoitemplates/pins.env"

require_root() {
  [[ $EUID == 0 ]] || { echo 'Run this system script with sudo.' >&2; exit 1; }
  # shellcheck source=/etc/os-release
  source /etc/os-release
  [[ $ID == ubuntu && $VERSION_ID == 24.04 && $(dpkg --print-architecture) == amd64 ]] || {
    echo 'This bootstrap supports Ubuntu 24.04 amd64 only.' >&2; exit 1;
  }
}

# The managed agent hooks run these files. Each one, and every directory above it, must be
# owned by root and writable by nobody else, so the agent account cannot swap what runs.
require_root_owned_policy() {
  local file path mode
  [[ $BOOTSTRAP_ROOT == /opt/machine-bootstrap/current ]] || {
    echo 'Run the managed-policy installers from /opt/machine-bootstrap/current.' >&2; exit 1;
  }
  for file in /usr/local/lib/agent-policy/pre_tool_use.py \
    /usr/local/lib/agent-policy/context_reminder.py \
    /usr/bin/env /usr/bin/python3 /usr/bin/git /usr/local/bin/gitleaks; do
    path=$(readlink -e -- "$file") || { echo "Missing managed-policy file: $file" >&2; exit 1; }
    while :; do
      mode=$(stat -c '%a' -- "$path")
      [[ $(stat -c '%u' -- "$path") == 0 && $((8#$mode & 8#022)) == 0 ]] || {
        echo "Not root-owned, or writable by others: $path" >&2; exit 1;
      }
      [[ $path != / ]] || break
      path=$(dirname -- "$path")
    done
  done
}

# Replace a complete root-owned file without exposing a partial write to hooks.
atomic_policy_install() {
  local source=$1 target=$2 temp
  temp=$(mktemp "$(dirname -- "$target")/.agent-policy.XXXXXX")
  if install -o root -g root -m 0644 "$source" "$temp" && mv -f -- "$temp" "$target"; then
    return 0
  fi
  rm -f -- "$temp"
  return 1
}

download_verified() {
  local url=$1 checksum=$2 target=$3
  curl --fail --silent --show-error --location --retry 3 \
    --connect-timeout 30 --max-time 900 --speed-time 60 --speed-limit 1024 "$url" -o "$target"
  printf '%s  %s\n' "$checksum" "$target" | sha256sum --check --status
}

apt_locked() {
  local file=$1 package spec
  local -a packages=()
  mapfile -t packages < "$file"
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends --allow-downgrades --allow-change-held-packages "${packages[@]}"
  for spec in "${packages[@]}"; do
    package=${spec%%=*}
    [[ $(dpkg-query -W -f='${Version}' "$package") == "${spec#*=}" ]]
    apt-mark hold "$package" >/dev/null
  done
}

apt_key() {
  local name=$1 url=$2 checksum=$3 format=$4 temp
  temp=$(mktemp)
  download_verified "$url" "$checksum" "$temp"
  install -d -m 0755 /etc/apt/keyrings
  if [[ $format == armored ]]; then
    gpg --batch --yes --dearmor --output "/etc/apt/keyrings/$name.gpg" "$temp"
    chmod 0644 "/etc/apt/keyrings/$name.gpg"
  else
    install -m 0644 "$temp" "/etc/apt/keyrings/$name.gpg"
  fi
  rm -f "$temp"
}
