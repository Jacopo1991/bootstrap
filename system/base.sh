#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "$0")/common.sh"
require_root

# Freeze Ubuntu dependency resolution as well as explicitly requested packages.
# Keep the original sources as non-active backups on this fresh distro.
if [[ -f /etc/apt/sources.list ]]; then
  mv /etc/apt/sources.list /etc/apt/sources.list.bootstrap-backup
fi
if [[ -f /etc/apt/sources.list.d/ubuntu.sources ]]; then
  mv /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/ubuntu.sources.bootstrap-backup
fi
cat > /etc/apt/sources.list.d/bootstrap-ubuntu.sources <<EOF
Types: deb
URIs: https://snapshot.ubuntu.com/ubuntu/$APT_SNAPSHOT/
Suites: noble noble-updates noble-security
Components: main universe restricted multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
Check-Valid-Until: no
EOF
apt-get update
apt_locked "$BOOTSTRAP_ROOT/system/apt-base.lock"
apt_key githubcli "$GH_KEY_URL" "$GH_KEY_SHA256" binary
cat > /etc/apt/sources.list.d/bootstrap-gh.list <<'EOF'
deb [arch=amd64 signed-by=/etc/apt/keyrings/githubcli.gpg] https://cli.github.com/packages stable main
EOF
apt-get update
apt_locked "$BOOTSTRAP_ROOT/system/apt-gh.lock"
ln -sfn /usr/bin/fdfind /usr/local/bin/fd

# Preserve unrelated distro settings while converging these five keys.
python3 - <<'PY'
import configparser, pathlib
path = pathlib.Path('/etc/wsl.conf')
config = configparser.ConfigParser(strict=False, interpolation=None)
config.optionxform = str
if path.exists():
    config.read(path)
for section, key, value in [('boot','systemd','true'), ('user','default','agent'),
                            ('interop','appendWindowsPath','false'),
                            ('automount','enabled','false'), ('interop','enabled','false')]:
    if not config.has_section(section):
        config.add_section(section)
    config.set(section, key, value)
with path.open('w') as f:
    config.write(f, space_around_delimiters=False)
path.chmod(0o644)
PY

# Install and enable the isolated VS Code Remote-SSH endpoint.
bash "$BOOTSTRAP_ROOT/system/ssh.sh"
