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
