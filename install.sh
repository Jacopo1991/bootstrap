#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ $EUID != 0 && $(id -un) != agent ]] || { echo 'Run as the non-root admin user.' >&2; exit 1; }
[[ -z $(git -C "$ROOT" status --porcelain) ]] || {
  echo 'Commit bootstrap changes before running install.sh.' >&2; exit 1;
}
sudo -v
bash_cmd=/bin/bash
sudo "$bash_cmd" "$ROOT/system/base.sh"
sudo "$bash_cmd" "$ROOT/system/users.sh" "$(id -un)"
# shellcheck source=system/common.sh
source "$ROOT/system/common.sh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
download_verified "$CHEZMOI_URL" "$CHEZMOI_SHA256" "$tmp/chezmoi.tar.gz"
tar -xzf "$tmp/chezmoi.tar.gz" -C "$tmp" chezmoi
if ! cmp -s "$tmp/chezmoi" /usr/local/bin/chezmoi; then
  sudo install -m 0755 "$tmp/chezmoi" /usr/local/bin/chezmoi
fi
download_verified "$MISE_URL" "$MISE_SHA256" "$tmp/mise.tar.xz"
tar -xJf "$tmp/mise.tar.xz" -C "$tmp" mise/bin/mise
if ! cmp -s "$tmp/mise/bin/mise" /usr/local/bin/mise; then
  sudo install -m 0755 "$tmp/mise/bin/mise" /usr/local/bin/mise
fi

# Publish only committed public files, never the admin's .git config or home.
revision=$(git -C "$ROOT" rev-parse HEAD)
public_source="/opt/machine-bootstrap/revisions/$revision"
sudo install -d -m 0755 "$public_source"
git -C "$ROOT" archive HEAD | sudo tar -xf - -C "$public_source"
# chezmoi init expects a Git source. Create fresh metadata without copying the
# admin's repository config, credentials, templates or hooks.
sudo git -C "$public_source" init --quiet --initial-branch=main --template=
sudo chown -R root:root "$public_source"
sudo chmod -R go-w "$public_source"
sudo ln -sfn "$public_source" /opt/machine-bootstrap/current

# chezmoi's existing-source init applies .chezmoiroot and prompts once for the
# single Git identity. The source remains public and owned by root.
cd /tmp
sudo -H -u agent env PATH=/usr/local/bin:/usr/bin:/bin \
  CHEZMOI_GIT_NAME="${CHEZMOI_GIT_NAME:-}" CHEZMOI_GIT_EMAIL="${CHEZMOI_GIT_EMAIL:-}" \
  chezmoi --source /opt/machine-bootstrap/current init --apply
echo 'Bootstrap applied. Restart this distro before the WSL boundary checks.'
