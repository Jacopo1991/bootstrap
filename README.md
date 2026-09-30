# Public Ubuntu 24.04 WSL bootstrap

Design step 3 implementation. Supports fresh **Ubuntu 24.04 amd64**, a non-root
admin and a dedicated unprivileged `agent` account. No account authentication or
secret provisioning is performed. Only secret names, such as `BWS_ACCESS_TOKEN`,
`ANTHROPIC_API_KEY` and `OPENAI_API_KEY`, belong in public documentation; values
must never enter this repository, command examples, logs or CI.

## Run order

### Design step 6 — create the distro and public machine baseline

From a Windows checkout of this repository in PowerShell:

```powershell
.\windows\new-distro.ps1 -Name AgentDev -MaxSizeGB 800
wsl -d AgentDev
```

First launch runs Ubuntu's setup. Create your **admin** username and password;
do not call this user `agent`. Inside that admin session:

```bash
git clone https://github.com/Jacopo1991/bootstrap.git
cd bootstrap
bash install.sh
```

`install.sh` runs `system/base.sh` and `system/users.sh` through sudo, installs
checksum-pinned chezmoi and mise in `/usr/local/bin`, publishes only the committed
public source under `/opt/machine-bootstrap`, then runs **chezmoi init --apply as
agent**. `.chezmoiroot` selects `home/`. The Git name and email prompts create one
identity in the agent's Git config. CI supplies non-secret identity inputs with
`CHEZMOI_GIT_NAME` and `CHEZMOI_GIT_EMAIL`; normal installs prompt once.

The checkout must be clean and committed. Rerun `bash install.sh` as admin to
converge the same revision again. There is no authentication, automatic upgrade
or GPU/Docker installation in this entrypoint.

Exit Linux, terminate **only this distro**, then launch it again:

```powershell
wsl --terminate AgentDev
wsl -d AgentDev
```

The default user is now `agent`, systemd is enabled, and Windows drive automount,
Windows binary interop and Windows PATH import are disabled. Terminating and
restarting the distro is required for these settings to take effect, including
when updating an existing installation. The admin's Linux home has mode `0700` with its access/default ACLs
removed. Agent is locked for password login, absent from sudo/docker groups,
and explicitly denied sudo. Agent login via `wsl -d AgentDev` still works.

For a one-off task involving D:, open the admin explicitly with
`wsl -d AgentDev -u youradmin`. Use a private mount namespace so the agent's
sessions never receive the drive mount:

```bash
sudo unshare --mount --propagation private /bin/bash
mkdir -p /mnt/d
sudo mount -t drvfs D: /mnt/d
# Perform the admin task here; do not start agent processes in this namespace.
sudo umount /mnt/d
exit
```

Unmount immediately after the task. Do not enable automount or interop for it.

### Design step 7 — verify the tools and user boundary

As `agent`:

```bash
bash /opt/machine-bootstrap/current/checks/distro.sh
bash /opt/machine-bootstrap/current/checks/boundary.sh
```

The distro check validates exact versions, takes content/mode/mtime snapshots
of managed files and installed tools around another `chezmoi apply`, then requires
an empty diff and clean `chezmoi verify`. Installation pins umask to `022` for
both generated chezmoi configuration and the initial apply, so the admin
session's umask cannot change managed file modes. CI starts installation under
umask `002` and requires the distro check to pass.

The boundary check requires
`sudo -n true` to fail, checks groups, and attempts to access the admin home and
open `~admin/.config/gh/hosts.yml` without printing any file contents. It also
rejects an accessible Docker socket when one exists. On WSL it fails for a
Windows-drive mount, a reachable `/mnt/c/Windows`, `WSL_INTEROP` in the agent login
environment, or an `/run/WSL/<pid>_interop` socket belonging to a process in
the agent login's ancestry (including WSL's root-owned `/init`). The
`WSLInterop` binfmt registration status is not authoritative: it can remain
enabled while Windows PE execution is blocked. Outside WSL the check explicitly
prints `SKIP: WSL-only checks (not WSL)`; that skip does not verify the live WSL
boundary.

From the Windows host, run the owner-side execution probe:

```powershell
.\windows\interop-probe.ps1 -Name AgentDev
```

It copies Windows `whoami.exe` to a unique path under the distro's `/var/tmp`,
runs it as `agent`, and requires a nonzero exit with no stdout. It bounds the
launch wait and removes the temporary executable even when the check fails.

From PowerShell, with other work in this distro saved:

```powershell
.\windows\vhdx-size.ps1
.\checks\compact-check.ps1 -Name AgentDev -MaxSizeGB 800
.\windows\install-compaction-task.ps1 -Name AgentDev
```

Run the compaction check and task installer in **elevated Windows PowerShell as
the Windows account that owns the distro**. Save all work in that distro first.
The check verifies the filesystem cap, writes **5 GiB of real random data** into
a unique temporary file, deletes only that file, syncs, and compacts. It requires
the VHDX file length to shrink by at least 4 GiB. Ensure at least 6 GiB is free
inside Linux and sufficient free space on the Windows disk. Cleanup runs on
failure too. `vhdx-size.ps1` reports file/allocated bytes and Linux used/cap bytes;
reading Linux usage starts a stopped WSL 2 distro. WSL 1 has empty size fields.

### VHD cap and monthly compaction checks

Sparse VHD mode is never enabled. The [WSL 2.5.6 release note](https://github.com/microsoft/WSL/releases/tag/2.5.6)
puts sparse VHD support behind `--allow-unsafe`; WSL warns of potential data
corruption. This setup uses a normal dynamic VHDX with an **800 GiB default cap**
and explicit compaction instead.

Before a fresh install, `new-distro.ps1` merges `[wsl2] defaultVhdSize=800GB` into
`%USERPROFILE%\.wslconfig`, preserving other keys and comments. An existing file
is backed up beside it before any change. Rerunning the same merge changes
nothing. `-MaxSizeGB` selects another cap (GB here means 1024 cubed bytes).
This global default affects future VHDs, not existing ones. If the WSL VM is
already running, the change may not be active yet: save your other WSL work and
stop it yourself before creation. The installer does not stop other distros;
it reads `df -B1` as root and fails if the new filesystem exceeds the requested
cap. It also refuses sparse VHDXs and an existing `sparseVhd=true` configuration.
Keep `sparseVhd` off. Raising an existing cap later uses
`wsl --manage AgentDev --resize 1000GB`; follow Microsoft's
[disk-space guidance](https://learn.microsoft.com/en-us/windows/wsl/disk-space).

Manual compaction: `.\windows\compact-distro.ps1 -Name AgentDev`.
It refuses missing/WSL 1/sparse disks and any other running WSL distro, runs root
`fstrim /`, terminates only the named distro, waits up to 300 seconds for
exclusive read access to its VHDX, and uses diskpart to attach read-only,
compact, then detach. It logs file bytes before/after. It never shuts down all
of WSL or operates on another distro.

The task installer copies the checker and common script into an admin-only
`C:\ProgramData\machine-bootstrap` directory and converges one task per distro;
rerunning it replaces the previous weekly task.
It runs on the **first day of every month at 03:30 local time**, under the distro
owner's account with highest privileges (S4U, no stored password), and appends
to `C:\ProgramData\machine-bootstrap\compact.log`. The owner must be an
administrator. This task only recommends compaction: it never trims, stops a
distro, or calls diskpart. For the selected distro, it always logs the VHDX file
size. It reads Linux used/cap bytes only when that WSL 2 distro is already
running; stopped distros log only file size and receive no recommendation.
It appends `COMPACTION RECOMMENDED: run windows\compact-distro.ps1 -Name <name>`
when file size exceeds Linux used space by more than 50 GiB or exceeds 90% of the
filesystem cap. The monthly trigger uses Task Scheduler's
[ScheduleByMonth schema](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-daysofmonth-monthlyscheduletype-element).
No scheduled task is installed automatically during creation.

This checks the requested Linux user boundary. The Windows account that owns WSL
can still launch the distro as root; this is not a boundary against that owner.

### Design step 8 — separately install and verify GPU containers

Install/update the **Windows NVIDIA driver** on the host before this step. Never
install a Linux NVIDIA driver, the `cuda` or `cuda-drivers` meta-packages, a system
CUDA toolkit, or Docker Desktop for this setup. WSL supplies the driver bridge;
the GPU tests use CUDA runtime libraries packaged in PyTorch wheels.

Open the admin explicitly from PowerShell (replace `youradmin`):

```powershell
wsl -d AgentDev -u youradmin
```

Then, inside Linux:

```bash
sudo bash /opt/machine-bootstrap/current/system/gpu-docker.sh
bash /opt/machine-bootstrap/current/checks/gpu.sh
```

The separate installer adds Docker Engine from Docker's apt repository and the
NVIDIA Container Toolkit from NVIDIA's repository. It masks Docker during package
installation and leaves **docker.service and docker.socket disabled and stopped**
afterwards. Containerd is also disabled at boot. Agent never joins the Docker
group. The GPU check runs as admin, runs host Python inference as agent, starts
Docker explicitly for the container check, and stops Docker/socket/containerd
on exit. No persistent daemon startup is enabled.

Both host and container must pass `nvidia-smi`, report `sm_120` in
`torch.cuda.get_arch_list()`, use the expected `(12, 0)` card on `cuda:0`, perform
and compare a CUDA matrix multiply, and run a tiny public GPT-2 model on GPU.
The model revision is immutable, safetensors are required and remote model code
is disabled. The exact same assertions and hash-locked Python dependencies run
in the digest-pinned Python container. These checks download public wheels and
model files; no model service or credentials are used.

## Pins and repeatability

| Component | Pin location |
| --- | --- |
| Ubuntu WSL image (24.04.5 amd64) and SHA-256 | `windows/new-distro.ps1` |
| chezmoi, mise, Node 24 LTS, Python 3.12, uv, native Claude Code, bws, SecretSpec | `home/.chezmoitemplates/pins.env` |
| Node/Python/uv global tools | `home/dot_config/mise/config.toml` |
| Codex CLI and ccusage, including npm dependency versions/integrities | `home/dot_local/share/bootstrap/npm/package-lock.json` |
| Explicit apt package versions (held after install) | `system/apt-*.lock` |
| Ubuntu dependency resolution | Signed Ubuntu snapshot `20260930T000000Z` |
| apt signing key hashes | `home/.chezmoitemplates/pins.env` |
| GPU Python packages and all dependencies with hashes | `checks/gpu-requirements.lock` |
| GPU container image | `checks/gpu-image.env` |
| Public test model | Revision in `checks/gpu-smoke.py` |
| ShellCheck, Gitleaks and release SHA-256 | `home/.chezmoitemplates/pins.env` |
| GitHub Actions checkout | Full commit SHA in the workflow |

Claude Code uses the official native installer with an exact release argument
and a checked installer hash. Its auto-updater is disabled in agent shell startup
and install/check processes. A changed upstream installer or signing key fails
closed until the public pin is reviewed. mise installs exact versions; npm uses
`npm ci`, and GPU dependencies use `uv pip sync --require-hashes`. Updating pins
is a reviewed source change, never a floating `latest` install. Keep the mise
config and release pins aligned when updating versions.

The Ubuntu snapshot replaces the distro's standard Ubuntu sources (saved as
inactive backups), freezes apt dependencies and deliberately does not take
unreviewed security upgrades. Refresh that snapshot and its package locks in a
new reviewed update when needed. Vendor apt packages are selected by exact
version; missing versions cause installation to fail rather than pick a newer one.

## Windows flag verification and minimum WSL version

**Minimum supported WSL version for these scripts: 2.5.0** (the separately
serviced WSL package, distinct from a distro using WSL 2). Scripts also check
required flags in the installed `wsl --help`. Sources verified 2026-09-30:

- Microsoft [Basic commands](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)
  documents `--location`, `--no-launch` and distro termination.
- Microsoft [Build a custom distro](https://learn.microsoft.com/en-us/windows/wsl/build-custom-distro)
  documents `--from-file` and `--name`; modern distro support begins at 2.4.4.
- Microsoft [Disk space](https://learn.microsoft.com/en-us/windows/wsl/disk-space)
  documents the supported `--manage` baseline as 2.5 and higher.
- Microsoft [WSL configuration](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)
  documents `defaultVhdSize` for new virtual disks.
- Microsoft [compact vdisk](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/compact-vdisk)
  requires a detached or read-only attached dynamic VHD.
- Canonical [Install Ubuntu on WSL 2](https://ubuntu.com/wsl/docs/latest/howto/install-ubuntu-wsl2/)
  specifies 2.4.10 or higher for its modern Ubuntu distro format.

The resulting command is `wsl --install --from-file <pinned-image> --name <name>
--location D:\wsl\<name> --no-launch`, followed by `wsl --set-version <name> 2`
only when needed, a cap read-back and `wsl --terminate <name>`. An existing name is reused only if it is
already WSL 2 at the requested path and has the matching image marker written by
this script; no distro is unregistered or moved.

Installer references: [chezmoi](https://www.chezmoi.io/),
[mise](https://mise.jdx.dev/), [Claude native installation](https://code.claude.com/docs/en/setup),
[Codex CLI](https://developers.openai.com/codex/cli/),
[Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/), and
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html).

## CI

GitHub-hosted `ubuntu-24.04` runs pinned ShellCheck/Gitleaks. It imports the same
checksum-pinned Ubuntu WSL filesystem into a disposable Docker container, creates
an admin with an empty gh credential-file fixture, runs `install.sh` twice as
that admin, asserts all five `/etc/wsl.conf` keys after each install, then runs
the distro and boundary checks as agent. The container explicitly skips the
WSL-only runtime checks. This avoids the
hosted runner's preinstalled PPAs and tools masking fresh-image failures. The
single **gate** job runs with `always()` and fails if any required job failed,
was cancelled or was skipped. A `windows-latest` job explicitly runs Windows
PowerShell 5.1 tests with WSL, diskpart and scheduling mocked, covering config
merge/backup, creation/cap checks, refusal rules and idle skipping. It is required
by the gate. CI cannot establish physical VHDX compaction or
physical NVIDIA GPU behavior; run those host checks in steps 7 and 8.
