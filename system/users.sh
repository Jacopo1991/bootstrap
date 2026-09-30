#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "$0")/common.sh"
require_root
admin=${1:-${SUDO_USER:-}}
[[ $admin =~ ^[a-z_][a-z0-9_-]*$ && $admin != agent && $admin != root ]] || {
  echo 'Supply the existing non-root admin username.' >&2; exit 1;
}
getent passwd "$admin" >/dev/null
admin_home=$(getent passwd "$admin" | cut -d: -f6)
[[ $admin_home == /home/* && -d $admin_home && ! -L $admin_home ]] || {
  echo 'Admin must have a real home directory under /home.' >&2; exit 1;
}
[[ $(stat -c %u "$admin_home") == "$(id -u "$admin")" ]]
id agent >/dev/null 2>&1 || useradd --create-home --shell /bin/bash agent
[[ $(id -u agent) != 0 && $(getent passwd agent | cut -d: -f6) == /home/agent ]]
usermod --shell /bin/bash --lock agent
for group in sudo docker; do
  if id -nG agent | tr ' ' '\n' | grep -qx "$group"; then
    [[ $(id -gn agent) != "$group" ]] || { echo 'Unsafe agent primary group.' >&2; exit 1; }
    gpasswd --delete agent "$group"
  fi
done
# Remove access and default ACLs before enforcing home isolation.
setfacl --remove-all --remove-default "$admin_home"
chmod 0700 "$admin_home"
chmod 0700 /home/agent
install -d -m 0755 /etc/bootstrap
printf '%s\n' "$admin" > /etc/bootstrap/admin-user
printf '%s\n' "$admin_home" > /etc/bootstrap/admin-home
chmod 0644 /etc/bootstrap/admin-{user,home}
printf 'agent ALL=(ALL:ALL) !ALL\n' > /etc/sudoers.d/99-bootstrap-agent
chmod 0440 /etc/sudoers.d/99-bootstrap-agent
visudo -cf /etc/sudoers
