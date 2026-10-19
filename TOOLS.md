# Gate tools

Installed by bootstrap before lanes start; local checks never download tools. Release
versions and SHA-256 hashes live in `home/.chezmoitemplates/pins.env`.

| Tool | Job | Installation | Review |
| --- | --- | --- | --- |
| ShellCheck 0.11.0 | Shell lint (previously downloaded on every check) | chezmoi tools script, `~/.local/bin/shellcheck` | Monthly pin refresh |
| Gitleaks 8.30.1 | Secret scanning | installer and chezmoi tools script | Monthly pin refresh |
| lychee 0.24.2 | Offline relative-link checks | chezmoi tools script | Monthly pin refresh |
| osv-scanner 2.6.0 | Offline dependency checks | chezmoi tools script | Monthly pin refresh |
| pre-commit | Local hook runner; version and dependencies in the hashed requirements lock | chezmoi tools script | Monthly pin refresh |
| chezmoi 2.73.0 | Template rendering | `install.sh`, `/usr/local/bin/chezmoi` | Monthly pin refresh |

These are existing gate dependencies. No replacement tool is introduced. Missing
or mismatched tools fail the gate with an installer diagnostic; a lane must not
install them into the shared home. The PM/admin applies bootstrap before retrying.

The existing GitHub Actions fresh-runner lint job retains its disposable setup
in `ci/lint-install.sh`, only when `GITHUB_ACTIONS=true` and preflight fails.
When tools are already present, it also makes no downloads. `just check` strips
that variable, and the trusted local gate's environment allowlist excludes it.
