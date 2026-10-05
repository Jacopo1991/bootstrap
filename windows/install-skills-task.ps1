[CmdletBinding()]
param()
. "$PSScriptRoot/common.ps1"
. "$PSScriptRoot/founder-task.ps1"

# Run by the founder from elevated Windows PowerShell, as the account whose gh login can read
# Jacopo1991/cortex-core: installs the skills for the Windows-side Claude Code and Codex
# (user scope; --force once replaces hand-copied folders) and registers a daily task that
# runs `gh skill update --all` as that account.
function Install-SkillsTask {
    [CmdletBinding()]param()
    Assert-BootstrapAdministrator
    $gh = Get-Command gh -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $gh) { throw 'gh (GitHub CLI) is not on PATH; install it and run gh auth login first.' }
    & $gh.Source auth status | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'gh is not logged in; run gh auth login first.' }

    $stateDirectory = Join-Path $env:LOCALAPPDATA 'machine-bootstrap'
    $stamp = Join-Path $stateDirectory 'skills-installed'
    if (-not (Test-Path -LiteralPath $stamp)) {
        foreach ($agent in 'claude-code', 'codex') {
            & $gh.Source skill install Jacopo1991/cortex-core --all --agent $agent --scope user --force
            if ($LASTEXITCODE -ne 0) { throw "gh skill install failed for $agent ($LASTEXITCODE)." }
        }
        New-Item -ItemType Directory -Force -Path $stateDirectory | Out-Null
        New-Item -ItemType File -Force -Path $stamp | Out-Null
    } else {
        Write-Output 'Skills already installed here; the daily task keeps them current.'
    }

    Initialize-FounderTaskDirectory
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'skills-update.ps1') -Destination (Join-Path $script:FounderTaskDirectory 'skills-update.ps1') -Force
    Register-FounderTask -Name 'MachineBootstrap-Skills-Update' -ScriptFile 'skills-update.ps1' -Time '09:15' `
        -Description 'Daily gh skill update --all for the cortex-core skills (no agent runs).'
    Write-Output "Daily skills update registered (09:15, runs when you are logged in, catches up after a missed start). Log: $stateDirectory\skills-update.log"
}
if ($MyInvocation.InvocationName -ne '.') { Install-SkillsTask }
