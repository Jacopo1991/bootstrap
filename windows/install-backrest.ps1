[CmdletBinding()]
param([string]$DistroName = 'AgentDev', [switch]$Force)
. "$PSScriptRoot/common.ps1"

# Run by the founder from elevated Windows PowerShell, as the account that owns the distro.
# Backrest (restic) runs here on Windows as the founder's own process, never inside AgentDev, so
# the agent's no-C:-access boundary stays intact. It reads AgentDev through
# \\wsl.localhost\AgentDev\home\agent and writes to C:\backups\restic.
#
# The repository password is NOT set here: open http://127.0.0.1:9898 on first start, create the
# Backrest user, then set the password on the "restic" repository (it is stored in Backrest's
# config.json under your profile; keep a copy in your password manager, a lost password means
# unreadable backups).

$script:BackrestVersion = 'v1.14.1'
$script:BackrestUrl = 'https://github.com/garethgeorge/backrest/releases/download/v1.14.1/backrest_Windows_x86_64.zip'
$script:BackrestSha256 = '16b6d902303df77c14601325eada8656158e8a0531973d6094d22638ff45f545'
$script:BackrestRepoPath = 'C:\backups\restic'
$script:BackrestTaskName = 'MachineBootstrap-Backrest'
$script:BackrestAlwaysIncluded = @('consultancy-website', 'customer-harness', 'typo3-dkm-plugin')
$script:BackrestExcludes = @(
    '**/node_modules', '**/.venv', '**/__pycache__', '**/.cache', '**/dist', '**/build',
    '**/.next', '**/target', '**/qmd/*.sqlite*', '**/.qmd'
)
$script:BackrestBackupCron = '30 2 * * *'
$script:BackrestPruneCron = '30 3 * * 0'
$script:BackrestCheckCron = '30 4 * * 0'

function Get-LocalOnlyRepoNames {
    # Repositories under the workspace root that have no GitHub remote, plus the always-included
    # local-only ones that exist. A .git that is not a directory (worktree) counts as no remote,
    # which errs on the side of backing more up.
    param([Parameter(Mandatory)][string]$WorkspaceRoot)
    $names = [System.Collections.Generic.List[string]]::new()
    foreach ($directory in Get-ChildItem -LiteralPath $WorkspaceRoot -Directory) {
        $gitConfig = Join-Path (Join-Path $directory.FullName '.git') 'config'
        $isAlways = $script:BackrestAlwaysIncluded -contains $directory.Name
        $hasGithub = $false
        if (Test-Path -LiteralPath $gitConfig -PathType Leaf) {
            $hasGithub = [bool](Select-String -LiteralPath $gitConfig -Pattern '^\s*url\s*=.*github\.com' -Quiet)
        }
        if ($isAlways -or -not $hasGithub) { $names.Add($directory.Name) }
    }
    return @($names | Sort-Object -Unique)
}

function Get-BackrestSources {
    param([Parameter(Mandatory)][string[]]$RepoNames, [string]$Distro = 'AgentDev')
    $Root = "\\wsl.localhost\$Distro\home\agent"
    $paths = @("$Root\project-data", "$Root\cortex")
    foreach ($name in $RepoNames) { $paths += "$Root\dev_workspace\$name" }
    return $paths
}

function New-BackrestConfig {
    param([Parameter(Mandatory)][string[]]$Sources, [string]$Distro = 'AgentDev')
    $schedule = { param($cron) [ordered]@{ cron = $cron; clock = 'CLOCK_LOCAL' } }
    # No password on purpose: the founder sets it in the web UI. autoInit creates the restic
    # repository the first time a password is saved.
    $repo = [ordered]@{
        id = 'restic'
        uri = $script:BackrestRepoPath
        autoInit = $true
        autoUnlock = $true
        prunePolicy = [ordered]@{ schedule = (& $schedule $script:BackrestPruneCron); maxUnusedPercent = 10 }
        checkPolicy = [ordered]@{ schedule = (& $schedule $script:BackrestCheckCron); readDataSubsetPercent = 5 }
    }
    # \\wsl.localhost only answers while the distro runs, so each backup starts it first.
    $startDistro = [ordered]@{
        conditions = @('CONDITION_SNAPSHOT_START')
        onError = 'ON_ERROR_CANCEL'
        actionCommand = [ordered]@{ command = "wsl.exe -d $Distro -u agent -- true" }
    }
    $plan = [ordered]@{
        id = 'agentdev-daily'
        repo = 'restic'
        paths = @($Sources)
        excludes = @($script:BackrestExcludes)
        schedule = (& $schedule $script:BackrestBackupCron)
        retention = [ordered]@{ policyTimeBucketed = [ordered]@{ daily = 7; weekly = 4; monthly = 6 } }
        hooks = @($startDistro)
    }
    return [ordered]@{ modno = 1; version = 4; instance = 'founder-windows'; repos = @($repo); plans = @($plan) }
}

function Merge-BackrestConfig {
    # Keeps whatever Backrest wrote itself (auth user, repo password, other repos and plans) and
    # replaces only the generated plan; the repo is added if missing, otherwise left as the founder has it.
    param([string]$ExistingJson, [Parameter(Mandatory)]$Generated)
    if ([string]::IsNullOrWhiteSpace($ExistingJson)) { return $Generated }
    $existing = $ExistingJson | ConvertFrom-Json
    $repos = @($existing.repos | Where-Object { $null -ne $_ })
    if (-not ($repos | Where-Object id -eq 'restic')) { $repos += [pscustomobject]$Generated.repos[0] }
    $plans = @($existing.plans | Where-Object { $null -ne $_ -and $_.id -ne 'agentdev-daily' })
    $plans += [pscustomobject]$Generated.plans[0]
    $existing | Add-Member -NotePropertyName repos -NotePropertyValue $repos -Force
    $existing | Add-Member -NotePropertyName plans -NotePropertyValue $plans -Force
    return $existing
}

function ConvertTo-BackrestJson {
    param([Parameter(Mandatory)]$Config)
    return ($Config | ConvertTo-Json -Depth 12)
}

function Install-BackrestBinary {
    param([Parameter(Mandatory)][string]$InstallDirectory)
    $zip = Join-Path $env:TEMP ('backrest-' + [Guid]::NewGuid().ToString('N') + '.zip')
    try {
        Invoke-WebRequest -Uri $script:BackrestUrl -OutFile $zip -UseBasicParsing
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant() -ne $script:BackrestSha256) {
            throw "Backrest download hash mismatch; nothing installed."
        }
        New-Item -ItemType Directory -Force -Path $InstallDirectory | Out-Null
        Expand-Archive -LiteralPath $zip -DestinationPath $InstallDirectory -Force
    } finally { Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue }
    if (-not (Test-Path -LiteralPath (Join-Path $InstallDirectory 'backrest.exe'))) { throw 'backrest.exe missing after extraction.' }
}

function Register-BackrestLogonTask {
    param([Parameter(Mandatory)][string]$Launcher)
    $powerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute $powerShell -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -File "' + $Launcher + '"')
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $script:BackrestTaskName -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description 'Backrest (restic) web UI and backup scheduler, started at logon.' -Force | Out-Null
}

function Install-Backrest {
    [CmdletBinding()]param()
    Assert-BootstrapAdministrator
    $base = Join-Path $env:LOCALAPPDATA 'backrest'
    $binDirectory = Join-Path $base 'bin'
    $configFile = Join-Path $base 'config.json'
    $workspaceRoot = "\\wsl.localhost\$DistroName\home\agent\dev_workspace"

    Invoke-Wsl -WslArgs @('-d', $DistroName, '-u', 'agent', '--', 'true')
    if (-not (Test-Path -LiteralPath $workspaceRoot)) { throw "Cannot read $workspaceRoot from Windows." }
    $sources = Get-BackrestSources -RepoNames (Get-LocalOnlyRepoNames -WorkspaceRoot $workspaceRoot) -Distro $DistroName
    foreach ($source in $sources) {
        if (-not (Test-Path -LiteralPath $source)) { Write-Warning "Backup source not found: $source" }
    }

    Install-BackrestBinary -InstallDirectory $binDirectory
    New-Item -ItemType Directory -Force -Path $script:BackrestRepoPath, (Join-Path $base 'data') | Out-Null

    $existing = if (Test-Path -LiteralPath $configFile) { Get-Content -LiteralPath $configFile -Raw } else { $null }
    if ($existing -and -not $Force) {
        throw "$configFile exists. Re-run with -Force to refresh only the agentdev-daily plan (a backup is kept)."
    }
    if ($existing) { Copy-Item -LiteralPath $configFile -Destination ($configFile + '.bootstrap-' + [Guid]::NewGuid().ToString('N') + '.bak') }
    $config = Merge-BackrestConfig -ExistingJson $existing -Generated (New-BackrestConfig -Sources $sources -Distro $DistroName)
    [IO.File]::WriteAllText($configFile, (ConvertTo-BackrestJson -Config $config), [Text.UTF8Encoding]::new($false))

    $launcher = Join-Path $base 'start-backrest.ps1'
    $launcherText = @"
`$env:BACKREST_CONFIG = '$configFile'
`$env:BACKREST_DATA = '$(Join-Path $base 'data')'
`$env:BACKREST_PORT = '127.0.0.1:9898'
& '$(Join-Path $binDirectory 'backrest.exe')'
"@
    [IO.File]::WriteAllText($launcher, $launcherText, [Text.UTF8Encoding]::new($false))
    Register-BackrestLogonTask -Launcher $launcher
    Start-ScheduledTask -TaskName $script:BackrestTaskName

    Write-Output "Backrest $script:BackrestVersion installed in $binDirectory; config $configFile."
    Write-Output 'NEXT (you): open http://127.0.0.1:9898, create the Backrest user, and set the password on the "restic" repository.'
    Write-Output "Sources: $($sources -join '; ')"
}
if ($MyInvocation.InvocationName -ne '.') { Install-Backrest }
