# Antigravity CLI (agy) probe

For the PM, after bootstrap has installed `agy` 1.4.0 (T-48). Agents do not run these steps:
the login is Jacopo's, and the probe runs from an unsandboxed PM shell, because lane sessions
are refused `agy` by the policy hook.

Public material only until Google's data terms are checked. Never paste the login URL, the
login code, a token, or any part of `~/.gemini` into a prompt, a repository, a log or a report.

## 1. One-time login (Jacopo)

Jacopo runs this once from Windows, as the `agent` user of the AgentDev distro:

    wsl -d AgentDev -u agent -- agy -p "say hi"

The CLI detects the headless session and prints an authorization URL and a code. Jacopo opens
the URL in his own browser, signs in with his Google account (AI Pro), and enters the code.
Credentials are then kept under `~/.gemini/antigravity-cli/`. Agents cannot read that folder
(see the README section on agy).

Then Jacopo sets the data options in the CLI, since no settings key is documented for them:

- `/config`: `Enable Telemetry` off;
- Remote Control off (no `agy remote-control serve` daemon).

## 2. One prompt per available model

From the PM's unsandboxed shell, for each model the CLI lists (the model list comes from the
CLI, not from this document), send one public-material prompt:

    agy -p "<public prompt>"

Use a prompt with nothing private in it, for example a question about a public library's
documentation. Record the model name and whether the reply came back. Do not use
`--dangerously-skip-permissions` (refused in every session).

## 3. Quota

Read the quota with `/usage` in an interactive `agy` session, or the quota line the CLI prints.
Record the figure as shown, with the time it was read.

## 4. Record (PM)

Fill in after the probe. Keep the record in the PM's own project records, not in this file.

| Item | Result |
| --- | --- |
| Version (`agy --version`) | |
| Date of login | |
| Telemetry (`Enable Telemetry`) | |
| Remote Control | |
| Models the AI Pro login gets | |
| Gemini 4 Argon listed | yes / no |
| Replies to the public prompt, per model | |
| Quota as read, and when | |
