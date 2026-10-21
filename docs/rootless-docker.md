# Rootless Docker for evaluations

Evaluations that run model-written code (Inspect AI) use a Docker daemon that runs **as `agent`**, with its socket at `/run/user/1001/docker.sock` and its data under `~/.local/share/docker-rootless`. The normal root Docker (`docker.service`, `docker.socket`, `containerd.service`) stays disabled, and `agent` is not in the `docker` group.

## Why lanes cannot use it

A Docker socket gives the full power of the `agent` user: a container can bind-mount any path the agent can read and write anywhere the agent can. That would bypass the Claude and Codex lane sandboxes and the policy hook. So only unsandboxed agent processes reach the socket (the PM shell, a user unit, `task`). Lane sessions do not:

- The Claude sandbox blocks creating Unix sockets, and its settings have no `allowUnixSockets` entry.
- The policy hook denies `docker`, `podman`, `nerdctl` and any `DOCKER_HOST`/`DOCKER_*` setting in lane sessions.
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

## Live checks after install

These are not part of the gate; the PM runs them once after `gpu-docker.sh` and the chezmoi apply:

1. `systemctl --user start docker-rootless`, then `bash checks/boundary.sh` (the socket must be accepted).
2. `python3 -I checks/docker-containment.py check --live --sandbox 'codex sandbox'` while the daemon runs, and the same without `--live` and with the daemon stopped (it listens at the path itself).
3. In a real Claude lane and a real Codex lane, run `python3 -I checks/docker-containment.py probe` while `serve` runs in the PM shell; no connection may be printed.
4. The Inspect example above, then stop the daemon.
