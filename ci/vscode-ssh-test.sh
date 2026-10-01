#!/usr/bin/env bash
set -euo pipefail

config=/etc/ssh/sshd_config.agentdev
override=/etc/systemd/system/ssh.service.d/agentdev.conf
for setting in \
  "ExecStart=/usr/sbin/sshd -D -f $config" \
  "ExecStartPre=/usr/sbin/sshd -t -f $config"; do
  grep -Fxq -- "$setting" "$override"
done
test -L /etc/systemd/system/ssh.socket
test "$(readlink /etc/systemd/system/ssh.socket)" = /dev/null
/usr/sbin/sshd -t -f "$config"
effective=$(/usr/sbin/sshd -T -f "$config" -C user=agent,addr=127.0.0.1,host=localhost)
for setting in \
  'port 2222' 'passwordauthentication no' 'kbdinteractiveauthentication no' \
  'authenticationmethods publickey' 'permitrootlogin no' 'allowusers agent' \
  'allowtcpforwarding local' 'permitopen 127.0.0.1:*' 'gatewayports no' \
  'allowstreamlocalforwarding no' 'x11forwarding no' 'allowagentforwarding no'; do
  grep -Fxq -- "$setting" <<< "$effective"
done
[[ $(grep -c '^listenaddress ' <<< "$effective") == 1 ]]
grep -Fxq 'listenaddress 127.0.0.1:2222' <<< "$effective"

test_dir=$(mktemp -d)
server_pid=
tunnel_pid=
cleanup() {
  [[ -z $tunnel_pid ]] || kill "$tunnel_pid" 2>/dev/null || true
  [[ -z $server_pid ]] || kill "$server_pid" 2>/dev/null || true
  rm -rf "$test_dir"
}
trap cleanup EXIT
if ! sudo -H -u agent /usr/sbin/sshd -T -f "$config" \
  -C user=agent,addr=127.0.0.1,host=localhost 2>&1 |
  tee "$test_dir/agent-sshd-output" >/dev/null; then
  diagnostic=$(head -n 1 "$test_dir/agent-sshd-output" |
    sed -E 's@/etc/ssh/ssh_host_[[:alnum:]_-]+@<host-key-file>@g')
  printf 'Agent sshd -T diagnostic: %s\n' "${diagnostic:-no stderr}"
  exit 1
fi
ssh-keygen -q -t ed25519 -N '' -f "$test_dir/key" -C ci-vscode-ssh
cat "$test_dir/key.pub" | python3 system/authorize-agent-key.py
first=$(sha256sum /home/agent/.ssh/authorized_keys | cut -d' ' -f1)
cat "$test_dir/key.pub" | python3 system/authorize-agent-key.py
second=$(sha256sum /home/agent/.ssh/authorized_keys | cut -d' ' -f1)
[[ $first == "$second" ]]
[[ $(stat -c '%U:%G:%a' /home/agent/.ssh/authorized_keys) == agent:agent:600 ]]
[[ $(stat -c '%U:%G:%a' /home/agent/.ssh) == agent:agent:700 ]]

# Refuse unsafe symlinks and malformed input without writing through them.
touch "$test_dir/sentinel"
rm -rf /home/agent/.ssh
ln -s "$test_dir" /home/agent/.ssh
if cat "$test_dir/key.pub" | python3 system/authorize-agent-key.py >/dev/null 2>&1; then
  echo 'Symlinked .ssh directory was accepted.' >&2; exit 1
fi
[[ ! -e "$test_dir/authorized_keys" ]]
rm /home/agent/.ssh
mkdir -m 0700 /home/agent/.ssh
chown agent:agent /home/agent/.ssh
ln -s "$test_dir/sentinel" /home/agent/.ssh/authorized_keys
if cat "$test_dir/key.pub" | python3 system/authorize-agent-key.py >/dev/null 2>&1; then
  echo 'Symlinked authorized_keys was accepted.' >&2; exit 1
fi
[[ ! -s "$test_dir/sentinel" ]]
rm /home/agent/.ssh/authorized_keys
printf '%s\n' 'root-managed data' > /home/agent/.ssh/authorized_keys
if cat "$test_dir/key.pub" | python3 system/authorize-agent-key.py >/dev/null 2>&1; then
  echo 'Root-owned authorized_keys was accepted.' >&2; exit 1
fi
[[ $(cat /home/agent/.ssh/authorized_keys) == 'root-managed data' ]]
rm /home/agent/.ssh/authorized_keys
printf '%s\n' 'ssh-rsa AAAA malicious' | python3 system/authorize-agent-key.py >/dev/null 2>&1 && {
  echo 'Wrong key type was accepted.' >&2; exit 1;
}
printf '%s\n%s\n' "$(cat "$test_dir/key.pub")" 'ssh-ed25519 AAAA second-line' |
  python3 system/authorize-agent-key.py >/dev/null 2>&1 && {
    echo 'Multiline key input was accepted.' >&2; exit 1;
  }
cat "$test_dir/key.pub" | python3 system/authorize-agent-key.py
[[ $(stat -c '%U:%G:%a' /home/agent/.ssh/authorized_keys) == agent:agent:600 ]]

install -d -m 0755 /run/sshd
/usr/sbin/sshd -f "$config"
for _ in {1..50}; do
  ss -H -ltn | awk '$1 == "LISTEN" && $4 == "127.0.0.1:2222" { found=1 } END { exit !found }' && break
  sleep 0.05
done
ss -H -ltn | awk '$1 == "LISTEN" && $4 ~ /:2222$/ { found++; if ($4 != "127.0.0.1:2222") bad=1 } END { exit !(found == 1 && !bad) }'

ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -i "$test_dir/key" -p 2222 agent@127.0.0.1 'test "$(id -un)" = agent' >/dev/null 2>&1
if ssh -o BatchMode=yes -o PubkeyAuthentication=no -o PreferredAuthentications=password \
  -o NumberOfPasswordPrompts=0 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -i "$test_dir/key" -p 2222 agent@127.0.0.1 true >/dev/null 2>&1; then
  echo 'Password authentication unexpectedly succeeded.' >&2
  exit 1
fi
if ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -i "$test_dir/key" -p 2222 root@127.0.0.1 true >/dev/null 2>&1; then
  echo 'Root login unexpectedly succeeded.' >&2
  exit 1
fi

python3 - "$test_dir/port" <<'PY' &
import socket, sys
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", 0))
listener.listen()
with open(sys.argv[1], "w", encoding="ascii") as output:
    output.write(str(listener.getsockname()[1]))
connection, _ = listener.accept()
with connection:
    connection.sendall(b"vscode-loopback-ok")
listener.close()
PY
server_pid=$!
for _ in {1..50}; do [[ -s $test_dir/port ]] && break; sleep 0.05; done
target_port=$(cat "$test_dir/port")
ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null -i "$test_dir/key" -p 2222 \
  -L "127.0.0.1:22481:127.0.0.1:$target_port" agent@127.0.0.1 >/dev/null 2>&1 &
tunnel_pid=$!
forwarded=0
for _ in {1..50}; do
  if python3 -c 'import socket; s=socket.socket(); s.settimeout(.2); s.connect(("127.0.0.1",22481)); print(s.recv(64).decode()); s.close()' 2>/dev/null | grep -qx vscode-loopback-ok; then
    forwarded=1
    break
  fi
  sleep 0.05
done
[[ $forwarded == 1 ]] || { echo 'Loopback TCP forwarding failed.' >&2; exit 1; }
wait "$server_pid"
echo 'PASS: key-only agent login, root/password denial and VS Code loopback forwarding.'
