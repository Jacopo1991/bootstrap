#!/usr/bin/env bash
# Fresh Ubuntu WSL filesystem, running on the ubuntu-24.04 hosted runner.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
# shellcheck source=../system/common.sh
source "$ROOT/system/common.sh"
tmp=$(mktemp -d)
container="bootstrap-ci-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
image="$container:local"
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  docker image rm "$image" >/dev/null 2>&1 || true
  rm -rf "$tmp"
}
trap cleanup EXIT
echo 'Downloading the pinned Ubuntu WSL filesystem (389 MB).'
download_verified "$UBUNTU_IMAGE_URL" "$UBUNTU_IMAGE_SHA256" "$tmp/ubuntu.wsl"
echo 'Ubuntu checksum verified; importing the fresh filesystem.'
docker import "$tmp/ubuntu.wsl" "$image" >/dev/null
docker run --name "$container" --rm \
  --mount "type=bind,src=$ROOT,dst=/repo,readonly" \
  "$image" /bin/bash -euxo pipefail -c '
    command -v sudo
    command -v git
    command -v curl
    useradd --create-home --shell /bin/bash bootstrap-admin
    printf "bootstrap-admin ALL=(ALL:ALL) NOPASSWD:ALL\n" > /etc/sudoers.d/90-ci-admin
    chmod 0440 /etc/sudoers.d/90-ci-admin
    chmod 0755 /home/bootstrap-admin
    install -d -o bootstrap-admin -g bootstrap-admin -m 0755 /home/bootstrap-admin/.config/gh
    touch /home/bootstrap-admin/.config/gh/hosts.yml
    chown bootstrap-admin:bootstrap-admin /home/bootstrap-admin/.config/gh/hosts.yml
    su - bootstrap-admin -c '\''
      set -eu
      git config --global --add safe.directory /repo
      cd /repo
      export CHEZMOI_GIT_NAME="Bootstrap CI"
      export CHEZMOI_GIT_EMAIL="bootstrap-ci@example.invalid"
      umask 002
      bash install.sh
      python3 ci/wsl-config.py
      sudo -H -u agent python3 /repo/ci/git-credentials-test.py /home/agent/.gitconfig
      sudo -H -u agent chezmoi verify
      bash install.sh
      python3 ci/wsl-config.py
      sudo -H -u agent python3 /repo/ci/git-credentials-test.py /home/agent/.gitconfig
      sudo -H -u agent chezmoi verify
      umask 022
      sudo -H -u agent python3 /repo/ci/git-gh-approval-test.py
      sudo -H -u agent test -d /home/agent/.cache/uv -a -w /home/agent/.cache/uv
      sudo -H -u agent python3 /repo/ci/claude-managed-install-test.py
      sudo bash /repo/ci/vscode-ssh-test.sh
      cd /tmp
      sudo -H -u agent bash /opt/machine-bootstrap/current/checks/distro.sh
      sudo -H -u agent bash /opt/machine-bootstrap/current/checks/boundary.sh
    '\''
  '
