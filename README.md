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
rejects an accessible Docker socket when one exists, with one exception: agent's
rootless socket `/run/user/1001/docker.sock` passes only when agent owns it, it is
not group- or world-accessible, and a process running as agent serves it (any other
Docker or containerd socket, or any other path, fails). On WSL it fails for a
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
values, and file contents are not written to inventory. The dated records stay
on the Windows machine under the protected ProgramData directory. These values
describe the host at collection time and do not certify that a stopped distro's
cached measurements are current beyond the retained record window.

#### Inventory copy into AgentDev

PM lanes read the newest snapshot from
`/home/agent/project-data/inventory/latest.json`. The direction is inverted so
that the inventory never starts AgentDev: Windows only writes the same sanitized
`latest.json` to its own `C:\ProgramData\machine-bootstrap\export` folder (never
the dated history), and calls neither `wsl.exe` nor `\\wsl.localhost` for it. The
inventory task installer creates that folder with a protected ACL: SYSTEM and
Administrators have full control and the owner account has read-only access,
because a non-elevated token carries Administrators as deny-only. Re-run
`.\windows\install-inventory-task.ps1` once to add `-ExportDirectory` to the task.

Inside AgentDev, `system/inventory-mirror.sh` (run by `install.sh`) installs a
root-owned `inventory-mirror.timer` that starts `inventory-mirror.service` two
minutes after boot and every 15 minutes after. A timer only fires while the
distro already runs, so it can never wake a stopped one. The service mounts the
export folder read-only (`mount -t drvfs`, `uid=0,umask=077`) in its own private
mount namespace and unmounts it afterwards, so automount stays off, the agent's
sessions never see a Windows mount, and the boundary check still passes.
`system/inventory-mirror.py` refuses to mount in the shared namespace by comparing
its mount namespace with PID 1's, which needs `CAP_SYS_PTRACE` in the unit's
bounding set; if PID 1 cannot be read it refuses too. Any failure also leaves one
line (UTC time and the error), including a failed unmount, in
`~/project-data/inventory/last-error`, which a copy that unmounted cleanly removes.

The copy runs as root but only into agent-owned directories. It refuses a
symlink, hardlink, FIFO or other non-regular file as the source and as an
existing `latest.json`, refuses a symlinked `project-data` or `inventory`
directory or one not owned by `agent`, requires UTF-8 JSON that looks like an
inventory snapshot (schema 1 or 2, at most 4 MiB), and publishes the exact bytes
by writing a `0600` temporary file in the same directory and renaming it over
`latest.json`. The result is owned by `agent` with mode `0600`; `project-data`
and `inventory` are `0700`. A missing source (no inventory run yet) is not an
error. Any refusal fails the service and leaves the previous snapshot in place.
The agent still cannot read Windows paths. Check the timer with
`systemctl list-timers inventory-mirror.timer` and the last run with
`systemctl status inventory-mirror.service`.

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

The same step also installs `docker-ce-rootless-extras` (pinned to the engine's
version) and `uidmap`, and adds agent's `/etc/subuid` and `/etc/subgid` ranges only
if they are missing. On a machine where Docker is already installed, run
`sudo bash /opt/machine-bootstrap/current/system/gpu-docker.sh --rootless-only`: it
leaves the NVIDIA toolkit and the root daemon's configuration alone. Evaluations
(Inspect AI, installed from a hashed lock as `inspect`) use agent's own rootless
daemon, `docker-rootless.service`, which is started by hand from an unsandboxed
agent shell and cannot be enabled. Lane sandboxes must not reach its socket; see
[docs/rootless-docker.md](docs/rootless-docker.md) for starting an evaluation and
for `checks/docker-containment.py`, the live containment probe. That probe is required
before first use and again after every Codex or Claude Code upgrade:
`python3 -I checks/docker-containment.py check --sandbox 'codex sandbox'`, plus its
`serve` and `probe` modes from a real Claude lane and a real Codex lane (exact commands
in the doc).

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
the user, and uses the managed `lane` profile, which extends `:workspace` and enables
network access through a host allowlist proxy. The `review` profile extends
`:read-only` and has no network access. The retired
`untrusted` policy is not valid in the pinned Codex version.
Claude Code uses the founder-approved `acceptEdits` mode, allows routine
Git/gh commands, and uses the native Linux sandbox with unsandboxed retries disabled and startup failing if the sandbox is
unavailable. The base image pins the sandbox's `bubblewrap` and `socat`
dependencies. Claude's optional seccomp add-on is not installed; Windows interop
and drive automount remain disabled by the distro baseline.

The policy hook blocks Windows/host commands, file-tool writes outside the
current repository, shell writes outside that repository, and commits whose
staged diff cannot pass the pinned Gitleaks scan. The one writable place outside
a repository is the review scratch `/tmp/claude-<uid>/scratch/`, and only while
`/tmp/claude-<uid>` (Claude Code's `$TMPDIR`) is a real directory owned by the
agent and closed to others; symlinks out of it are refused. The Codex hooks are
managed (below), so Codex trusts them by policy and `/hooks` cannot disable them;
no hook-trust review or bypass is involved. Hooks are additional guardrails; the
Codex workspace sandbox and Claude native sandbox enforce subprocess boundaries.

#### Managed guardrails

Literal shell scripts and all directly invoked recipe bodies are judged by the
command policy, fail-closed: syntax or execution inputs it cannot model are denied.
`task check` is the single explicit trusted gate entry for lanes. Direct `just`,
`make`, `npm run` and `pnpm run` calls do not inherit gate trust.
Recipe comparisons use the merge base with `origin/HEAD`, falling back to local
`main`, `master`, then the worktree's source branch recorded in Git's reflogs.
Without a usable base, recipe execution is denied with an explanatory message.
Unchanged recipes use the same strict judge as changed recipes, including nested
shell files. Simple literal Just and package scripts are supported; Make syntax,
runner configuration, non-shell programs and dynamic script paths in judged bodies
are denied. Recipe cwd changes are denied because lines may run in separate shells.

Run scripts and recipes in separate calls after commands that may change files.
A script or recipe run itself counts as changing for later scripts in the same
chain: `bash a.sh && bash b.sh` and `just check && just lint` are refused because
the first run could rewrite the second definition. Redirects to `/dev/null` do
not count as file changes. Shell assignments affecting resolution or startup
(including `CDPATH`, `PATH`, `BASH_ENV` and `ENV`) are refused. Builtin assignment
forms (`printf -v`, `read`, `mapfile`, `readarray`, `getopts`, declarations), shell
resolution changes (`hash`, `enable`, aliases, `shopt`, `set -o`) and wrappers
around them are denied. Plain prints and read-only commands remain allowed.
Quoted print literals may contain brackets, braces, stars, question marks and
tildes; these characters do not make a print an assignment or shell expansion.
Only literal `cd <path>` is modelled in typed commands and ordinary shell files;
cwd options, variable operands and directory-stack operations are denied.

Package scripts and install lifecycle scripts use the same checks for npm, pnpm,
Yarn and Bun. Run targets must be explicitly defined in `package.json`; binary
and file fallbacks are refused. Yarn (including `yarnpkg`) and Bun support explicit
`run` and `install` forms. Fetch-and-run forms (`exec`, `dlx`, `x`, `create`,
`bunx`, and `pnpx`) are refused. `npx` requires explicit `--no-install` or `--no`,
an existing executable in local `node_modules/.bin`, and a script the policy can
judge. Node-shebang binaries are denied.

This inspection is not a process sandbox: inherited environment, mutable installed
commands, dependency lifecycle code, executable tool configuration and concurrent
file replacement remain execution boundaries for the OS sandbox. The trusted
`task check` gate runs arbitrary project verification through its existing controls.
The lane check is advisory: its worktree recipes and scripts are mutable. The PM's
merge gate provides trusted verification. The task executable, rather than the
command hook, enforces PM/lane authority for other task routes; `task status`
remains available for inspection.

The guardrails are root-owned, so an agent in any permission mode (including Claude
`--dangerously-skip-permissions` and Codex `--dangerously-bypass-approvals-and-sandbox`)
cannot edit or switch them off. Nothing in them limits bypass or auto mode, the approval
policy, the sandbox choice or the agent's own extra hooks; those stay preferences in
`~/.claude/settings.json`, `~/.codex/config.toml` and `~/.codex/rules/default.rules`.
`install.sh` publishes the revision to `/opt/machine-bootstrap/current`, then runs, from there:

- `system/claude-managed.sh`: `/etc/claude-code/managed-settings.d/10-agent-guardrails.json`
  (from `system/claude-managed-guardrails.json`) holds the 88 `permissions.deny` rules and the
  `PreToolUse`/`UserPromptSubmit` hooks. Managed settings rank above user, project, local and
  `--settings` values, deny rules from any level win over allow rules, managed hooks run
  alongside the user's own hooks, and only a managed `disableAllHooks` can turn them off
  ([settings](https://code.claude.com/docs/en/settings#settings-precedence),
  [managed settings](https://code.claude.com/docs/en/managed-settings),
  [hooks](https://code.claude.com/docs/en/hooks)). The hooks use exec form (`command` plus
  `args`, no shell): `/usr/bin/env -u LD_PRELOAD -u LD_LIBRARY_PATH PATH=/usr/local/bin:/usr/bin:/bin /usr/bin/python3 -I
  /usr/local/lib/agent-policy/<script>.py`.
- `system/codex-managed.sh`: `/etc/codex/requirements.toml` (from
  `system/codex-requirements.toml`) pins `[features].hooks = true`, declares the same hooks
  under `[hooks]` (`managed_dir` is `/usr/local/lib/agent-policy`) and holds the forbidden/prompt
  command rules under `[rules] prefix_rules`. Requirements cannot be overridden by user config;
  managed hooks are trusted by policy and run alongside user hooks; requirements rules merge
  with `~/.codex/rules/*.rules` and the most restrictive decision wins
  ([managed configuration](https://learn.chatgpt.com/docs/enterprise/managed-configuration),
  [hooks](https://learn.chatgpt.com/docs/hooks)).
- Codex read-deny (T-50): `--sandbox` limits writes, not reads, and Codex refuses to combine
  `sandbox_mode` with `default_permissions`. So `task` launches Codex with
  `-c default_permissions="lane"` (build lanes) or `"review"` (reviewers, red team) and no
  `--sandbox`. `system/codex-requirements.toml` defines both profiles (`lane` extends
  `:workspace`, `review` extends `:read-only`, so each keeps its write scope), allows only those
  two in `allowed_permission_profiles` and defaults to `lane`. Both deny reads of everything
  in the Claude `Read(~/…)` deny list (`~/.ssh`, `~/.aws`, `~/.azure`, `~/.kube`, `~/.gnupg`,
  `~/.config/gh`, `~/.config/gcloud`, `~/.docker/config.json`, `~/.netrc`, `~/.npmrc`,
  `~/.claude.json`, `~/.codex/auth.json`) plus `~/.claude/.credentials.json`, `~/.gemini`,
  `~/project-data` and the private repos (`typo3-dkm`, `typo3-dkm-plugin`, `customer-harness`,
  `photo-organizer` under `~/dev_workspace`, and their `cortex-kb-*` repos). The deny covers
  commands the sandbox runs; the `codex` process reads `~/.codex/auth.json` itself. A private
  project's own lane therefore cannot read its repo until that project gets its own profile.
  `checks/agent-drift.py` reports `codex-deny-list-missing` when the installed requirements
  lack these profiles. Git writes (T-53): `:workspace` leaves a linked worktree's metadata
  read-only, so `task lane` (Codex builder only) adds exactly four `--add-dir` grants, resolved
  with `git rev-parse --path-format=absolute`: the worktree's own git dir and the shared
  `<common>/objects`, `<common>/refs` and `<common>/logs`. A lane can therefore commit and merge on
  its task branch. `hooks`, `config` and `info` stay read-only because a writable hook or config
  runs as code outside the sandbox at the next Git command, and no other worktree's dir is
  granted. The grant is always `<common>/worktrees/<name>`, never `<common>` itself, and `task
  lane` refuses when `extensions.worktreeConfig` is on. The lane-writable Git metadata (the
  worktree's `.git`, `commondir`, `gitdir`, `config.worktree`) is checked against `task`'s records
  after every lane, and before check, review and merge; a mismatch marks the lane `tampered`, stops
  the batch runner and is never repaired automatically. With the shared `refs`
  writable, a lane can also move other local branches (`git branch -f`, `-D`, `-m`), as it could
  before T-52. The policy hook does not refuse those yet (`git update-ref` is refused; `branch`
  is not checked), so this is an accepted risk until cortex-core T-42 #5 adds that refusal.
  Codex builder lanes get private uv, pip, pre-commit and npm caches under their worktree's
  .work/tmp; Claude lanes keep their existing environment. The shared ~/.cache/uv,
  ~/.cache/pip, ~/.cache/pre-commit and ~/.npm stay read-only to lanes because the PM's
  unsandboxed tools and trusted checks reuse them.
  Reviews, red teams and realigns use `review` and get no Git grants. PM proof after install, one `codex sandbox -c 'default_permissions="review"' --
  test -r <path>` per denied path (must fail; never print contents) and the controls
  `/etc/hostname` and `~/.bashrc` (must succeed); repeat with `"lane"`; then run `task smoke`
  (T-56); the change counts as installed only when it passes. Also confirm `codex exec` with
  `-c default_permissions` starts under the requirements (no "overrides cannot both be set" error)
  and that `~/.codex/config.toml` `sandbox_mode` does not conflict.

Both installers refuse to run unless the policy scripts, `/usr/bin/env`, `/usr/bin/python3`,
`/usr/bin/git` and the root-owned `/usr/local/bin/gitleaks` (installed by `install.sh`), and
every directory above them, are owned by root and writable by nobody else. The scripts use only
the standard library, `-I` keeps `PYTHONPATH`, user site-packages and the script directory off
`sys.path`, and the fixed `PATH` makes the hook's `git` and `gitleaks` the root-owned ones.
`install.sh` atomically installs the policy scripts into `/usr/local/lib/agent-policy/`,
independent of the `current` symlink. chezmoi removes the old user-level copies (`~/.codex/hooks.json`,
`~/.local/share/bootstrap/agent-policy/*.py`), so there is one source of truth.
`~/.codex/hooks.json` is managed by bootstrap; agents' own Codex hooks belong in the project's `.codex` folder.
`ci/managed-policy-test.py` (lint) checks the content; `ci/claude-managed-install-test.py` and
`ci/git-gh-approval-test.py` (distro job, as agent) check ownership, immutability and the
installed Codex rule engine.

Rollback: before installing a revision from before this change, run
`sudo rm /etc/claude-code/managed-settings.d/10-agent-guardrails.json /etc/codex/requirements.toml`
and `sudo rm -rf /usr/local/lib/agent-policy` first.

Limits: Codex runs every hook command through the user's shell (`$SHELL -c`), so an agent that
controls the environment Codex starts with (for example `BASH_ENV` exported from `~/.bashrc`)
can stop the Codex hook before it runs; Codex's `[rules]` are evaluated inside Codex and are not
affected, and Claude's exec-form hooks start no shell. The `claude` and `codex` installations
themselves live in the agent's home and are agent-writable. Server-managed Claude settings, if
an organization login ever delivers them, take precedence over the file-based ones.

Claude Code mods (v2.1.287 and later) run inside Claude Code and can approve
tool calls that a non-managed `PreToolUse` hook blocked. `install.sh` therefore
runs `system/claude-managed.sh` as admin, which installs the root-owned drop-in
`/etc/claude-code/managed-settings.d/50-managed-mods-only.json`. It sets
`allowManagedModsOnly` on the built-in guard (`cc-plugin-sec-default@builtin`),
so mods the agent installs, loads with `--plugin-dir`, or has Claude write do not
load; built-in mods and the agent's own settings hooks are unaffected. The
directory and file are `root:root` and not writable by the agent, and CI checks
that. The option is read from managed settings only, so it is deliberately not
in the agent's `~/.claude/settings.json`. `disableSideloadFlags` is not set
because it would also reject `--agents` and `--mcp-config`. The policy hook
itself is a managed hook (above), which a mod cannot override either. The pinned
Claude Code release is 2.1.287 or later, so
the distro job's last step (`ci/claude-mods-load-test.sh`) runs a fixture mod both
with `--plugin-dir` and installed into the agent's plugin scope: neither answers
`/modping` under the managed drop-in, and both do once the drop-in is moved aside
in the disposable container, which is the control. It also requires the
`allowManagedModsOnly` refusal in the debug log and fails if the pin is older
than 2.1.287.

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

## Projects, registries and repository boundaries

**Starting a project.** `new-project create <name>` (in `~/.local/bin`) creates the code repository `~/dev_workspace/<name>` and the knowledge repository `~/cortex/cortex-kb-<name>`, each on `main` with a first commit. The knowledge repository gets `INTENT.md`, `STATUS.md`, `README.md` and `AGENTS.md` at the root, and a Backlog.md `backlog/` from `backlog init` (integration mode none, task prefix `T`, `auto_commit`, `remote_operations` and `check_active_branches` all false) for tasks and decisions. Both repositories get the standard pre-commit gate (see [Local quality gate](#local-quality-gate-pre-commit)) before the first commit, so that commit already passes it. The command ends by printing "run chezmoi apply to add it to the qmd index". Add `--local-only` for projects without GitHub. Otherwise the PM creates the two private GitHub repositories with the standard ruleset and then runs `new-project publish <name>`. The founder or the PM runs it from a normal shell as the agent user; agent sessions cannot, because the policy hook blocks `git init` there on purpose.

**Lane downloads.** Both sandboxes enable network access to `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`, `github.com`, `objects.githubusercontent.com`, `codeload.github.com`, `raw.githubusercontent.com`, `release-assets.githubusercontent.com` and `openrouter.ai`. Codex's managed `[permissions.lane.network]` enables access, disables SOCKS and allows local binding for tests; its `.domains` table allows exactly those hosts. User config enables the enforcing proxy with `[features] network_proxy = true`. The `review` profile has no network section. Both vendors can write the uv, pre-commit, pip and npm caches. Installs and lint downloads run inside the sandbox. The Codex chezmoi modifier enforces managed keys while retaining local `[projects.*]` trust entries.

**One repository per session.** Agents commit only in the repository the session was opened in. Build lanes report in chat; the PM records `STATUS.md`, backlog tasks and decisions in the knowledge repository.

### Task batches and lanes

`task start` and `task lane` accept `--tier routine|hard|design`. Tasks may
declare `Tier: hard` alongside their `Verify:` lines; the default is routine.
The flag takes precedence and the tier is pinned in task state and shown by
`task status`. Runner lines require a model or a tier. For example:

```text
1. prepare: builder codex, tier routine
2. implement: builder codex, tier hard, after: prepare
```

Without a model, the batch passes `--tier` to `task lane` for routing.
An explicit runner model takes precedence over tier routing.
Tier-only tasks keep their tier on every fix round without a model pin; only
the final vendor swap applies. Explicit-model tasks keep their model escalation rules.

`task boost --claude-until <ISO datetime> --codex-until <ISO datetime>` sets
temporary vendor allowance; `task boost off [--claude] [--codex]` clears it.
Without boost, routine uses the usual builder default (Codex gpt-6-luna,
Claude sonnet); hard and design use Codex gpt-6.1-sol or Claude opus.
While that vendor is boosted, hard uses Codex gpt-6.1-sol or
Claude fable; design uses Codex gpt-6-astra or Claude opus. Routine keeps its
default. An explicit `--model` wins, except that top models run only for their
specialty: `gpt-6-astra` needs `Tier: design` and `fable` needs `Tier: hard`.
`task lane` refuses them on any other tier, and the batch parks that task with the
reason, whether the model came from `--model`, a runner line or a fix round. The tier
is the one recorded for the task; a `--tier` flag or runner `tier` that differs from it
is refused on `task lane` and `task start`, so it never changes the recorded tier or
makes the model eligible, unless a valid `--override` is given (the change is ledgered).
Only Jacopo can switch a model: `task lane --override "Jacopo YYYY-MM-DD: <request>"`
(runner line field `, override: Jacopo YYYY-MM-DD: <request>`) permits it; any other
text is refused. Each use is appended to
`~/.local/state/task/usage/overrides.jsonl` and shown in `task status` and
`task usage --weekly-line`. `--no-pace` does not override this, and reviews never use
these models, override or not.
`--no-boost` disables boosted tier
routing, while Codex hard remains gpt-6.1-sol.
The launch notice explains the decision. For high-risk tasks in boost, hard-tier reviews and rounds explicitly
marked with `task review <repo> <id> --run --final` use Claude opus or Codex gpt-6.1-sol.
Other reviews keep their usual cheap models; no round is inferred to be final.
Usage limits retry once on the other reviewer vendor and record its actual
identity. If that fallback uses the builder's vendor, the batch stops for an
independent review; the merge gate still requires the other vendor.

The shared routing constant is in `home/dot_local/bin/task_routing.py`.
Optional `~/.config/task/routing.json` overrides defaults and hard/design models,
for example `{"models":{"claude":{"hard":"fable"}},"extra_lanes":2}`.
Model keys are `default`, `hard`, `design` under `codex` or `claude`.
`extra_lanes` is a nonnegative integer, default 2. The batch runner normally
runs one task at a time; an active vendor boost permits 1 + extra_lanes tasks
for that vendor. Total concurrency is capped by the largest current vendor
limit and the slots in `~/.config/task/gate.toml` (default 3).
Dependencies wait for their predecessor to merge; parked predecessors skip
their dependants. Expiry reduces new dispatches without interrupting live work.
`task status` shows the effective vendor limits and gate slots.

#### Risk and checks

Risk (`low`, `normal`, `high`) is separate from the builder-routing `tier` above.
`task start <repo> <id> --builder codex --risk high --risk-reason "rails change"`
sets it; omitted risk defaults to normal, including older records. High needs a
one-line reason. Change it later with `task risk <repo> <id> low` or
`task risk <repo> <id> high --reason "safety boundary"`; changes enter the override ledger.
Refresh preserves risk. `task status` shows both risk and builder tier.

| Risk | Required checks | Default build/fix rounds | Total extra rounds per task |
| --- | --- | --- | --- |
| low | Trusted check; review commands refused; merge needs no review or report | 2 | 1 |
| normal | Trusted check and one cheap other-vendor review | 3 | 1 |
| high | Trusted check, other-vendor review and red-team at the reviewed commit | 3 | 1 |

Low risk also refuses `review --record`; its merge skips report and criteria checks,
and close ticks the pinned criteria without review evidence. The batch runner skips
low-tier review and runs a red-team after a high-tier review, before merge.

Normal reviews stay cheap even in boost or with `review --run --final`. Manual
Opus, Fable and other deeper reviews are refused outside high risk. High keeps the
existing boost routing: hard builder tier or explicit `--final` selects the deeper
reviewer while that vendor is boosted. Default cheap reviewers are Haiku for
Codex-built work and gpt-6-luna for Claude-built work.

At check, review and merge, changed paths in `main...HEAD` matching `review.high_paths`
force high risk; attempting to lower it names the matching path and refuses.
Patterns apply to the whole repository-relative path, with `*` matching any part;
the `"*"` repository entry applies everywhere. Defaults cover every repo's `policy/**`,
skill rules and references, agent instructions, `INTENT.md` and decision records,
and bootstrap's agent-policy hook/rule tables, managed settings, Codex sandbox/rules,
hold script/markers, boundary checks, Git credential settings and gitleaks config.
The task executable itself is excluded. The complete paths are in the example policy.

`task redteam <repo> <id> --brief FILE` runs the other vendor at high effort,
using Sonnet for a Codex builder and gpt-6.1-sol for a Claude builder by default.
It prepends `~/cortex/cortex-core/skills/company/references/red-team-brief.md` when
present, records that fact, vendor, model and output for the commit, and appears in status.
It refuses low and normal risk. High-risk merge requires that output at the reviewed
head, or `task merge ... --no-redteam "reason"` (ledgered after a successful merge
that passes the remaining gate); ordinary `--override`
does not waive this rule. High-tier close requires current review and red-team evidence.

`task lane ... --extra-round "small contained fix"` grants one extra round when the
limit is reached; it refuses the flag below the limit. Total grants are capped at
the policy's `extra_rounds` per task. Round counts persist across `start --refresh`,
as do the ledgered extra grants.
`task lane ... --effort low|medium|high` forwards effort to either vendor and records
it; default is high.

The company usage-pacing TOML also accepts `[review]`: `high_paths` maps repository
names (or `"*"`) to pattern lists. `[review.low]` accepts positive-integer `max_rounds`
and nonnegative-integer `extra_rounds`; `[review.normal]` and `[review.high]` also
accept `codex` and `claude` reviewer model names, keyed by builder vendor.
`[review.high]` additionally accepts `red_team_model` for Codex (default `gpt-6.1-sol`)
and `red_team_model_claude` for Claude (default `sonnet`).
Missing keys use built-in defaults; per-repository high-path lists replace their
defaults. An unknown or malformed `[review]` table (for example `[review.low]`) keeps its
defaults with one warning; the other review tables are kept. Risk enforcement fails closed on an unreadable or unparseable policy
file, while usage pacing reports its existing defaults fallback.

#### Usage pacing

`task usage` shows, per vendor, weekly and 5-hour use, the weekly reset (UTC and Europe/Copenhagen),
the reading's age, the share of the week elapsed, the pace verdict and the policy applied. It spends
no tokens. Readings are the vendors' own numbers: the newest Codex `rate_limits` in
`~/.codex/sessions/**/rollout-*.jsonl` and the newest Claude `rate_limit_event`. Claude lanes and
`task review --run` run `claude -p --output-format stream-json --verbose`: the events go to
`.work/lane.jsonl` (reviews: `review-response.jsonl` in the task state) and the newest reading per
window to `~/.local/state/task/usage/claude.json`; `lane.log`, `final.md` and the review reply keep
the plain text. A reading with no `rate_limit_event` yet, from before the last weekly reset, or older
than 7 days is `unknown` with the reason, and `task` then behaves as before and prints a warning.
The interactive status-line `rate_limits` is not read. ccusage is for per-project attribution only.

Verdicts, with weekly used % against the share of the week elapsed: `exhausted-automated-share`
(used ≥ 100 − reserve), `under-used-near-reset` (used below `under_use_percent` with at most
`under_use_hours` left), `ahead` (more than `ahead_margin` points above the elapsed share), `under`
(more than that below it) and `on`. In the final `under_use_hours` before a vendor's weekly reset the
weekly reserve is released (limit 100 %; every `task usage` verdict, exhausted included, says "reserve
released until <reset>"); the 5-hour bands keep their reserve. `task lane`, `task review --run` and each batch start (`task-batch`,
so `task runner-start` too) apply the verdict:

| Verdict | Effect |
| --- | --- |
| `on`, `under`, unknown | unchanged (unknown prints a warning) |
| `ahead` | routine lanes use the vendor's cheaper model (Claude haiku, Codex gpt-6-luna) even when `--model` names another one (only `--no-pace`, logged, keeps it); no new batch starts; only the first `priority` repository with an unmerged task gets new lanes (others exit 4, held back) |
| `exhausted-automated-share` | no new lanes or reviews on that vendor (running ones finish); a routine lane moves to the other vendor unless that is also ahead or exhausted; otherwise it is held. With a spare reset held, a line asks Jacopo to use it |
| `under-used-near-reset` | spend before the reset: when the 5-hour reading permits boost, `task boost` for that vendor is switched on until its weekly reset (stronger models by tier and extra capacity for already queued work, logged). High-risk hard-tier reviews and explicit `--final` rounds use the stronger reviewer (Claude opus, Codex gpt-6.1-sol). Allowance is never spent on new build tasks; the `research_backlog` names pre-approved research the PM may use next |

Manual and automatic boosts pause per vendor at or above `boost_five_hour_max` (default 50 %).
A missing 5-hour reading, or one older than 45 minutes while the vendor's lanes ran since it,
also pauses boost for a vendor with a 5-hour window. A vendor without one (Codex today) keeps
its weekly rules. Normal routing and capacity apply to paused boosts, and no new automatic
boost starts. `task usage` and `task status` explain a saved boost's pause. Its expiry stays saved,
so a fresh reading below the threshold resumes it automatically until that expiry. `--no-pace`
does not bypass this boost limit.

The 5-hour window is a second pace input per vendor with the same reserves (it ignores spare resets,
which only refill the weekly window). The reserve shrinks with the time left in the window: reserve ×
hours left ÷ 5 (30 with 1.5 h left is 9: exhausted at 91 %, warning at 76 %); a missing or stale reading
keeps the full reserve, and `task usage` shows the effective values. `5h-exhausted` (used ≥ 100 − reserve): no new lane or review on
that vendor until the 5-hour reset, which `task usage` and the hold message show; running ones finish,
and a routine lane moves to the other vendor when that has room on both windows. `5h-warning`
(15 points lower): at most one lane or review of that vendor runs machine-wide (`task` records each
started process under `~/.local/state/task/usage/running/`); a new one is held until it ends. A
reading older than 45 minutes while that vendor's lanes ran since counts as `5h-warning` and prints a
warning. For a vendor that has a 5-hour window (Claude; Codex once one shows in its readings), a
missing 5-hour reading (none yet, or its window has reset) is `5h-warning` too. A vendor with no
5-hour window in its readings of the last 7 days (Codex today) has only the weekly rules, and
`task usage` says so. A runner's capacity, boosted or not, is
capped by this (0 when exhausted, 1 in the warning band), so boost adds parallel lanes only below
the warning band. `task usage --weekly-line` summarizes recorded runs over the last seven days.

While Jacopo holds a spare reset for a vendor (`spare_resets` > 0) there is no reserve: the window is
spent at the normal pace, `ahead` never holds lanes back, the share ends at 100 % and `task usage`
says "spare reset held; reset at 100 %", after the release wording in the final `under_use_hours`.

A held lane exits 4 (`task lane`, `task review --run`); the batch runner waits a minute and asks again.
`--no-pace` on `task lane`, `task review`, `task runner-start` and `task-batch` skips the check for that
one command. Overrides, boosts and held-back lanes are logged to `~/.local/state/task/usage/pace.log`.
`~/.local/state/task/usage/weeks.json` keeps, per vendor and weekly window, the last used %, the day
the automated share was first seen used up and the lanes held back.

`~/.local/state/task/runs.jsonl` records lane rounds and reviews, with vendor, model, effort, tier,
round, start/end times, plan ID from `TASK_PLAN`, before/after vendor readings and window deltas.
Unavailable readings and ccusage tokens are null; a window reset makes its delta null.
Finishes are deduplicated by run ID. Merge and runner park/stop events refer to those IDs.
Pending records in `pending-runs/` survive crashes; status shows them as unfinished without writing
an invented end. `task status` summarizes the last day per vendor; `task usage --weekly-line`
summarizes seven days (unique runs, weekly percentage-point deltas and outcome counts).
The former weekly pacing-counter report is replaced by this run summary; pacing rules are unchanged.
For manual reviews, generate the review prompt before launching to capture a start reading; recording
a review without that prompt leaves its start and before reading null.

A PM logs its run with `task note <repo> - pm-run start`, followed by
`task note <repo> - pm-run end [finished|failed|interrupted] [reason]`. Set `TASK_VENDOR`, `TASK_MODEL`,
`TASK_EFFORT`, `TASK_TIER` and `TASK_PLAN` at start to record its identity. Repeating start while pending
or repeating end is safe. Lanes default to high effort, configurable with `--effort`; reviews use high.

The policy is TOML at the path in `~/.config/task/usage-policy-path`, default
`~/cortex/cortex-kb-company/policy/usage-pacing.toml`; `task` only reads it and the company PM writes it.
A missing file or key means the built-in defaults. An unknown or invalid key prints one warning line
naming it (`pace warning: <file>: unknown key <key>; ignored, other keys kept`); that key keeps its
default and every other key is kept. An unreadable or unparseable file is reported and all defaults apply.
[`docs/usage-pacing.example.toml`](docs/usage-pacing.example.toml) shows every key at its default:
`[reserve]` (claude 25, codex 10), `[spare_resets]` (claude 0, codex 1), `priority`, `ahead_margin` (10),
`under_use_percent` (60), `under_use_hours` (36), `boost_five_hour_max` (50, percent 0–100) and `research_backlog`.

Two company decisions shape vendor spend. **Decision-23 (Codex daily ramp):** with
`codex_ramp = { per_day = 20, ceiling = 80 }` (no key, no ramp) a new Codex lane, `task review --run`
or `task redteam` launches only while Codex weekly use is at or below `min(ceiling, per_day × started
day)`. Day 1 starts at the last weekly reset (`resets_at` − 7 days), so an early reset restarts at day 1.
Running work is never stopped. A refusal exits 4 and names the used %, the allowance and the day; a
routine lane does not move to Codex past its ramp. `task usage` shows the day, the allowance and
`within ramp` or `past ramp`. Hold-switch, always-on service, security and money work may launch above
the allowance and the ceiling with `--ramp-exempt hold|service|security|money` on `task lane`,
`task review --run` and `task redteam`; each use is written to the overrides ledger (kind `ramp_exempt`).
High risk alone is not an exemption. Claude has no ramp. **Decision-22 (Claude 3-point brake)** stays
outside `task` for now; the PM applies it by hand.

After the vendor readings `task usage` prints a `fallback:` block with one line per vendor that is past
its Codex ramp, braked (automated share used up, or ahead of pace), near or out of its 5-hour window
(the warning band, including a missing or stale reading) or blocked by an open foundation defect,
for example `codex past ramp (day 1, 20 %): builds -> Claude lane; reviews of Claude work -> wait`.
Builds fall back to the other vendor's lane unless that vendor is also listed (then `wait`); reviews of
the other vendor's work wait, or run one at a time in the 5-hour warning band. The PM records a
foundation defect by hand as `blocked = { codex = "<reason>" }`. Nothing is printed when every vendor
is normal.

The app-side PM plans the batch, talks to Jacopo and gets his go. It may then
launch one headless Codex batch runner with `--model gpt-6.1-sol` and
`--dangerously-bypass-approvals-and-sandbox`; the managed policy hook stays active.
The runner handles only that approved batch: dispatch, trusted checks, cheap
other-vendor reviews, recording, merge and close on green. A FAIL gets one fix
round; a second FAIL or a decision needing Jacopo stops the batch.

Trust comes from sandbox capability: `task` probes write access to
`~/.local/state/task`, without using vendor environment markers. The PM shell
and unsandboxed runner can write it and produce trusted evidence. Sandboxed
Codex and Claude Code builders cannot: their checks remain advisory, and commands
that mutate PM state refuse with “run this from the PM shell or a batch runner”.
Always launch builders via `task lane`. Sessions that cannot write
`~/.local/state/task` are advisory lanes.
The hook permits PM task command routes; its other rules remain in force.
`task lane` and plain `task review` are PM/runner-only too: lane dispatch starts
a builder, and review preparation writes private routing and prompt records.

Use `task review <repo> <id> --run [--request <file>]` from the PM shell or an
approved batch runner to prepare the prompt, run the routed reviewer, save its
reply and record the verdict in one command. This keeps private task-state file
access inside `task`; the runner does not need shell redirections into that state.
Codex-built work routes to Claude Haiku with high effort and read-only tools;
Claude-built work routes to GPT-6 luna with high reasoning effort and the read-only
sandbox. The command prints the full reviewer reply under a clear header, then
ends with the recorded verdict. Reviewer identity comes from stored routing,
just as with `--record`.
`TASK_REVIEW_TIMEOUT` sets the timeout in seconds (default 900). A timeout, empty
reply or reviewer command failure exits non-zero without recording a review.
Plain `task review <repo> <id>` still prints the prompt and headless command for
manual use; `--record <file>` still records a supplied reply. `--run` and `--record`
are mutually exclusive, and all review modes refuse in a sandboxed session.

Reports may accumulate rounds with repeated `## Criteria` headings. Merge and review
use only the final block, which must contain every pinned criterion marked `met`
with evidence. A missing or incomplete final block blocks; earlier `not met` rows
are superseded and cannot supply missing final-round evidence.

`task start --seed <name>` copies the main checkout's `.work/<name>` into the
task worktree, including an existing worktree. Resumed runner lines naming a seed
apply it through the same start path. Identical targets are left alone; a differing
file or directory stops with a conflict message and preserves lane work.

Before reporting, builders run advisory `task check` from the worktree on the final
commit and paste its final PASS/FAIL line, including the log path, under `## Check`
in `.work/report.md`. `review --run` requires a passing check for that head and
pinned task revision; `--no-check "reason"` records an exception. Both the review
gate and prompt use the trusted check for that head and revision when present:
it is authoritative over any advisory check, whatever their order. Without a
matching trusted check, the newest advisory check counts. Review prompts also
include the previous Blocking list and incremental diff on re-review. Blocking findings
must name a requested criterion or concrete defect/regression and a file:line;
other observations belong in Notes. New non-defect findings on unchanged code
are Notes on re-review.

Task briefs may include `## Read first` (files/docs to read before editing) and
`## Test against` (real data, fixtures or examples), or inline `Read first:` and
`Test against:` fields with indented or bullet continuations. Dispatch prints
both fields; omitted fields show `(PM: none given)`.

Task state retains numbered `review-<n>.md` replies with verdict, head and time
and JSON metadata, plus append-only `checks.jsonl` (advisory history stays in
the worktree's archived `.work`). `task status` reports first-review pass rate
for tasks first reviewed in the last 14 days, and mean total reviews for tasks
merged in that period with available review history. Legacy records are used
when available; missing history is excluded. Archive copies are deduplicated.

Optional `.task.toml` gate variants run sequentially under the same slot and port
reservation, each with a fresh `TMPDIR`. Every variant must pass. Each variant's
`env` overrides `[env]`, using the same reserved-name and credential checks and
`{repo}` / `{worktree}` expansion:

```toml
[[gate_variant]]
name = "real" # Empty overrides use the base [env].

[[gate_variant]]
name = "fake"
[gate_variant.env]
PH_PRODUCT_DIR = "{worktree}/.work/fake"
```

When variants are declared, these are the runs; add an empty variant to include
the base configuration. With no variants, the gate runs once as before.
Checks and merge gates run the matrix. Advisory lane checks
also accept `task check --variant fake`; trusted checks always run every variant.
The combined check log labels each run, and JSON evidence and history include
per-variant results. `task status` lists variant names.

Advisory lane checks write `.work/check.log`. Trusted checks write `check.log`
in the task's state directory; merge gates write `gate-check.log` there. They leave the lane
log untouched. Check JSON and history record the log path and summary; status
and review prompts identify the corresponding trusted or advisory log.
Merge and published-merge repair preserve the trusted `check.log` in state
and archive the lane log separately as `advisory-check.log`. Batch fix rounds
read the log recorded by the latest check, including trusted PM checks.

### Hold switch

`task hold` shows the two holds. Jacopo uses `sudo agent-hold pull "reason"` and
`sudo agent-hold release`; `agent-hold` shows his marker at `/etc/agent-hold`.
Everyone else uses `task hold pull "reason"` with `TASK_HOLDER` set or `--holder <name>`.
`task hold release` also requires `TASK_HOLDER` or `--holder <name>` and removes only
`~/.local/state/task/hold`, never Jacopo's marker. Names provide attribution, not authentication;
the filesystem and sandbox provide the write boundary.
Existence holds work; the text is for people. Unreadable or nonregular markers also hold work.

The only mode is drain: a step already launched finishes; nothing new launches, including
retries and fallbacks. Every builder, reviewer, gate step, runner and workspace-tidy apply
starts in a `systemd-run --user` unit. Each unit uses `ExecCondition=` with the installed `task hold --check`:
it needs no holder and succeeds only when both markers are absent and inspectable.
Skipped units are recorded as `skipped: hold`, with their unit name. A missing user manager
fails closed; there is no process fallback. Gate-browser runs inside its existing gate unit,
and a batch started by a runner runs inline in that runner's unit.
`task merge` checks the hold once on entry in the PM's shell; its sync and publication
continue in that process. A hold pulled after entry does not interrupt them.
Status, usage, boost, lock display and hold inspection remain available.
Running `just check` by hand and the printed manual reviewer command are operator paths
outside the switch; the operator must respect the hold before launching them.
For a stop beyond drain, pull a hold first, then use the existing
`task cancel <repo> runner-<batch>` for runner units and
`task cancel <repo> <id>` for recorded lanes. Reconcile interrupted work through the
existing cancel path; interrupted work is never auto-merged. Inspect and reconcile
before the appropriate owner releases the hold. There is no automatic stop or release.

Always launch build lanes through `task lane`, including from a batch runner:

```sh
task start bootstrap example --builder codex --no-verify 'Direct approved task'
# Write the approved instructions to the task worktree's .work/prompt.md.
task lane bootstrap example --model gpt-6.1-sol
task status bootstrap
```

`task lane <repo> <id> [--model gpt-6-luna|gpt-6.1-sol] [--builder codex|claude]`
requires an existing task worktree, pinned task record and `.work/prompt.md`.
The builder defaults to the pinned builder; an explicit builder must match it.
Codex defaults to `gpt-6-luna` and always uses `codex exec --sandbox workspace-write`.
Claude uses `claude -p --model sonnet --effort high --permission-mode acceptEdits
--allowedTools "Bash Read Write Edit Glob Grep"`; its sandbox, configured in
`settings.json`, and the policy hook remain active. Builders use no bypass flag.

The launcher removes hook-blocked Git overrides (`GIT_PAGER`, `GIT_CONFIG*` and
the hook's `GIT_OVERRIDE_ENV` set) and gate-run ownership variables, preserving
other Git variables such as author/committer identity and `GIT_OPTIONAL_LOCKS`.
It runs from the task worktree, detaches and prints its pid. The final response is
`.work/final.md`, logs are `.work/lane.log`, and `task status` shows the lane as
running, finished with its exit code, or interrupted. A live lane cannot be
launched twice. Commit, check, report and other-vendor review still precede merge.

### Installing a bootstrap change

After every install of a guard, hook, sandbox profile, permission or `task`
change, the PM runs `task smoke` from a normal shell. The change counts as
installed only when it passes; the install record in STATUS names the run id and
the result.

```sh
task smoke                    # both builders (default); or --builder codex|claude
```

It creates `~/dev_workspace/task-smoke-<run>` (one commit on `main`, no remote)
and `~/cortex/cortex-kb-task-smoke-<run>`, then runs the normal path for each
builder: `task start`, an unrelated commit on `main`, `task lane` on the cheapest
model at low effort, the lane's commit, merge of `main` and report, `task check`,
`task review --run` with the cheapest reviewer of the other vendor, and
`task merge`. It prints `PASS`, or `FAIL <step>: <first error line>` for each
step, and stops that builder at its first failure. It writes
`~/.local/state/task/smoke/<run>.json`. A pass removes the scratch project; a
FAIL keeps it and prints the paths, and `task smoke --clean <run>` removes them.

A smoke costs two cheap lanes (Codex `gpt-6-luna`, Claude `haiku`) and two cheap
reviews (Claude `haiku` on the Codex build, Codex `gpt-6-luna` on the Claude
build).

### Cortex knowledge search (qmd)

[qmd](https://www.npmjs.com/package/@tobilu/qmd) (MIT) indexes the Markdown in
every `~/cortex/*` repository for local keyword and semantic search.

- **Pin:** `@tobilu/qmd` 2.8.3 in the npm lockfile (v2); `tools.sh` links
  `~/.local/bin/qmd`. The monthly pin updater leaves it alone, so bump it by hand.
- **Override (`@modelcontextprotocol/server` 2.3.0):** `package.json` has an npm
  `overrides` entry under `@tobilu/qmd` that moves qmd's MCP server dependency to
  2.3.0. qmd 2.8.3, the latest release, pins it exactly at 2.0.0, which carries
  GHSA-46jr-4wx8-v453, GHSA-jm63-cq47-j5jp and GHSA-rvq5-wwqv-78pq (fixed in 2.3.0).
  JSON has no comments, so the reason lives here. The override also moves
  `@modelcontextprotocol/core` to 2.3.0, and both packages are Apache-2.0 from 2.3.0
  (2.0.0 was MIT). Drop the override once a qmd release no longer pins `@modelcontextprotocol/server` at
  2.0.0 (check with `npm view @tobilu/qmd dependencies`); bump qmd, remove the entry,
  regenerate the lock and rerun `ci/osv-db-test.py`.
- **Config:** chezmoi renders `~/.config/qmd/index.yml` with one collection per
  non-hidden `~/cortex/*` directory (mask `**/*.md`). Each one-line context is
  `<repo>: <title>` taken from that repository's `INTENT.md` front matter at
  apply time, so no project detail lives in this public source. After adding a
  repository run `chezmoi apply`.
- **Project reference material:** a project may put Markdown its PM should read in
  `~/project-data/<project>/reference/`. Each such folder becomes the read-only
  collection `<project>-reference` (mask `**/*.md`; context from the first line of
  its `README.md`, else `Reference material for <project>`). Nothing outside
  `reference/` is indexed, and a folder containing `secrets.env`, any `*.key` file or
  a symlink is skipped with a warning at `chezmoi apply`. Never put secrets in
  `reference/`. PM seats get no Filesystem access to `~/project-data`; qmd is the
  read path. Run `chezmoi apply` after adding a reference folder.
- **Timer:** the user timer `qmd-index.timer` (every 15 minutes, `OnCalendar=*:0/15`)
  runs `~/.local/bin/qmd-refresh`: plain indexing, no agent runs. `users.sh` enables
  lingering for `agent` so it fires without a login session.
- **Freshness:** before `qmd update` and `qmd embed`, `qmd-refresh` fetches and
  fast-forwards (`--ff-only`) every `~/cortex/*` checkout that has an `origin`, is on
  `main` and is clean. It leaves every other checkout alone and logs why it skipped
  it (`journalctl --user -u qmd-index`). Git authenticates through the agent's
  `~/.gitconfig` credential helper (`gh auth git-credential`, so the agent's `gh`
  login), with prompts disabled: a failed fetch is logged and makes the unit fail,
  while indexing still runs.
- **Setup:** after `chezmoi init --apply`, `install.sh` starts agent's user manager
  (`system/user-systemd.sh`) and runs `~/.local/share/bootstrap/qmd-setup.sh` as agent
  with the user bus. It renders `index.yml`, runs `qmd pull`, `qmd update` and
  `qmd embed` (models land in `~/.cache/qmd/models`, outside the sandbox; agent
  sessions and the timer never download), enables and checks the timer, and registers
  the MCP server at user scope (`claude mcp add --scope user qmd -- ~/.local/bin/qmd mcp`,
  stdio, replacing any earlier entry). Each step is fatal on error and re-runnable.
- **GPU:** node-llama-cpp's prebuilt CUDA backend needs `libcudart.so.13` and
  `libcublas.so.13`, which the WSL driver does not provide (only `libcuda` comes from
  `/usr/lib/wsl/lib`). The tools script unpacks the hash-pinned NVIDIA wheels
  (`CUDA_RUNTIME_*`, `CUBLAS_*` in `pins.env`, cached in `~/.cache/bootstrap/cuda-wheels`)
  next to the backend library, which finds them through its `$ORIGIN` runpath: no root and
  no environment variables. Vulkan is not an option here: only the CPU `lvp` device is
  installed. If the qmd pin moves to a node-llama-cpp built for another CUDA major
  version, update those two pins. Check with
  `~/.local/share/bootstrap/npm/node_modules/.bin/node-llama-cpp inspect gpu`.
- **Tests:** `python3 ci/qmd-test.py [chezmoi]` checks the pin, the rendered config,
  the units, the install wiring and `qmd-refresh` against real temporary git
  repositories; `ci/lint.sh` runs it with the pinned chezmoi.

### AgentDev shell connector (Desktop Commander)

PM seats in the Claude app on Windows run AgentDev commands through
[Desktop Commander](https://github.com/wonderwhy-er/DesktopCommanderMCP) (MIT) running inside
AgentDev as the `agent` user: one persistent stdio MCP server per Claude app session, started
once through `wsl.exe -d AgentDev -u agent -- /home/agent/.local/bin/agentdev-shell-mcp`, the
same pattern as qmd. It replaces `wsl.exe -d AgentDev -u agent -- <command>` through Windows
Desktop Commander, which started a new `wsl.exe` launcher per command and left ghost launchers
behind when a call hung. Long-lived processes go through `start_process` and
`read_process_output`.

- **Never in agent sessions.** It bypasses the policy hook, so no Claude Code or Codex
  configuration may start it. `ci/shell-connector-test.py` fails if a tracked agent-session config
  references it, and `checks/agent-drift.py` reports `shell-connector-in-agent-session` for a live
  registration in `~/.claude.json`, `~/.claude/settings.json` or `~/.codex/config.toml`.
- **Pin:** `@wonderwhy-er/desktop-commander` 0.2.52 with its integrity in the npm lock, installed
  by `chezmoi apply` like the other npm tools; never `npx …@latest`. Pins are reviewed by hand:
  the monthly pin updater does not move it.
- **Launcher:** `~/.local/bin/agentdev-shell-mcp` merges into the config file the pinned server
  reads, `~/.claude-server-commander/config.json` (the 0.2.52 `dist/config.js`), on every start:
  `telemetryEnabled` `false`, `defaultShell` `/bin/bash`, and `allowedDirectories`
  `/home/agent/dev_workspace`, `/home/agent/cortex` and `/tmp/claude-1001/scratch`. Other keys are
  kept, so a PM can change one through `set_config_value`, but telemetry is off again at the next
  start. A first start builds on the server's own defaults with onboarding off. The launcher also
  sets `DESKTOP_COMMANDER_DISABLE_TELEMETRY=1`, points the server's Chrome lookup at the pinned
  Playwright Chromium (otherwise it downloads Chrome for Testing from Google at every start for
  its PDF tools), changes to `$HOME` and execs the server with `--no-onboarding`. It opens no
  network port and writes nothing to stdout before the server starts.
- **Boundary:** AgentDev itself: the agent user has no sudo, no Windows mounts, and interop is
  off (`checks/boundary.sh`). There is no wrapper shell and no extra policy layer.
  `allowedDirectories` limits only the file tools, and the server's `blockedCommands` list matches
  command names only, so neither is a boundary.
- **Vendor hosts:** found in the pinned package's `dist/`: `desktopcommander.app` (feature flags,
  welcome page), `telemetry.desktopcommander.app` and its fallback
  `dc-telemetry-proxy-83847352264.europe-west1.run.app` (the GA4 Measurement Protocol proxy,
  `/mp/collect`; the npm postinstall pings it too), and `mcp.desktopcommander.app` (the opt-in
  remote-device channel, which also fetches its Supabase settings from there). `system/base.sh`
  points them to `0.0.0.0` and `::` in a marked `/etc/hosts` block (`system/vendor-block-hosts.sh`)
  before chezmoi runs `npm ci`, and sets `[network] generateHosts=false` in `/etc/wsl.conf` so WSL
  does not rewrite `/etc/hosts` at boot.
- **Claude app entry** (Windows-side config, added by Jacopo after the install; nothing in this
  repository touches Windows files):

  ```json
  "agentdev-shell": {
    "command": "wsl.exe",
    "args": ["-d", "AgentDev", "-u", "agent", "--", "/home/agent/.local/bin/agentdev-shell-mcp"]
  }
  ```

- **Tests and probe:** `python3 ci/shell-connector-test.py` (run by `ci/lint.sh`) checks the pin,
  the hosts block, the `base.sh` wiring, the launcher's merge with a stub server, MCP over stdio
  through the launcher, and the agent-session configs. Where the package is installed it also runs
  the real server twice in a throwaway home. After an install, `python3 ci/shell-connector-probe.py`
  speaks MCP to the real `~/.local/bin/agentdev-shell-mcp`, lists its tools and runs `echo ok` as
  `agent`; the fresh-distro CI run (`ci/distro.sh`) does the same.

### Backlog.md MCP for the Claude app

The Company Control Room page calls [Backlog.md](https://github.com/MrLesk/Backlog.md)'s own MCP
server through the Claude app on Windows (as `host:<server>`). The Claude app reports no MCP
roots, so there is one server entry per knowledge repository, each pinned to its repository
with `backlog mcp start --cwd`. Started through
`wsl.exe -d AgentDev -u agent -- /home/agent/.local/bin/agentdev-backlog-mcp <repo>`, it runs
inside AgentDev as `agent` over stdio, like qmd and the shell connector.

- **Pin:** `backlog.md` 1.53.0 and `backlog.md-linux-x64` 1.53.0 in the npm lock, installed by
  `chezmoi apply` like the other npm tools. The launcher execs the platform binary directly (the
  npm `cli.js` wrapper does not pass SIGTERM on). 1.53.0 has no telemetry and no update check.
- **Never in agent sessions.** Agents use the `backlog` CLI. `ci/backlog-mcp-test.py` fails if a
  tracked agent-session config references the launcher, and `checks/agent-drift.py` reports
  `backlog-mcp-in-agent-session` for a live registration of the launcher or of `backlog mcp` in
  `~/.claude.json`, `~/.claude/settings.json` or `~/.codex/config.toml`.
- **Launcher allowlist:** `~/.local/bin/agentdev-backlog-mcp` serves only the repositories named
  in it: `cortex-kb-company`, `cortex-kb-product-harness`, `cortex-kb-household-signal-harness`,
  `cortex-kb-madplanner-v2`, `cortex-kb-consultancy-website`, `cortex-kb-home-energy-lab`, and the
  dormant `cortex-kb-hermes`, `cortex-kb-historical-ingestion`, `cortex-kb-linkedin-ops` and
  `cortex-kb-orw`. Never `cortex-kb-typo3-dkm`, `cortex-kb-customer-harness` or
  `cortex-kb-photo-organizer` (standing privacy rule). Any other name, a path or `..` exits 2
  before the server starts. Backlog.md resolves its project by walking up from `--cwd` to the
  first directory with a Backlog config, and a root `backlog.config.yml` can move the backlog
  folder, so the launcher also refuses a repository whose path contains a symlink (including
  `$HOME`), whose `backlog/` or `backlog/config.yml` is a symlink or missing, or that has a
  `backlog.config.yml`. On every launch it walks the entire `backlog/` tree and refuses any
  file or directory symlink that resolves outside the repository (also broken links and
  unreadable files). These are startup checks per connection; restart after filesystem or
  config changes. It starts the server in the repository with a clean environment
  (`HOME`, `PATH`, `LANG` only: `DEBUG` would print to stdout and break MCP), opens no network
  port and writes nothing to stdout before the exec.
- **Callbacks and git:** before starting, the launcher rejects project `onStatusChange` or
  `on_status_change` commands and task-frontmatter callbacks throughout `backlog/`, including
  drafts, completed and archived tasks. It rejects `auto_commit: true` and requires
  `remote_operations: false` explicitly, because remote operations default to on. It also requires
  `check_active_branches: false`: recent local branches are outside the startup scan, and 1.53.0
  can evaluate JavaScript task frontmatter while reading those branches. It reads
  `backlog/config.yml` using the scalar rules in the pinned
  [1.53.0 config parser](https://github.com/MrLesk/Backlog.md/blob/v1.53.0/src/file-system/operations.ts)
  and decodes task YAML with the already locked `yaml` package; non-YAML frontmatter engines
  and unreadable metadata fail closed. `onStatusChange` is the only command callback in this
  version's [configuration reference](https://github.com/MrLesk/Backlog.md/blob/v1.53.0/ADVANCED-CONFIG.md).
  The [config resolver](https://github.com/MrLesk/Backlog.md/blob/v1.53.0/src/utils/backlog-directory.ts)
  selects `backlog.config.yml`, or `backlog/config.yml` / `config.yaml`, or `.backlog/config.yml`
  / `config.yaml`; it has no user-level Backlog config layer. This launcher requires its own
  `backlog/config.yml` and rejects the root override, so no parent or alternate config is used.
- **What the page may call:** the page's capability manifest grants read-only access only:
  `task_view`, `task_list`, `task_search` and `document_view`. Write tools stay out of the page
  manifest unless Jacopo later approves a server-side guard. Backlog.md 1.53.0 has no tool filter,
  so that manifest, enforced by the Claude app, is the allowlist for the page. Its enforcement
  is proven only by the live probe
  through the page/platform, including refused calls outside the manifest; the stdio probe
  below checks server availability and cannot prove that platform boundary.
  It covers the page only: any other Claude app chat
  with these servers connected sees every Backlog.md tool, including `task_archive`,
  `milestone_archive` and `milestone_remove` (archive and delete), `document_create`, and the
  Definition of Done config upsert. Recovery is the git history in each knowledge repository;
  Backlog.md writes files without committing (`autoCommit` off), so an edit is recoverable only
  once the PM has committed the state before it.
- **Claude app entries** (Windows-side config, pasted by Jacopo after the install; nothing in
  this repository touches Windows files). `chezmoi apply` writes the same block to
  `~/.local/share/bootstrap/claude-app-mcp.json`; merge its entries into `mcpServers`:

```json
{
  "mcpServers": {
    "backlog-company": {
      "command": "wsl.exe",
      "args": ["-d", "AgentDev", "-u", "agent", "--", "/home/agent/.local/bin/agentdev-backlog-mcp", "cortex-kb-company"]
    },
    "backlog-product-harness": {
      "command": "wsl.exe",
      "args": ["-d", "AgentDev", "-u", "agent", "--", "/home/agent/.local/bin/agentdev-backlog-mcp", "cortex-kb-product-harness"]
    },
    "backlog-household-signal-harness": {
      "command": "wsl.exe",
      "args": ["-d", "AgentDev", "-u", "agent", "--", "/home/agent/.local/bin/agentdev-backlog-mcp", "cortex-kb-household-signal-harness"]
    },
    "backlog-madplanner-v2": {
      "command": "wsl.exe",
      "args": ["-d", "AgentDev", "-u", "agent", "--", "/home/agent/.local/bin/agentdev-backlog-mcp", "cortex-kb-madplanner-v2"]
    },
    "backlog-consultancy-website": {
      "command": "wsl.exe",
      "args": ["-d", "AgentDev", "-u", "agent", "--", "/home/agent/.local/bin/agentdev-backlog-mcp", "cortex-kb-consultancy-website"]
    },
    "backlog-home-energy-lab": {
      "command": "wsl.exe",
      "args": ["-d", "AgentDev", "-u", "agent", "--", "/home/agent/.local/bin/agentdev-backlog-mcp", "cortex-kb-home-energy-lab"]
    },
    "backlog-hermes": {
      "command": "wsl.exe",
      "args": ["-d", "AgentDev", "-u", "agent", "--", "/home/agent/.local/bin/agentdev-backlog-mcp", "cortex-kb-hermes"]
    },
    "backlog-historical-ingestion": {
      "command": "wsl.exe",
      "args": ["-d", "AgentDev", "-u", "agent", "--", "/home/agent/.local/bin/agentdev-backlog-mcp", "cortex-kb-historical-ingestion"]
    },
    "backlog-linkedin-ops": {
      "command": "wsl.exe",
      "args": ["-d", "AgentDev", "-u", "agent", "--", "/home/agent/.local/bin/agentdev-backlog-mcp", "cortex-kb-linkedin-ops"]
    },
    "backlog-orw": {
      "command": "wsl.exe",
      "args": ["-d", "AgentDev", "-u", "agent", "--", "/home/agent/.local/bin/agentdev-backlog-mcp", "cortex-kb-orw"]
    }
  }
}
```

- **Tests and probe:** `python3 ci/backlog-mcp-test.py` (run by `ci/lint.sh`) starts a stub
  server through the launcher for each allowed name and checks the refusals (private and
  unknown names, paths, `..`, repository-path and nested escaping symlinks, callbacks in config
  and task metadata, unsafe git settings, a missing or parent Backlog config), runs the pinned
  server over stdio through the launcher against a temporary Backlog project (tools listed,
  `task_list` leaves the project unchanged), checks that this block and `claude-app-mcp.json`
  match the launcher's allowlist, and checks the agent-session configs. After an install,
  `python3 ci/backlog-mcp-probe.py` speaks MCP to the real `~/.local/bin/agentdev-backlog-mcp`
  for `cortex-kb-company`, lists its tools and runs `task_list` read-only.

### Browser tests in a project gate

`gate-browser` runs browser and persona specs in the trusted host gate using the
machine verification pack (no install or download). Lanes write specs; invoking
the helper without `TASK_GATE_RUN_ID` prints
`SKIP (lane: browser tests run in the trusted gate)` and exits 77.
Suite aggregators must count 77 as a skip only in lanes; it is a failure in a
trusted gate. Bootstrap's `ci/lint.sh` does this for its browser fixture suite.

Add this recipe to the project's code repository and call it from its `check`
suite, with that same skip handling:

```just
browser:
    gate-browser --server 'exec npm run dev -- --host "$HOST" --port "$PORT" --strictPort' --ready / 'acceptance/**/*.spec.ts'
```

The server command must honor `HOST=127.0.0.1` and `PORT`, and fail on an occupied
port. Use `exec` for a single server process; the helper stops its process group
on success, failure, readiness timeout or interruption. The default port is
`TASK_GATE_PORT_BASE`; `--port-offset 0..199` selects another port in that gate's
reserved range. Run helpers sequentially or assign distinct offsets.
Preserve `TASK_GATE_RUN_ID`, `TASK_GATE_PORT_BASE` and `TMPDIR` if the check recipe
clears its environment. Do not set a gate run ID in a lane.

Specs import `test`/`expect` from `@playwright/test` and use `page.goto('/')`.
The helper supplies the machine modules, Chromium, `baseURL`, and a generated
configuration; it does not load a project's Playwright config. Browser context
requests and WebSockets are restricted to the allocated origin; service workers
are blocked. Specs and server commands are trusted project code: Node fetch,
custom browser contexts and subprocesses are not a network sandbox and must not
contact external origins. Readiness accepts a local path and does not follow
redirects. `--timeout` sets readiness seconds (default 30); the browser suite has
a two-minute limit.

The list reporter prints each spec result followed by a helper PASS/FAIL summary.
Server logs, JSON results, failure traces and screenshots live in a unique
`$TMPDIR/gate-browser-*` directory for the lifetime of the gate TMPDIR.
`python3 ci/gate-browser-test.py` checks lane skipping and, in the trusted gate,
runs a static page in Chromium and checks allocated ports and teardown after
passing and failing specs.

### Verification pack (Playwright)

Tooling for SPEC-0015 (independent black-box verification of a task's acceptance
criteria); the method is in `skills-proposed/verify-ui/SKILL.md` for the PM to move
into cortex-core's `skills/`.

- **Pin (alpha, deliberate):** `@playwright/test`, `@playwright/mcp` and
  `@axe-core/playwright` in the npm lockfile. `@playwright/mcp` has never been released
  against a stable Playwright (every version depends on an exact `-alpha-` build), and
  the MCP server and the test runner must share one Playwright version so they use the
  same browser build. So the whole set runs on that alpha: `@playwright/test` and
  `playwright-core` carry the same version, and an npm `overrides` entry stops
  axe-core's open peer range from pulling a second copy. Stable would mean a different
  browser build per tool, so the alpha is accepted on purpose. Revisit when
  `npm view @playwright/mcp dependencies` shows a release pinned to a stable
  Playwright, or if the alpha breaks a run. Then bump `@playwright/mcp` and set the
  other two to the version its `playwright` dependency names;
  `ci/verification-pack-test.py` checks that they match.
- **Browser:** `~/.local/bin/playwright` and `playwright-mcp` wrap the pinned binaries
  with one shared cache (`PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright`) that
  sessions, the MCP server and test runs all use. `install.sh` runs
  `~/.local/share/bootstrap/verify-setup.sh` as agent after `qmd-setup.sh`, outside the
  sandbox: it downloads Chromium for the pinned version, checks with `ldd` that the
  browsers resolve every system library, and registers the MCP server at user scope
  (`claude mcp add --scope user playwright -- ~/.local/bin/playwright-mcp`, stdio,
  headless Chromium). Nothing downloads at test time.
- **System libraries:** the 27 packages `playwright install-deps --dry-run chromium`
  lists (NSS, NSPR, ALSA, X fonts, Xvfb and the font packages) are pinned in
  `system/apt-base.lock` at the snapshot's candidate versions and installed as root by
  `system/base.sh` from `install.sh`; no apt runs from an agent session. When the
  Playwright pin moves, rerun the dry-run and update the lock.
- **just:** `JUST_URL` and `JUST_SHA256` in `pins.env` (hash-verified download like the
  other release binaries); the tools script installs `~/.local/bin/just` and checks its
  version. Bump by hand.
- **Per project:** the founder or the PM runs `verify-enable <knowledge-repo>` (a
  `cortex-kb-<name>` checkout) from a normal shell. It adds `acceptance/` (Playwright
  config with desktop and phone viewports, trace and screenshot on failure, no HTML
  report; an example test tagged `@EXAMPLE-AC1`; axe-core helper), links
  `acceptance/node_modules` to the pinned install, and adds a `just verify <url> [tag]`
  recipe to the repository's `justfile`. Evidence is written to
  `acceptance/evidence/<run>/` inside the knowledge repository; `verify-enable`
  gitignores `evidence/` and `node_modules` in `acceptance/.gitignore`, so it is never
  committed. Existing files are kept; a different config is never overwritten.
- **Tests:** `python3 ci/verification-pack-test.py` checks the pins and version match,
  the `just` pin, the apt lock entries, the wiring and `verify-enable` against temporary
  repositories. When the pinned install is present it also checks that the written
  config loads, and when `just` is present that it accepts the recipe; with a startable
  Chromium in the shared cache it runs the example against a local static page. Only
  `install.sh` on the real machine proves the Chromium download, that the libraries
  install from the lock, and that browser smoke run.

### Local quality gate (pre-commit)

Every project repository runs the same checks before each commit, replacing the
secret scan lost with CI. The config (`~/.local/share/bootstrap/pre-commit/pre-commit-config.yaml`)
uses only `repo: local` hooks that call pinned binaries on the agent's PATH, so
nothing is fetched at commit time and it works inside the agent sandbox:

- **gitleaks** on the staged diff (`gitleaks git --pre-commit --staged`, values redacted;
  a repository's own `.gitleaks.toml` applies).
- **lychee** on changed Markdown, offline and `file` links only: a broken relative
  link fails; web links are never fetched.
- **osv-scanner** on changed lockfiles (npm, pnpm, yarn, bun, uv, poetry, pdm, Pipenv,
  `requirements*.txt`, Cargo, `go.mod`, Composer, Bundler) with `--offline`, against the
  databases in `~/.cache/osv-scalibr`. A lockfile whose ecosystem has no offline
  database fails closed.
- **package-cap-check** on every tracked Python manifest, lock file, Dockerfile and compose
  file, against the package caps below.

A change without lockfiles takes about a second. A lockfile adds about one second for
PyPI and about six for npm (its database is about 200 MB).

- **Pins:** lychee and osv-scanner are release binaries with SHA-256 in `pins.env`;
  pre-commit and its dependencies come from the hashed
  `home/dot_local/share/bootstrap/pre-commit/requirements.lock` (`uv pip compile
  --generate-hashes`), installed with `uv pip sync --require-hashes` into
  `~/.local/share/bootstrap/pre-commit/venv` and linked to `~/.local/bin/pre-commit`.
  The monthly pin updater refreshes this lock and the inspect lock with
  `uv pip compile --upgrade --exclude-newer` (see Monthly pin review).
- **Sandbox:** inside the agent sandbox `~/.cache` is read-only. pre-commit then runs
  local hooks from its existing store, so the tools script creates the store
  (`pre-commit gc`) and the hook install creates it too.
- **Vulnerability data:** `osv-db-refresh` downloads the PyPI, npm, crates.io, Go,
  Packagist and RubyGems databases (only changed files, zip-checked). The tools script
  runs it on install. The user timer `osv-db-refresh.timer` now runs security-watch
  daily, refreshing these databases before scanning installed environments
  (`OnCalendar=daily`, `Persistent=true` catches up after downtime). It uses no agent
  runs and is enabled by `qmd-setup.sh` next to `qmd-index.timer`.
- **Errors:** `osv-db-refresh` tries every ecosystem, logs `ERROR <ecosystem>` for each
  failure, keeps that ecosystem's previous database and exits non-zero, so the unit
  shows in `systemctl --user --failed` and `journalctl --user -u osv-db-refresh`. Only a
  fully successful run touches `~/.cache/osv-scalibr/.last-refresh`. The drift report
  (`checks/agent-drift.py`) reports `osv-db-never-refreshed`, or `osv-db-stale` when that
  stamp is more than 15 days old.
- **Accepted exceptions:** an `osv-scanner.toml` next to a lockfile can ignore one
  advisory, with a reason and an `ignoreUntil` date; after that date the advisory blocks
  again. Bootstrap's own npm lock ignores `GHSA-vfj7-8cjw-p6xm` (braces 3.0.3, no fixed
  version, reached through qmd's fast-glob) until 2027-01-05
  (`home/dot_local/share/bootstrap/npm/osv-scanner.toml`).
- **New projects:** `new-project create` writes `.pre-commit-config.yaml` into both
  repositories and runs `pre-commit install`.
- **Existing repositories:** the founder or the PM runs `pre-commit-enable <checkout>`
  from a normal shell as the agent user (agent sessions cannot write `.git/hooks`).
  It writes the config if absent, refuses to overwrite a different one, and installs the
  hook. Then commit `.pre-commit-config.yaml` on a branch and open a pull request. Each
  clone needs `pre-commit-enable` (or `pre-commit install`) once, because hooks are not
  cloned.
- **Tests:** `ci/pre-commit-test.sh` checks that a planted fake secret, a broken relative
  link and a lockfile without an offline database are blocked, and that a clean change
  passes from a read-only store. With a local PyPI database it also checks that a known
  advisory is blocked. It also covers `pre-commit-enable`. `ci/new-project-test.sh`
  checks the config and hook in both new repositories. `ci/osv-db-test.py` checks the
  npm exception (one ID, a reason, the expiry date, still matching the lock), the units
  and their enablement, and that `osv-db-refresh` reports failures, keeps old data and
  stamps only a full success. With osv-scanner installed, a synthetic offline npm
  database checks that the exception passes the lock until it expires, that an expired
  copy blocks it and that the lock without the exception is blocked.

### Antigravity CLI (agy)

`agy` (Antigravity CLI) is a third vendor for research, read-only audits and plan
red-teams. Bootstrap installs the release tarball pinned in `pins.env` (`AGY_URL`,
`AGY_SHA256`): the archive SHA-256 is checked before extraction. When the rendered bootstrap
run-on-change script executes, it replaces a pinned binary whose contents differ. It redownloads
and verifies the release archive on each run. The installed binary and version directory
are made read-only, and `~/.local/bin/agy` is a wrapper that sets
`AGY_CLI_DISABLE_AUTO_UPDATE=true` before executing the pinned binary. An owner can undo those
permissions; the drift check reports `agy-version-drift` if the version or recorded binary SHA
changes. The vendor's install script is never run. The pinned release is inside the seven-day
cooldown, but is taken as a security-fix exception to Jacopo's 2026-10-25 rule never to run
outdated, potentially vulnerable versions. Its release fixes cross-user plugin MCP OAuth token
use; 1.4.6 also closed sandbox access to `~/.docker` and `~/.kube`, and 1.4.4 fixed telemetry
settings reverting while settings load. The cooldown rule itself is unchanged.

- **Who runs it:** the PM, from an unsandboxed shell (`agy -p "<prompt>"`, `--add-dir <path>`).
  The policy hook refuses `agy` and `antigravity` in every lane session (T-48). It refuses
  `--dangerously-skip-permissions` in every session.
- **Data:** public material only until Google's data terms are checked. According to Google's
  forum reply of 2026-10-09, prompts are not collected for training when `Enable Telemetry` is
  off; public material only until the probe confirms that. The CLI's README says
  Google collects and uses "Interactions data" by default, with an opt-out in settings. No
  `settings.json` key for that opt-out, or for Remote Control, is documented in the release
  notes we pinned, so bootstrap writes no settings file. The opt-outs are Jacopo's account
  action in the CLI (`/config`: `Enable Telemetry` off; Remote Control off).
- **Credentials:** the Google sign-in lives in `~/.gemini/` (credentials under
  `~/.gemini/antigravity-cli/`). Both vendors' sandboxes deny reads of `~/.gemini`: Claude's
  `Read(~/.gemini/**)` deny and sandbox read-deny in the managed guardrails, and Codex's
  `~/.gemini` filesystem deny in `system/codex-requirements.toml` (T-52, the `lane` and `review`
  profiles). The policy hook adds token-level refusals of any Bash, Write, Edit or `apply_patch`
  access to `~/.gemini` in both vendors. The hook matches command tokens, so it does not catch a
  recursive search from the home folder (for example `grep -r ... ~`); the sandbox read-deny is
  the control for those reads.
- **Login and probe:** the one-time login and the probe are in [docs/agy-probe.md](docs/agy-probe.md).

### Package caps (the litellm lock rule)

`system/agent-policy/package-caps.toml` caps `litellm` at `1.106.2`, with exact
pins or upper bounds for PyPI and pinned tags for images naming `litellm`,
including `ghcr.io/berriai/litellm` / `berriai/litellm`. The text rule applies
to any image repository with that package name, regardless of registry.
Version `1.107.0` and the affected floating image tags were compromised on
2026-10-21 ([incident note](https://docs.litellm.ai/blog/security-incident-oct-21-2026)).
The rule rests on resolver constraints plus the offline repository gate:

- **Resolvers:** `install.sh` generates `/usr/local/lib/agent-policy/constraints.txt`.
  Agent pip's `[install] constraint` and `build-constraint` keys point there;
  `~/.profile` exports `UV_CONSTRAINT` and `UV_BUILD_CONSTRAINT` for `uv pip
  install`/`compile`, `uv tool install` and `uvx`. `uv lock`/`sync`/`run` ignore
  user-level constraints: `new-project` writes a complete `[project]` file with
  generated `[tool.uv] constraint-dependencies` and `build-constraint-dependencies`.
  The synthetic-wheel probes prove runtime conflicts on pip 26.2.1 and uv 0.12.21;
  build dependency resolution is not probed.

- **Gate:** every standard pre-commit config runs `package-cap-check` over tracked
  requirements/constraints (`.txt`/`.in`, including files under `requirements/`
  and `constraints/` directories), all `*.in` files, pyproject, uv/Poetry/Pipenv/PDM locks,
  Dockerfiles, compose YAML, setup metadata and PEP 723 scripts. Bootstrap uses
  `python3 ci/package-cap-check.py` with its checkout's policy before installation.
  Lock files are always parsed, even without capped names; a malformed lock fails
  closed. Capped entries must resolve at or below the cap and use registry sources;
  git, path and URL sources fail. Every scanned non-lock file uses one allowlist:
  every case-insensitive **substring** occurrence of a capped name must belong to
  an allowed form. These are exact pins or upper bounds at or below the cap
  (`litellm==1.106.2`, `litellm[proxy] <= 1.104.0`, quoted or unquoted), Poetry
  assignments (`litellm = "==1.106.2"`, `litellm = {version = "<=1.104.0"}`,
  optionally with extras), image tags (`litellm:v1.106.2`, optionally `-stable`
  and an approved digest), wheel/sdist filenames whose complete version is at
  or below the cap, and explicitly allowed other package names.
  Unknown forms fail closed: bare names, `litellm.git`, `litellm_proxy`, unknown
  joined names, and ranges without an allowed upper bound fail even with an
  allowed lock pin. Rewrite refused declarations as plain pins.
  Container discovery uses `Dockerfile`, `Dockerfile.*`, `*.Dockerfile`,
  `compose*.y*ml` or `docker-compose*.y*ml`, anywhere in the repository.
  A version must end the word; quote or escape concatenation after a safe version
  prefix is refused.
  The policy's `allowed_names` currently exempts only `langchain-litellm`, a
  separate distribution. An exemption matches that complete name; extending
  it into an unknown name such as `langchain-litellm-unknown` still fails.
  Comments and heredocs count; manifest/shell/YAML syntax, variables, requirement
  includes and `dockerfile:` references are not interpreted. Non-lock files without a
  capped-name substring are never parsed further. Violations report the file,
  line and occurrence; unapproved image digests and local versions still fail. Resolver
  constraints still cap transitive installs. The installed command requires the
  root-owned policy at `/usr/local/lib/agent-policy/package-caps.toml`.

`pipx`, Poetry, images and existing uv projects without workspace constraints
have only the gate guard for the scanned file formats listed above. Files such
as conda's `environment.yml`, `Pipfile` and `Containerfile` are outside discovery.
The gate checks tracked declarations/locks at commit time; it does not block ad hoc installs. The agent hook keeps its ordinary
command rules, including allowing `uv sync`.

To add a package, add `[packages.<pypi-name>]` with required keys `max_version`,
`reason` and `source` (HTTPS). Optional keys are `images`, `added` and
`approved_digests` and `allowed_names`. To allow another package name containing
the capped name, add it to that package's `allowed_names` map with a nonempty
reason, for example `allowed_names = { langchain-litellm = "Separate LangChain integration distribution" }`.
Keep this list small; unknown names remain refused. `images` records known vendor
repositories; the text rule also checks other registries.
Reinstall bootstrap to regenerate constraints; the PM handles
existing-project rollout. Gate and resolver tests are `ci/package-caps-test.py`
and `ci/package-cap-resolver-test.py`.

## Skills, prompt reminders and the minutes watchdog

- **Skills:** the skills in `Jacopo1991/cortex-core` are distributed with `gh skill`.
  `install.sh` runs `home/dot_local/share/bootstrap/skills-setup.sh` as `agent`, outside the
  sandbox: `gh skill install Jacopo1991/cortex-core --all --scope user` for `claude-code` and
  `codex`, with `--force` on the first run only (it replaces the hand-copied `slim-workflow`
  folders; a stamp in `~/.local/state/bootstrap` marks it done). It needs `gh auth login` as
  `agent`. The user timer `gh-skill-update.timer` runs `gh-skill-update` daily with
  `Persistent=true` (plain installs and updates, no agent runs). It first installs the skills that
  are new in the repository, for both agents and without `--force`, then runs `gh skill update --all`,
  so a skill added to cortex-core reaches every seat without a manual step. New ones cannot be
  installed with `gh skill install --all`: without `--force` that refuses and installs nothing as
  soon as one skill is already present, so the script lists the repository's skills
  (`gh skill install Jacopo1991/cortex-core` prints `name<TAB>description` when piped) and installs
  each by name; "already installed" means nothing to do. A failure is printed,
  exits non-zero and shows in `systemctl --user --failed` and `journalctl --user -u gh-skill-update`.
  The drift report allows `gh-skill-update` and any skill whose `SKILL.md` names
  `Jacopo1991/cortex-core`; hand copies and other sources are `global-skill-outside-bootstrap`.
- **Prompt reminder:** `agent-policy/context_reminder.py` is wired as a native
  `UserPromptSubmit` hook for Claude Code and Codex (`hookSpecificOutput.additionalContext`,
  reminders for slim-workflow, stopping when the same check/test fails after two real fix
  attempts or at the time box (the task brief's stop rules take precedence), reporting as
  the task or skill asks, the given worktree and PM adoption of direct requests, git/gh commands
  on their own, verify-ui only for tasks with a UI to verify, and github-ci). For Claude Code
  it is also a `PreToolUse` hook that adds a "read the github-ci skill first" note before a
  Write/Edit/Bash that touches `.github/workflows` or changes repository settings (`gh repo edit`,
  `gh secret set`, `gh workflow run`, `gh api` with a write method on a settings endpoint). It
  only adds context; it never blocks, and `pre_tool_use.py` stays the policy gate. Codex has the
  same per-prompt reminder; only Claude Code gets the tool-call note.
  Missing files, wrong paths and hook denials are resolved within authority or reported;
  they do not count as repeated failing checks and guardrails must never be bypassed.
- **Windows, run by the founder** from elevated PowerShell (the founder's own gh login, tasks run
  in the logged-in session, not S4U): `windows\install-skills-task.ps1` installs the skills for the
  Windows-side Claude Code and Codex (`--force` once) and registers `MachineBootstrap-Skills-Update`
  (daily 09:15: installs skills that are new in cortex-core without `--force`, same per-name
  method as the Linux timer, then `gh skill update --all`). `windows\install-minutes-watchdog-task.ps1
  [-IncludedMinutes 3000]` registers `MachineBootstrap-Minutes-Watchdog` (daily 09:45): it reads
  `gh api /users/Jacopo1991/settings/billing/usage`, sums this month's Actions minutes, and shows a
  Windows notification once at 50% and once at 80% of the allowance (state and
  `minutes-watchdog.log` under `%LOCALAPPDATA%\machine-bootstrap`; every check is logged). Both
  tasks run protected copies from `C:\ProgramData\machine-bootstrap\founder` and are declared in
  the inventory's maintenance allowlist, so they are not reported as task drift. Both installers
  also copy `job-ping.ps1` next to them, so re-run them once to get the pings below (and the new-skill
  installs for the skills task).
- **Tests:** `ci/skills-hooks-test.py` (units, setup, drift allowlist, update command with a fake
  `gh`, hook output and wiring, Windows script shape) runs in `ci/lint.sh`;
  `ci/founder-tasks-test.ps1` (threshold and notification logic with `ci/fixtures/billing-usage.json`
  instead of the API, task XML, allowlist) runs in the Windows PowerShell 5.1 job.

## Job monitoring (Healthchecks.io pings)

`task-watch` reads existing records every 15 minutes when the PM enables
`task-watch.timer` after review. Class definitions live in the
[company skill, The company thread](https://github.com/Jacopo1991/cortex-core/blob/main/skills/company/SKILL.md).
Run `task-watch --coverage` for detection and interim company checks. It sends no
messages and calls no models; the company seat carries findings in its next report.
The timer is shipped without an enablement symlink.

PMs list live user unit/timer names, one per line (blank lines and `#` comments
allowed), in `~/.config/task/live-units`. Only those units produce P4 findings.
Timers also inspect their triggered unit when needed: an empty next elapse is
`no next run` only while the timer is not `running` and its unit is neither
`activating` nor `active`.
Asks are read from `backlog/tasks/*.md` under these six repositories in `~/cortex/`:
`cortex-kb-company`, `cortex-kb-product-harness`, `cortex-kb-household-signal-harness`,
`cortex-kb-madplanner-v2`, `cortex-kb-consultancy-website` and `cortex-kb-home-energy-lab`.
The default report check uses the company `STATUS.md` commit age, with a two-hour
cadence plus a ten-minute grace, counting only 07:00-23:00 Europe/Copenhagen
(including daylight saving). The seat may name existing report/collector heartbeat files through
`~/.config/task/watch-reports.json`, a mapping such as
`{"seat-report":{"path":"reports/company.md","seconds":7200},"collector":{"path":"reports/collector.json","seconds":3600}}`.
Paths are relative to home or absolute; `"git":true` uses the file's latest commit
instead of its modification time. Missing files are findings. Without a collector
source it remains interim. No new collector, paid-cap, hold or alert schema is inferred.

`~/.local/state/task/watch.jsonl` records opens and closes with class, source, key,
detail (P4 reason), first_seen and last_seen. `watch-state.json` updates open
findings' last_seen on every successful scan without repeating ledger lines;
cleared findings close once.
Source errors fail the run without closing prior findings. A lock serializes runs.
Only watcher state is written. Each run calls the existing `job-ping task-watch`
with its result (provision `TASK_WATCH_URL` in the usual pings file); coverage is read-only.

Every scheduled job bootstrap manages pings its own Healthchecks.io check when it finishes: the
check's URL on success, `<URL>/fail` on failure. There is no account in the repository and no URL
in Git. The founder creates the checks below (the names are the check names; `job-ping` matches a
check to its URL through the key in the last column) and puts each ping URL in an owner-provisioned
file; a job whose key is missing skips its ping silently.

| Check name | Job | Runs | Key in the pings file |
| --- | --- | --- | --- |
| `qmd-index` | `qmd-index.timer` (AgentDev) | every 15 minutes | `QMD_INDEX_URL` |
| `task-watch` | `task-watch.timer` (PM enables after review) | every 15 minutes | `TASK_WATCH_URL` |
| `osv-db-refresh` | `osv-db-refresh.timer` via security-watch (AgentDev) | daily | `OSV_DB_REFRESH_URL` |
| `security-watch` | `osv-db-refresh.timer` (same timer, AgentDev) | daily | `SECURITY_WATCH_URL` |
| `gh-skill-update` | `gh-skill-update.timer` (AgentDev) | daily | `GH_SKILL_UPDATE_URL` |
| `windows-skills-update` | `MachineBootstrap-Skills-Update` task | daily 09:15 | `WINDOWS_SKILLS_UPDATE_URL` |
| `windows-minutes-watchdog` | `MachineBootstrap-Minutes-Watchdog` task | daily 09:45 | `WINDOWS_MINUTES_WATCHDOG_URL` |
| `backrest-backup` | Backrest plan `agentdev-daily`, via its hooks | daily 02:30 | `BACKREST_BACKUP_URL` |

- **Files** (one `KEY=https://hc-ping.com/<uuid>` per line, nothing else needed): in AgentDev
  `~/project-data/healthchecks/pings.env` (runtime data, never Git; `chmod 600`); on Windows
  `%LOCALAPPDATA%\machine-bootstrap\pings.env` (keep it readable by your account only). AgentDev
  needs the AgentDev keys, Windows the last three. A ping URL is a secret: it is read at run
  time, handed to curl on stdin and never printed, logged, put in a command line or in Backrest's
  `config.json`. `JOB_PINGS_FILE` overrides the AgentDev path (tests use it).
- **Never in the way:** `job-ping` (installed to `~/.local/bin`; `windows\job-ping.ps1` on
  Windows) uses a 3 second connect and 6 second total timeout (5 seconds on Windows), no retries,
  drops all output and always exits 0. A missing file, a missing key, a value that is not an
  `https://` URL or an unreachable server changes nothing about the job.
- **What a ping means:** a success ping is sent only when the job fully succeeded (`qmd-refresh`
  after fetch, update and embed, `osv-db-refresh` after every ecosystem, `gh-skill-update` after the
  installs and the update, the Windows tasks when their script returns 0). Backrest pings from plan
  hooks: `CONDITION_SNAPSHOT_SUCCESS` pings the check and `CONDITION_SNAPSHOT_ERROR` pings `/fail`, both
  through `job-ping.ps1` copied to `%LOCALAPPDATA%\backrest`. A backup the distro-start hook
  cancels sends neither, so the check's own grace time reports it.
- **Check settings to choose in Healthchecks.io:** match each check's period to the "Runs" column
  and give it a grace time that covers the machine being off. The AgentDev jobs only run while the
  distro runs; the systemd timers catch up (`Persistent=true`) when it starts, but `qmd-index` has no
  catch-up, so give it a long grace time or expect an alert after the distro was stopped.
- **After pulling this change:** the AgentDev side needs `chezmoi apply` (as `install.sh` does) to
  get `job-ping`; the Windows side needs `install-skills-task.ps1`, `install-minutes-watchdog-task.ps1`
  and `install-backrest.ps1 -Force` re-run from elevated PowerShell (copies `job-ping.ps1`, and for
  Backrest adds the hooks to the plan). Not covered: the inventory, compaction and
  inventory-mirror jobs, which were not part of this change.
- **Tests:** `ci/job-ping-test.py` (which URL, silent skips, timeouts, no URL in a command line, dead
  network) runs in `ci/lint.sh`; the Windows twin is tested in `ci/founder-tasks-test.ps1`.

## Daily installed-environment security scan

Security-watch replaces the old refresh-only schedule using the existing
`osv-db-refresh.timer`; there is no separate `security-watch.timer`, so the timer
count stays the same. The timer now runs daily, catches up after downtime, adds up
to one hour of random delay, and starts `security-watch.service`. Each scan first
starts the existing `osv-db-refresh.service` and waits for it; refresh failure fails
the run even when cached data can still be scanned.

The fixed roots, relative to the agent HOME, are:

- `.local/share/bootstrap/npm` (including installed `node_modules` and the lockfile);
- `.local/share/bootstrap/pre-commit/venv` and `.local/share/bootstrap/inspect/venv`;
- `.local/share/bootstrap/gpu-venv` when installed;
- `dev_workspace`, recursively, including project venv metadata and lockfiles.

The scanner is pinned to osv-scanner 2.6.0. Scans use `--offline`,
`--offline-vulnerabilities`, `--no-resolve`, `--no-ignore` and the bundled
`python/wheelegg` and `javascript/packagejson` plugins alongside default lockfile
plugins. Gitignored environments are included. Scanner path exclusions omit only
top-level children of `~/dev_workspace` named
customer-harness/CustomerHarness, typo3-dkm/typo3-dkm-plugin/Typo3/DKM,
photo-organizer, secrets and project-data (case insensitive). Dependencies such as
`node_modules/secrets` remain in scope. `.git`, `.worktrees` and `.work` are
excluded at any depth within each scan root, also case insensitive. The service
additionally masks the listed private repositories and `~/project-data` with
`InaccessiblePaths=`; it leaves cortex and the security state directory accessible.
Directory and file symlinks are not read by the scanner (covered by fixture canaries).
Missing required roots and scan errors fail the run; an absent optional GPU
environment has status `absent`, does not fail the run and is never counted as
clean. Every scanned root must return packages.
The npm tree must include `@openai/codex`, pre-commit must include `pre-commit`,
and Inspect must include `inspect-ai`, with PEP 503 name normalisation.
`dev_workspace` must include `@openai/codex`, present in the main bootstrap
checkout's tracked `home/dot_local/share/bootstrap/npm/package-lock.json`.
Error/fatal and `fserr` diagnostics fail the root. Fixture probes of scanner 2.6.0
produce `fserr (non-permission error): ... not implemented` for a chmod-000 directory,
and `Error during extraction:` for corrupt package.json and METADATA files.
The count guard fails a root below half the highest count of its last 7 accepted
runs. Rejected counts never enter history. Run `security-watch --accept-counts`
after an intentional shrink: it performs the full scan with normal pings and exit
codes, resetting history separately for roots that pass every other check.
Empty roots, diagnostics and missing sentinels cannot be accepted. Resets appear
in the report and STATUS.

Exemptions use native `osv-scanner.toml` `[[IgnoredVulns]]` entries with an advisory
`id`, `ignoreUntil` and `reason`. The only approved source is
`~/.local/share/bootstrap/npm/osv-scanner.toml`; a missing file means no exemptions.
The wrapper validates it with `tomllib`: non-empty ID and reason, a TOML date no
more than 90 days ahead, and no other tables or fields. A symlink is refused.
Every root receives one explicit `--config` pointing to the validated snapshot
(or an empty file), so configs planted in projects or dependencies have no effect.
JSON, Markdown and STATUS list the exemptions with reasons, expiry and whether
they are effective. Exemptions expire at the start of `ignoreUntil` in UTC;
exemptions apply to all roots (the approved reasons must be understood globally).
Expired exemptions expose the finding again.

Dated JSON reports under `~/.local/state/security/` contain scanner findings and
diagnostics per root; `latest.json` points to the latest result by content. A dated
Markdown summary and the `## Security` section of `~/cortex/cortex-core/STATUS.md`
show package/advisory counts and advisory IDs per root (the first five IDs in
STATUS). The job replaces only that section, preserving adjacent sections.
Exemption IDs/reasons, advisory IDs and errors are collapsed to one line and
limited to 200 characters in STATUS, with a leading `#` escaped.
Atomic `counts.json` holds each root's last seven accepted counts, also carried
by every dated JSON report. Missing, corrupt or non-dict latest/count state falls
back to the newest valid dated report. Without one this is a first run with
no baseline; the normal extraction, diagnostic and sentinel checks still apply.
Dated JSON/Markdown pairs older than 60 days are pruned by filename timestamp,
always retaining the newest valid dated report and the special state/config files.
A non-blocking `state/run.lock` covers the entire run, including manual resets;
overlapping runs exit with a clear error. This does not guard other writers of STATUS.
It calls `job-ping security-watch 0` for a clean completed scan, or `1` for findings,
refresh/scan errors or a failed status update. Pings pending: `SECURITY_WATCH_URL` must be
added to the existing owner-managed pings file (Jacopo's action); until then failures show
in STATUS and the journal. No report body is sent.
The unit masks `~/project-data` read-only (`TemporaryFileSystem=`) and binds back only
`~/project-data/healthchecks` (`BindReadOnlyPaths=`), so job-ping's default `pings.env` stays
readable. job-ping is best effort by design: it always exits 0, so no wrapper change is needed,
and a missing key or unreachable endpoint never fails the scan. Fixture tests verify calls,
not delivery; the live proof reads the unit's view of the pings path after install.

Refresh has a 30-minute budget and each of five roots has two minutes, within the
45-minute unit timeout. An incomplete record replaces `latest.json` and the dated
reports before work begins. SIGTERM stops scanning and leaves an incomplete result
and failure ping. Reports are written before STATUS, and survive STATUS decode or
write errors. Reports use a private umask. This wrapper does not install or repair packages.
`ci/security-watch-test.py` and `ci/osv-db-test.py` use synthetic packages and local
advisory zip fixtures, temporary HOME/state and stubbed refresh/ping commands; they
never refresh a real feed or scan an installed environment.

## Backups (Backrest on Windows)

Backrest (restic) runs on Windows as your own process, not in AgentDev, so the agent
still has no C: access. `windows/install-backrest.ps1` (elevated PowerShell, same account
that owns the distro) installs the pinned, hash-checked Backrest release and the matching
restic release (the Backrest Windows zip has none) side by side under
`%LOCALAPPDATA%\backrest\bin`, starts it at logon (web UI on `http://127.0.0.1:9898`) and writes
its config: repository `C:\backups\restic`, plan `agentdev-daily` at 02:30, keep 7 daily /
4 weekly / 6 monthly, prune Sundays 03:30 and check Sundays 04:30. Sources, read through
`\\wsl.localhost\AgentDev\home\agent\`: `project-data`, `cortex`, and `dev_workspace/` limited to
consultancy-website, customer-harness, typo3-dkm-plugin plus every repository without a GitHub
remote, skipping git worktrees (decided at install time and printed; re-run with `-Force` after adding one, which replaces only
the `agentdev-daily` plan). Excluded: node_modules, .venv, `__pycache__`, .cache, dist, build,
.next, target and the qmd index.

`\\wsl.localhost` only answers while the distro runs, so the plan has a hook that runs
`wsl.exe -d AgentDev -u agent -- true` before every backup and cancels the run if it fails.

**Repository password:** Backrest creates the restic repository when it starts, so the installer
asks for the password once (typed twice, never echoed) before the first start. It is kept only
in `%LOCALAPPDATA%\backrest\restic-password.txt`, readable by your account alone, and the repository
points restic at it with `RESTIC_PASSWORD_FILE`; it is never in `config.json`, logs or output.
Re-running the installer keeps an existing file and does not ask again. **Losing the password
means losing the backups**, so also store it in your password manager. Then open the web UI and
create the Backrest user.

For a restore with the CLI, set `$env:RESTIC_PASSWORD_FILE` to that file instead of typing the password.

**Restore** (Backrest UI: Repo, Snapshots, browse, Restore; or the restic CLI from a normal
Windows shell, with the installed `%LOCALAPPDATA%\backrest\bin\restic.exe`):

```powershell
$env:RESTIC_REPOSITORY = 'C:\backups\restic'          # restic asks for the password
restic snapshots                                       # list snapshots (note the ID)
restic ls latest --path '\\wsl.localhost\AgentDev\home\agent\project-data\typo3-dkm' | more
restic restore latest --include '*\typo3-dkm\notes.md' --target C:\restore          # one file
restic restore latest --include '*\project-data\typo3-dkm' --target C:\restore      # a whole project
```

Restores go to `C:\restore`, then copy what you need back into AgentDev from your own shell.
Backups were read over a Windows network path, so Linux file modes (such as the executable bit)
and symlinks are not preserved; re-apply `chmod` after restoring scripts.

## Pins and repeatability

| Component | Pin location |
| --- | --- |
| Ubuntu WSL image (24.04.5 amd64) and SHA-256 | `windows/new-distro.ps1` |
| chezmoi, mise, Node 24 LTS, Python 3.12, uv, native Claude Code, bws, SecretSpec | `home/.chezmoitemplates/pins.env` |
| Node/Python/uv global tools | `home/dot_config/mise/config.toml` |
| Codex CLI, ccusage, Backlog.md, qmd, Desktop Commander and Playwright (test, MCP, axe-core), including npm dependency versions/integrities | `home/dot_local/share/bootstrap/npm/package-lock.json` |
| Explicit apt package versions (held after install) | `system/apt-*.lock` |
| Ubuntu dependency resolution | Signed Ubuntu snapshot `20260930T000000Z` |
| apt signing key hashes | `home/.chezmoitemplates/pins.env` |
| GPU Python packages and all dependencies with hashes | `checks/gpu-requirements.lock` |
| GPU container image | `checks/gpu-image.env` |
| Public test model | Revision in `checks/gpu-smoke.py` |
| ShellCheck, agent/CI Gitleaks, lychee, osv-scanner and release SHA-256 | `home/.chezmoitemplates/pins.env` |
| pre-commit and all dependencies with hashes | `home/dot_local/share/bootstrap/pre-commit/requirements.lock` |
| Backrest and restic releases and SHA-256 | `windows/install-backrest.ps1` |
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
that admin, asserts all six `/etc/wsl.conf` keys and the vendor-host block in
`/etc/hosts` after each install, probes the AgentDev shell connector over stdio, then runs
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
The npm tools and Python locks keep a 7-day cooldown (T-43): each npm tool moves
to the newest stable version published more than 7 days ago, read from the
packument's `time` field; the npm lock is regenerated with an explicit `--before`
date; and the hashed pre-commit and inspect locks are recompiled with uv
`--upgrade --exclude-newer` at the same cutoff. A younger release that was
skipped is named in the summary. The GPU lock stays a manual update.
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

The uv cooldown applies only where versions are chosen: this monthly bump and a
hand-run lock upgrade. Outside agent sessions (the policy hook refuses inline
`--exclude-newer`), upgrade a `uv.lock` with the cutoff passed explicitly:

```bash
uv lock --upgrade --exclude-newer "$(date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%SZ)"
```

It never goes in the user uv config, `uv.toml` or `[tool.uv]`: there it refuses young
exact pins and breaks existing locks (T-37). The policy hook refuses uv commands
when a `uv.toml` or `pyproject.toml [tool.uv]` between the working directory and
the repository root sets `exclude-newer` or `exclude-newer-package`, as it
refuses the inline `--exclude-newer` and `UV_EXCLUDE_NEWER` forms.


### Repository commands and approved network access

Founder decision 2026-10-02: Claude Code uses `acceptEdits`, with explicit
allow rules for `git *`, `gh *`, `/usr/bin/git *` and `/usr/bin/gh *`, and no
Git/gh ask rules. The deny list and PreToolUse hook remain active as managed settings.
The sandbox remains enabled, with `allowUnsandboxedCommands: false`.
Its exclusions are exactly `git`, `gh`, `git *`, `gh *`, `/usr/bin/git *`
and `/usr/bin/gh *`: bare names alone only match argument-less calls.
Claude Code's `sandbox.network.allowedDomains` and Codex's managed
`permissions.lane.network.domains` allow exactly `pypi.org`,
`files.pythonhosted.org`, `registry.npmjs.org`, `github.com`,
`objects.githubusercontent.com`, `codeload.github.com`, `raw.githubusercontent.com`,
`release-assets.githubusercontent.com` and `openrouter.ai` (no wildcards). These are the host rules
pinned in `ci/agent-config-test.py`; the same test pins both vendors' writable
cache paths. The native GitHub CLI may use its existing credential store;
agents must never read or print credential files.

Force pushes (including lease variants, short flags, force refspecs and flags
after the remote/branch), remote branch deletion (delete flags, empty-source
refspecs, mirror/prune), `gh repo delete/edit`, `gh api` DELETE requests,
`gh release delete`, `gh secret` and `gh ruleset` are denied. Native Claude
rules cover common spellings, and the shared hook inspects parsed arguments,
including inherited repository selectors and attached API method values.
The previous never-merge, workspace, Git metadata/configuration and secret
checks remain in force.

Codex uses the managed `lane` permission profile extending `:workspace`, with
`network.enabled = true`, `enable_socks5 = false` and `allow_local_binding = true`.
The network policy lives in `/etc/codex/requirements.toml`; a config-defined
profile of the same name conflicts with the managed profile. User config sets
`[features] network_proxy = true` to activate enforcement. The old
`[sandbox_workspace_write] network_access` setting is ignored for managed profiles
and has been removed. The `[features.network_proxy]` policy table has been
replaced by the profile policy above and a boolean activation flag. The `review`
profile extends `:read-only` without network. Approval policy stays `on-request`
with the user approval reviewer.
Four native allow prefixes permit routine Git/gh to run outside the sandbox
without a prompt. Explicit forbidden prefixes (in
`/etc/codex/requirements.toml`) and the shared hook block the forbidden operations above.
Prompt rules there cover `git reset/rebase/cherry-pick/revert` and `git stash pop/drop/clear`.
These approval-required forms must use a plain command without global
selectors; the hook refuses selector variants that would otherwise miss a
literal prefix rule. This is a bounded list of destructive primitives, not
a general classifier for every possible Git operation. The PermissionRequest
hook denies other shell escalations and never grants approval itself.

Current-repository `git switch <branch>`, `git switch -c <branch>` and
`git checkout -b <branch>` remain allowed. Branch creation also permits
`git switch -c <name> <start-point>` and `git switch --create <name> <start-point>`
in the current repository. A start point must be a plain hexadecimal object ID
(4–40 characters, or a full 64-character SHA-256 ID) or literal `origin/<branch>`;
Git still verifies that it resolves to a commit. No extra switch options,
force-create, attached/abbreviated creation flags, revision expressions, other
remote refs or configuration overrides are permitted by this exception.
The no-start-point `--create <name>` spelling is also supported. Switch's branch-creation `-c`
is distinct from Git's global configuration override; global `git -c`,
attached configuration overrides and creation in another repository stay
blocked.

The shared policy permits log, show, diff, status, rev-parse, ls-files, grep and
blame against repositories under `/home/agent/dev_workspace` and
`/home/agent/cortex`. A narrow fetch exception also permits
`git fetch` / `git fetch origin` and `git -C <sibling> fetch [origin]` in either
approved root, updating refs/objects/FETCH_HEAD without changing the working
tree. Sibling fetch does not need the read-only pager/lock selectors below;
extra fetch options, refspecs, URLs and remote groups remain blocked there.
Sibling repositories with effective submodule configuration (including retained
activation/URL settings and includes), submodule files, gitlinks or stored
metadata are excluded: plain fetch can recurse into unchecked child remotes.
Inspection failures also deny this exception.
Bare fetch resolves the branch-configured remote (falling back to origin)
and verifies its effective GitHub URL. Pull, merge, checkout, switch and other
Git writes remain limited to the current repo. Build agents never merge pull requests.
The default-branch merge exception is `git merge [--no-edit] origin/<default branch>`
inside the current repo on an attached non-default branch, with one effective
GitHub origin URL and a resolvable symbolic `origin/HEAD`. Unknown/default/detached
branches, other refs and every other merge option are denied by the shared hook.
Plain `git merge main` is also allowed when `main` is the recorded origin default,
with the same checks and an unambiguous local `main` ref. This uses the local
branch, which may lag `origin/main`; dispatch guidance uses `origin/main`.
The one other exception is `git merge --ff-only origin/<the current branch>` (catching up
your own task branch from GitHub): same checks, exactly that argument form, never on the
default branch, and the remote-tracking ref must resolve uniquely.
The locally recorded origin default must be established by authorized setup;
the hook never contacts a remote to discover it.
Local-only repositories (no remote configured at all) have their own exception:
`git merge [--no-edit] main` on an attached task branch, where `main` must resolve
uniquely to the local branch (a tag of that name blocks it) and no branch merge options
may be configured. Adding any remote switches the repository back to the rules above.
A read-only preview, `git merge-tree --write-tree [--name-only] HEAD <source>`, is
allowed wherever the same `<source>` may be merged.
Cross-repository reads use `git --no-pager --no-optional-locks -C <repo>`
so no pager runs and status cannot refresh another repository's index. Add
`--no-textconv` to cross-repository log, show, diff and blame, and
`--no-ext-diff` to log, show and diff. Spell short options separately; aggregated
short flags and external input-file options are refused. Current-repository
Git commands retain ordinary add/commit/push use. All agent `git config`
writes are blocked, including local configuration and hook/fsmonitor/SSH/
alias/filter/diff keys; only explicit `--get` and `--list`/`-l` reads are
permitted. Owner-provisioned identity/remotes remain in place. A future key
exception needs an exact-name decision in a later PR. New
repository/worktree creation, output-file primitives and local filesystem
remotes require separate setup and are blocked. Push and pull require
one explicit named remote; fetch may omit it as described above. Effective URLs (including pushurl, insteadOf
and pushInsteadOf rewrites)
must be GitHub HTTPS or SSH. No remote connection is made by the policy check.
Common flags such as `git push -u origin <branch>` work. Supported fetch/pull
value options use `--key=value`; custom transport/push options, direct URL
operands and unspecified push/pull remotes are refused rather than guessed.
Repeated and attached `-C` selectors resolve sequentially; symlink escapes,
config/git-dir/work-tree overrides and side-effecting cross-repo read options
are refused. The existing host-command, file-write and staged-secret checks
remain in force. Native Edit/Write/MultiEdit and patches, including patch
renames, refuse direct Git metadata writes. Shell redirection and recognised
file writers use the same protection for lexical/resolved `.git` paths,
symlink aliases, and Git's reported worktree/common metadata directories.
Target-ancestor checks also protect existing nested repositories' separately
named Git directories (HEAD/objects/refs or HEAD/commondir), without scanning
the repository tree. Git file-write/staging operands use the same metadata
protection. Indirect pathspec files, directory move/remove/restore and agent
Git cleanup are refused. Git pathspec magic and directory/implicit staging
(`git add .`, `-A`/`-u` without filenames) are refused; stage explicit filenames.
For example, `git add file1 file2`, `git commit -m "..."` and
`git push -u origin <branch>` remain usable. Recursive cleanup could otherwise delete nested metadata without
naming it. Explicit ordinary-file operations and add/commit/push remain usable.
Ambiguous transfer options, directory transfers and directory mutations are
refused to prevent indirect metadata writes. Plain file copies remain usable.
Git `-c`/`--config-env`, environment-prefixed commands, inherited/per-tool
`GIT_CONFIG*`, `GIT_DIR`, `GIT_EXEC_PATH` and related execution overrides are
refused before helper Git calls. Normal Git commands may still update their
own index/refs; agents cannot edit the control files directly.
This hook is a command guard, not an OS boundary against
arbitrary programs or a replacement for reviewing the exact approval.

Sources checked 2026-10-02: [Claude sandbox modes and exclusions](https://code.claude.com/docs/en/sandboxing#the-unsandboxed-retry-escape-hatch),
[Codex approvals](https://learn.chatgpt.com/docs/agent-approvals-security),
[Codex execution rules](https://learn.chatgpt.com/docs/agent-configuration/rules),
and [Codex PermissionRequest](https://learn.chatgpt.com/docs/hooks#permissionrequest).
Pinned Codex `0.159.2` exposes the same PermissionRequest event and deny output;
CI checks the managed hook and uses the installed CLI's execution-policy checker.
No authentication, GitHub write or owner-host check is run by these tests.
