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
`system/inventory-mirror.py` refuses to mount in the shared namespace.

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
Claude Code uses the founder-approved `acceptEdits` mode, allows routine
Git/gh commands, and uses the native Linux sandbox with unsandboxed retries disabled and startup failing if the sandbox is
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
itself still lives in user settings; moving it into managed settings is a
separate follow-up. The pinned Claude Code release is 2.1.287 or later, so
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

**Package registries.** The Claude Code sandbox may reach exactly `pypi.org`, `files.pythonhosted.org` and `registry.npmjs.org`, and may write `~/.cache/uv` and `~/.npm`. Other registries are added here, by PR, when a project needs them. Codex keeps `network_access = false`: its domain-allowlist proxy does not yet resolve allowlisted hosts inside the Linux sandbox (openai/codex#22387), so a Codex lane asks for approval (`on-request`) to run an install outside the sandbox.

**One repository per session.** Agents commit only in the repository the session was opened in. Build lanes report in chat; the PM records `STATUS.md`, backlog tasks and decisions in the knowledge repository.

### Cortex knowledge search (qmd)

[qmd](https://www.npmjs.com/package/@tobilu/qmd) (MIT) indexes the Markdown in
every `~/cortex/*` repository for local keyword and semantic search.

- **Pin:** `@tobilu/qmd` 2.8.3 in the npm lockfile (v2); `tools.sh` links
  `~/.local/bin/qmd`. The monthly pin updater leaves it alone, so bump it by hand.
- **Config:** chezmoi renders `~/.config/qmd/index.yml` with one collection per
  non-hidden `~/cortex/*` directory (mask `**/*.md`). Each one-line context is
  `<repo>: <title>` taken from that repository's `INTENT.md` front matter at
  apply time, so no project detail lives in this public source. After adding a
  repository run `chezmoi apply`.
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

A change without lockfiles takes about a second. A lockfile adds about one second for
PyPI and about six for npm (its database is about 200 MB).

- **Pins:** lychee and osv-scanner are release binaries with SHA-256 in `pins.env`;
  pre-commit and its dependencies come from the hashed
  `home/dot_local/share/bootstrap/pre-commit/requirements.lock` (`uv pip compile
  --generate-hashes`), installed with `uv pip sync --require-hashes` into
  `~/.local/share/bootstrap/pre-commit/venv` and linked to `~/.local/bin/pre-commit`.
  The monthly pin updater does not cover them, so bump them by hand.
- **Sandbox:** inside the agent sandbox `~/.cache` is read-only. pre-commit then runs
  local hooks from its existing store, so the tools script creates the store
  (`pre-commit gc`) and the hook install creates it too.
- **Vulnerability data:** `osv-db-refresh` downloads the PyPI, npm, crates.io, Go,
  Packagist and RubyGems databases (only changed files, zip-checked). The tools script
  runs it on install, and the user timer `osv-db-refresh.timer` runs it weekly
  (`OnCalendar=weekly`, `Persistent=true`, so a week missed while the distro was stopped
  is caught up on the next start). It is a plain download with no agent runs, enabled by
  `qmd-setup.sh` next to `qmd-index.timer`.
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
  stamps only a full success. With osv-scanner and the offline npm database installed,
  it also checks that the exception passes the lock until it expires, that an expired
  copy blocks it and that the lock without the exception is blocked.

## Skills, prompt reminders and the minutes watchdog

- **Skills:** the skills in `Jacopo1991/cortex-core` are distributed with `gh skill`.
  `install.sh` runs `home/dot_local/share/bootstrap/skills-setup.sh` as `agent`, outside the
  sandbox: `gh skill install Jacopo1991/cortex-core --all --scope user` for `claude-code` and
  `codex`, with `--force` on the first run only (it replaces the hand-copied `slim-workflow`
  folders; a stamp in `~/.local/state/bootstrap` marks it done). It needs `gh auth login` as
  `agent`. The user timer `gh-skill-update.timer` runs `gh-skill-update` (a plain
  `gh skill update --all`, no agent runs) daily with `Persistent=true`; a failure is printed,
  exits non-zero and shows in `systemctl --user --failed` and `journalctl --user -u gh-skill-update`.
  The drift report allows `gh-skill-update` and any skill whose `SKILL.md` names
  `Jacopo1991/cortex-core`; hand copies and other sources are `global-skill-outside-bootstrap`.
- **Prompt reminder:** `agent-policy/context_reminder.py` is wired as a native
  `UserPromptSubmit` hook for Claude Code and Codex (`hookSpecificOutput.additionalContext`,
  six lines: slim-workflow, stop after two failed attempts or at the time box, report in chat and
  the PR description, git/gh network commands on their own, verify-ui, github-ci). For Claude Code
  it is also a `PreToolUse` hook that adds a "read the github-ci skill first" note before a
  Write/Edit/Bash that touches `.github/workflows` or changes repository settings (`gh repo edit`,
  `gh secret set`, `gh workflow run`, `gh api` with a write method on a settings endpoint). It
  only adds context; it never blocks, and `pre_tool_use.py` stays the policy gate. Codex has the
  same per-prompt reminder; only Claude Code gets the tool-call note.
- **Windows, run by the founder** from elevated PowerShell (the founder's own gh login, tasks run
  in the logged-in session, not S4U): `windows\install-skills-task.ps1` installs the skills for the
  Windows-side Claude Code and Codex (`--force` once) and registers `MachineBootstrap-Skills-Update`
  (`gh skill update --all`, daily 09:15). `windows\install-minutes-watchdog-task.ps1
  [-IncludedMinutes 3000]` registers `MachineBootstrap-Minutes-Watchdog` (daily 09:45): it reads
  `gh api /users/Jacopo1991/settings/billing/usage`, sums this month's Actions minutes, and shows a
  Windows notification once at 50% and once at 80% of the allowance (state and
  `minutes-watchdog.log` under `%LOCALAPPDATA%\machine-bootstrap`; every check is logged). Both
  tasks run protected copies from `C:\ProgramData\machine-bootstrap\founder` and are declared in
  the inventory's maintenance allowlist, so they are not reported as task drift.
- **Tests:** `ci/skills-hooks-test.py` (units, setup, drift allowlist, update command with a fake
  `gh`, hook output and wiring, Windows script shape) runs in `ci/lint.sh`;
  `ci/founder-tasks-test.ps1` (threshold and notification logic with `ci/fixtures/billing-usage.json`
  instead of the API, task XML, allowlist) runs in the Windows PowerShell 5.1 job.

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

**Your one step:** open the web UI, create the Backrest user, and set the password on the
`restic` repository (it creates the repository on save). The password is not in this repository;
keep a copy in your password manager, since without it the backups cannot be read.

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
| Codex CLI, ccusage, Backlog.md, qmd and Playwright (test, MCP, axe-core), including npm dependency versions/integrities | `home/dot_local/share/bootstrap/npm/package-lock.json` |
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

Founder decision 2026-10-02: Claude Code uses `acceptEdits`, with explicit
allow rules for `git *`, `gh *`, `/usr/bin/git *` and `/usr/bin/gh *`, and no
Git/gh ask rules. The existing deny list and PreToolUse hook remain active.
The sandbox remains enabled, with `allowUnsandboxedCommands: false`.
Its exclusions are exactly `git`, `gh`, `git *`, `gh *`, `/usr/bin/git *`
and `/usr/bin/gh *`: bare names alone only match argument-less calls.
The only network allow is `sandbox.network.allowedDomains` of exactly `pypi.org`
and `files.pythonhosted.org` (no wildcards), so `uv sync` works in the sandbox;
`sandbox.filesystem.allowWrite` adds only `~/.cache/uv`, created by the tools
script, as its writable cache. CI asserts both lists exactly. The native GitHub CLI may use its
existing credential store; agents must never read or print credential files.

Force pushes (including lease variants, short flags, force refspecs and flags
after the remote/branch), remote branch deletion (delete flags, empty-source
refspecs, mirror/prune), `gh repo delete/edit`, `gh api` DELETE requests,
`gh release delete`, `gh secret` and `gh ruleset` are denied. Native Claude
rules cover common spellings, and the shared hook inspects parsed arguments,
including inherited repository selectors and attached API method values.
The previous never-merge, workspace, Git metadata/configuration and secret
checks remain in force.

Codex keeps `workspace-write`, `network_access = false`, `on-request` and the
user approval reviewer. Four native allow prefixes permit routine Git/gh to
run outside the sandbox without a prompt. Explicit forbidden prefixes and
the shared hook block the forbidden operations above. Native prompt rules
cover `git reset/rebase/cherry-pick/revert` and `git stash pop/drop/clear`.
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
The only local merge exception is `git merge [--no-edit] origin/<default branch>`
inside the current repo on an attached non-default branch, with one effective
GitHub origin URL and a resolvable symbolic `origin/HEAD`. Unknown/default/detached
branches, other refs and every other merge option are denied by the shared hook.
The locally recorded origin default must be established by authorized setup;
the hook never contacts a remote to discover it.
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
