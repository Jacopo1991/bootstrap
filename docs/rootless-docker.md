# Rootless Docker for evaluations

Evaluations that run model-written code (Inspect AI) use a Docker daemon that runs **as `agent`**, with its socket at `/run/user/1001/docker.sock` and its data under `~/.local/share/docker-rootless`. The normal root Docker (`docker.service`, `docker.socket`, `containerd.service`) stays disabled, and `agent` is not in the `docker` group.

## Why lanes cannot use it

A Docker socket gives the full power of the `agent` user: a container can bind-mount any path the agent can read and write anywhere the agent can. That would bypass the Claude and Codex lane sandboxes and the managed deny rules. So only unsandboxed agent processes reach the socket (the PM shell, a user unit, `task`). Lane sessions do not:

- The Claude sandbox blocks creating Unix sockets, and its settings have no `allowUnixSockets` entry.
- The Codex `workspace-write` sandbox refuses AF_UNIX sockets in its built-in seccomp filter, with network access on or off (measured 2026-10-22: `socket() EPERM`, zero connections counted at the listener). No Codex setting exists to add; the live containment check below is what keeps this true.
- The policy hook that denied `docker`, `podman`, `nerdctl` and `DOCKER_*` settings was removed in decision-38. Native rules only deny reading `~/.docker/config.json`; the two sandbox facts above are what keep lanes off the socket.
- `checks/docker-containment.py` proves it from inside each sandbox; run it after install (see below).

## Start an evaluation

The daemon is started by hand (`docker-rootless.service` has no `[Install]` section, so it cannot be enabled, and it has no socket unit). Run these in an unsandboxed agent shell, not in a lane:

```bash
systemctl --user start docker-rootless
export DOCKER_HOST=unix:///run/user/1001/docker.sock
inspect eval docs/inspect/no_network_probe.py --model mockllm/model
systemctl --user stop docker-rootless
```

The probe task needs no model key. Its container (`docs/inspect/compose.yaml`) has `network_mode: none`, no bind mounts and no privileged settings; the task passes only if the container cannot open a connection. Stop the daemon afterwards, and run `bash checks/boundary.sh`: it passes with the daemon stopped or running.

## Live containment check (required)

The containment check is **required before first use and again after every Codex or Claude Code upgrade**, because the guarantee comes from the vendors' sandboxes and an upgrade can change it. Rootless Docker is not cleared for use until it passes. Run it from the unsandboxed PM shell (not a lane):

```bash
python3 -I checks/docker-containment.py check \
  --sandbox 'codex sandbox' \
  --sandbox 'codex sandbox -c sandbox_mode="workspace-write"' \
  --sandbox 'codex sandbox -c sandbox_mode="workspace-write" -c sandbox_workspace_write.network_access=true'
```

It must print `PASS: every sandbox failed to reach the Docker socket.` With the daemon running, add `--live` so it uses the real socket instead of its own listener. Then prove it from a real Claude lane and a real Codex lane: run `python3 -I checks/docker-containment.py serve` in the PM shell, run `python3 -I checks/docker-containment.py probe` from a Bash call inside each lane, and confirm that `serve` prints no connection. If any check fails, stop and do not use rootless Docker; never loosen a sandbox to make it work.

## Other live checks after install

These are not part of the gate; the PM runs them once after `gpu-docker.sh` and the chezmoi apply:

1. `systemctl --user start docker-rootless`, then `bash checks/boundary.sh` (the socket must be accepted).
2. The Inspect example above, then `systemctl --user stop docker-rootless`.
