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

The agent's managed Git config declares the same GitHub and Gist credential
helper as `gh auth setup-git`: an empty helper reset followed by
`!/usr/bin/gh auth git-credential` for each host. Reapplying the bootstrap
preserves these helper settings and keeps `chezmoi verify` clean. This stores
only helper configuration; it does not authenticate an account or touch tokens.

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

The agent user's configured hooks and sandbox never grant access to the Windows host or mounted Windows drives. For a one-off task involving D:, open the admin explicitly with
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

The distro check validates exact versions (including `bubblewrap`, `socat`, and Gitleaks), takes content/mode/mtime snapshots
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

The compaction task installer copies the checker, common script, and inventory
cache helper into an admin-only `C:\ProgramData\machine-bootstrap` directory
and converges one task per distro. It runs on the first day of every month at
03:30 local time as the distro owner's account with highest privileges (S4U, no
stored password), and appends to `C:\ProgramData\machine-bootstrap\compact.log`.
The owner must be an administrator. The task only recommends compaction: it
never trims, stops a distro, or calls diskpart. For a running WSL 2 distro, the
check reads live Linux used/cap bytes. For a stopped WSL 2 distro, it uses the
newest matching inventory record from the current date and previous 29 dates, matched
by distro name and VHDX path. It reports the source timestamp and applies the
same thresholds when cached values are available. Without a usable cache it
logs the VHDX file size and makes no recommendation. It recommends
`windows\compact-distro.ps1 -Name <name>` when the VHDX exceeds Linux used
space by more than 50 GiB or exceeds 90% of the recorded filesystem cap. No
scheduled task is installed automatically during creation.

### Nightly Windows inventory

From elevated Windows PowerShell as the account that owns WSL, register one
idempotent nightly task:

```powershell
.\windows\install-inventory-task.ps1
```

It runs every day at 02:30 local time under that account with highest privileges
using S4U, so Task Scheduler stores no password. The fixed scripts are copied
to the protected `C:\ProgramData\machine-bootstrap` directory. Run the
inventory manually with `.\windows\inventory.ps1` or read the latest local
record at `C:\ProgramData\machine-bootstrap\inventory\latest.json`.

Each run writes a UTF-8 JSON record named `yyyy-MM-dd.json` and updates
`latest.json` atomically. It retains dated files from today through 29 days earlier, even when some
calendar dates have no record. It deletes only older root-level files whose
names exactly match that date format. The versioned record contains WSL distro names, states, versions,
VHDX paths and file sizes; C: and D: free bytes; WSL and NVIDIA driver versions;
pending-reboot flags; MachineBootstrap task names and states; and filesystem
usage plus the ten largest direct subdirectories under `/home` and `/var`
for each running WSL 2 distro. Directory values are allocated bytes and paths;
the scan does not read file contents. A stopped distro is never started. Its
new record carries the newest matching cached usage, cap, and directory summary
when available, with the source date and timestamp visible in the JSON.

Each running distro probe has a 60-second wall limit. Native-command stderr is
discarded and failures use fixed status values; command diagnostics, environment
values, and file contents are not written to inventory. The inventory stays on
the Windows machine under the protected ProgramData directory. These values
describe the host at collection time and do not certify that a stopped distro's
cached measurements are current beyond the retained record window.

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

### Design step 10 — minimal agent configuration and drift report

The existing chezmoi apply manages Codex and Claude Code's global instructions,
native permissions, and one shared pre-tool policy hook. Codex uses the supported `on-request` approval policy, routes approvals to
the user, and sets `workspace-write` with sandbox networking off. The retired
`untrusted` policy is not valid in the pinned Codex version.
Claude Code keeps its normal ask-before-running mode and uses the native Linux
sandbox with unsandboxed retries disabled and startup failing if the sandbox is
unavailable. The base image pins the sandbox's `bubblewrap` and `socat`
dependencies. Claude's optional seccomp add-on is not installed; Windows interop
and drive automount remain disabled by the distro baseline.

The policy hook blocks Windows/host commands, file-tool writes outside the
current repository, shell writes outside that repository, and commits whose
staged diff cannot pass the pinned Gitleaks scan. The Codex hook is user-managed:
after first apply or any hook change, inspect and trust the exact hook definition
with Codex's `/hooks` command before relying on it. Codex skips an untrusted
hook. Do not use a hook-trust bypass. Hooks are additional guardrails; the
Codex workspace sandbox and Claude native sandbox enforce subprocess boundaries.

Approved code roots are `/home/agent/dev_workspace/<repo>`; Cortex knowledge
repositories belong in sibling paths under `/home/agent/cortex/<repo>`, never
inside code repositories. Runtime data belongs under
`/home/agent/project-data/<project>` and is never a Git repository.
`CustomerHarness` and `Typo3/DKM` are founder-approved local-only code
projects: keep them under the code root without a GitHub remote or publishing
their contents. Backups under `C:\backups\<project>` are Windows-owner copies;
the agent cannot access them. Other code clones under `Documents\Codex` or
outside the approved roots are drift.

Shared skills, MCP servers, plugins, and role-scoped availability start empty.
Candidate names are listed in this pull request for founder review; nothing from
the private toolkit inventory is installed or linked. The optional Superpowers
marketplace is outside toolkit sync and the pilot-critical path. Add shared
components only after a demonstrated need, progressing through CI, hooks and
permissions, role-scoped availability, self-triggering skills, then task
pointers. No automatic removal is performed; restore and security recovery
remain available.

The existing nightly Windows inventory task is reused for drift collection; no
new task or scheduler is installed. It queries only distros already running and
records a stopped distro as `SKIP` without starting it. For running distros,
the drift check reports dirty `chezmoi verify`/`chezmoi diff`, unexpected
globally installed agent CLIs, npm packages, uv tools, skills, MCP servers or plugins, and task/job
configuration outside the approved roots. Any custom task whose launcher or working directory does not match the
approved roots is reported as drift, never clean.
Windows task output contains sanitized task names and fixed action/working-root
classifications only; raw arguments, command lines and secret values are
discarded. MachineBootstrap inventory and compaction tasks remain the
owner-managed maintenance exception. Project workflow hooks, inbox dispatch,
and scheduled PM automation stay deferred until design step 14.

### VS Code Remote-SSH for AgentDev

The bootstrap installs OpenSSH Server and enables the systemd service for the non-sudo agent account. Its dedicated policy listens only on 127.0.0.1:2222, accepts Ed25519 public-key authentication, and permits local TCP forwarding to 127.0.0.1 for VS Code Remote-SSH. Password and keyboard interactive login, root login, remote TCP forwarding, agent forwarding, X11, and stream-local forwarding are disabled. Ubuntu socket activation is masked to prevent an extra wildcard listener. Windows mounts and WSL interop remain disabled for the agent session.

Run windows/setup-vscode-ssh.ps1 from a normal Windows PowerShell window as the WSL owner. It creates the key pair in %USERPROFILE%/.ssh only when both files are absent, streams only the public key to the distro as root, and adds or updates only the Host agentdev stanza in the owner's SSH config. It preserves existing keys and fails closed if the pair is incomplete, mismatched, or protected by a passphrase. Linux stores the key under /home/agent/.ssh/authorized_keys, owned by agent with mode 0600; the .ssh directory uses mode 0700.

The managed Host block uses `ProxyCommand C:\Windows\System32\wsl.exe -d AgentDev -u agent -- nc 127.0.0.1 2222`. Connecting starts a stopped AgentDev and keeps the WSL relay running for the SSH session, as reported by the owner during host setup. Existing connection directives stay in place; a conflicting global ProxyCommand or ProxyJump must be moved into another host stanza first.

Install the Microsoft VS Code Remote - SSH extension. In VS Code press F1, choose Remote-SSH: Connect to Host, then select agentdev. The first connection installs VS Code Server in the AgentDev Linux home. Open Linux projects under /home/agent/dev_workspace/<repo>. The extension host runs inside WSL without requiring Windows mounts or interop in the agent session.

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
| ShellCheck, agent/CI Gitleaks and release SHA-256 | `home/.chezmoitemplates/pins.env` |
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
the distro and boundary checks as agent. Hosted lint also tests blocking controls in the agent hook and exercises a synthetic, never-reusable secret-scanner canary without storing or logging its value. The container explicitly skips the
WSL-only runtime checks. This avoids the
hosted runner's preinstalled PPAs and tools masking fresh-image failures. The
single **gate** job runs with `always()` and fails if any required job failed,
was cancelled or was skipped. A `windows-latest` job explicitly runs Windows
PowerShell 5.1 tests with WSL, diskpart and scheduling mocked, covering config
merge/backup, creation/cap checks, refusal rules and idle skipping. It is required
by the gate. CI cannot establish physical VHDX compaction or
physical NVIDIA GPU behavior; run those host checks in steps 7 and 8.

### Monthly pin review

The monthly pin workflow runs on the first day of each month at 01:30 UTC. It
resolves the Ubuntu snapshot and base apt locks together, and resolves each
vendor apt lock against that vendor's own signed repository. It refreshes exact
stable releases when the official release artifact can be identified and
checksum-pinned, keeps Node on the 24 LTS line and Python on 3.12, and regenerates
the full npm lock with lockfile version 2 before requiring `npm ci` to pass.
Every unresolved component stays at its previous pin and is listed in the
workflow summary and pull request body. Claude Code's moving installer/version pairing is not resolved by this updater and
requires manual review; signing-key rotations remain pinned for manual review.

The workflow explicitly dispatches the existing `gate.yml` workflow against the
candidate branch before publishing a pull request, and accepts only a new
`workflow_dispatch` run whose workflow, branch and head SHA match exactly. A new
pull request is created only after that gate passes. An existing same-branch
review receives a clear failure report if preflight fails. The branch is checked
before publication and the published PR head is checked against the gate-tested
commit. It never merges a pull request. The update job uses
the repository's built-in Actions token with contents, pull request and Actions
write permission; it does not require a stored token. A repository administrator
must enable **Allow GitHub Actions to create and approve pull requests**. Keep
the existing `gate` job required and allow the `workflow_dispatch` event under
any Actions execution policy. A manual run defaults to a read-only candidate
preview; select the non-preview option only when a pull request is intended.


### Repository commands and approved network access

Claude Code keeps its sandbox enabled, its general unsandboxed retry disabled,
regular permission prompts, the existing deny list and PreToolUse hook. Only
`git` and `gh` are listed in `sandbox.excludedCommands`; explicit ask rules retain
prompts for these tools. This is the documented exception even with
`allowUnsandboxedCommands: false`; no general domain/network allow is added.
The native GitHub CLI may use its existing credential store; agents must never
read or print credential files or values.

Codex keeps `workspace-write`, `network_access = false`, `on-request` and the
user approval reviewer. Prompt-only execution rules cover git/gh. Its
PermissionRequest hook denies other Bash escalations and returns no grant for
git/gh, so the native user prompt still decides the exact request. This also
denies non-git/gh shell escalations for filesystem access; ordinary commands
inside the sandbox continue through PreToolUse. Never approve broad shell or
persistent allow prefixes as a substitute for the exact repository operation.

The shared policy permits log, show, diff, status, rev-parse, ls-files, grep and
blame against repositories under `/home/agent/dev_workspace` and
`/home/agent/cortex`. Other Git subcommands remain limited to the current repo.
Cross-repository reads use `git --no-pager --no-optional-locks -C <repo>`
so no pager runs and status cannot refresh another repository's index. Add
`--no-textconv` to cross-repository log, show, diff and blame, and
`--no-ext-diff` to log, show and diff. Spell short options separately; aggregated
short flags and external input-file options are refused. Current-repository
Git commands retain ordinary use; global/file configuration writes, new
repository/worktree creation, output-file primitives and local filesystem
remotes require separate setup and are blocked. Push, fetch and pull require
one explicit named remote; its effective URLs (including pushurl, insteadOf
and pushInsteadOf rewrites)
must be GitHub HTTPS or SSH. No remote connection is made by the policy check.
Common flags such as `git push -u origin <branch>` work. Supported fetch/pull
value options use `--key=value`; custom transport/push options, direct URL
operands and unspecified remotes are refused rather than guessed.
Repeated and attached `-C` selectors resolve sequentially; symlink escapes,
config/git-dir/work-tree overrides and side-effecting cross-repo read options
are refused. The existing host-command, file-write and staged-secret checks
remain in force. This hook is a command guard, not an OS boundary against
arbitrary programs or a replacement for reviewing the exact approval.

Sources checked 2026-10-01: [Claude sandbox modes and exclusions](https://code.claude.com/docs/en/sandboxing#the-unsandboxed-retry-escape-hatch),
[Codex approvals](https://learn.chatgpt.com/docs/agent-approvals-security),
[Codex execution rules](https://learn.chatgpt.com/docs/agent-configuration/rules),
and [Codex PermissionRequest](https://learn.chatgpt.com/docs/hooks#permissionrequest).
Pinned Codex `0.159.2` exposes the same PermissionRequest event and deny output;
CI checks the managed hook and uses the installed CLI's execution-policy checker.
No authentication, GitHub write or owner-host check is run by these tests.
