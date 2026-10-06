[CmdletBinding()]
param([string]$DistroName = 'AgentDev', [switch]$Force)
. "$PSScriptRoot/common.ps1"

# Run by the founder from elevated Windows PowerShell, as the account that owns the distro.
# Backrest (restic) runs here on Windows as the founder's own process, never inside AgentDev, so
# the agent's no-C:-access boundary stays intact. It reads AgentDev through
# \\wsl.localhost\AgentDev\home\agent and writes to C:\backups\restic.
#
# The script asks once for the repository password (typed twice) and keeps it only in
# %LOCALAPPDATA%\backrest\restic-password.txt, readable by your account alone; the repository's env
# points restic at it (RESTIC_PASSWORD_FILE). It never goes into config.json, logs or output.
# Keep a copy in your password manager too: a lost password means unreadable backups.

$script:BackrestVersion = 'v1.14.1'
$script:BackrestUrl = 'https://github.com/garethgeorge/backrest/releases/download/v1.14.1/backrest_Windows_x86_64.zip'
$script:BackrestSha256 = '16b6d902303df77c14601325eada8656158e8a0531973d6094d22638ff45f545'
# The Windows Backrest zip ships no restic and does not download one; pin the release Backrest
# v1.14.1 expects (0.19.1) and point BACKREST_RESTIC_COMMAND at it.
$script:ResticVersion = 'v0.19.1'
$script:ResticUrl = 'https://github.com/restic/restic/releases/download/v0.19.1/restic_0.19.1_windows_amd64.zip'
$script:ResticSha256 = 'da948ad707ed690426473aaba2046cd61f8f90f6f0e7dab6be0d5796531de67d'
$script:ResticZipEntry = 'restic_0.19.1_windows_amd64.exe'
$script:BackrestRepoPath = 'C:\backups\restic'
$script:BackrestTaskName = 'MachineBootstrap-Backrest'
$script:BackrestPingCheck = 'backrest-backup'  # Healthchecks.io check name; key BACKREST_BACKUP_URL in pings.env
$script:BackrestAlwaysIncluded = @('consultancy-website', 'customer-harness', 'typo3-dkm-plugin')
$script:BackrestExcludes = @(
    '**/node_modules', '**/.venv', '**/__pycache__', '**/.cache', '**/dist', '**/build',
    '**/.next', '**/target', '**/qmd/*.sqlite*', '**/.qmd'
)
$script:BackrestBackupCron = '30 2 * * *'
$script:BackrestPruneCron = '30 3 * * 0'
$script:BackrestCheckCron = '30 4 * * 0'

function Get-LocalOnlyRepoNames {
    # Repositories (a .git DIRECTORY) under the workspace root with no GitHub remote, plus the
    # always-included local-only ones. Worktrees (.git is a file) and plain folders are skipped:
    # a worktree's content lives in its main repository.
    param([Parameter(Mandatory)][string]$WorkspaceRoot)
    $names = [System.Collections.Generic.List[string]]::new()
    foreach ($directory in Get-ChildItem -LiteralPath $WorkspaceRoot -Directory) {
        $git = Join-Path $directory.FullName '.git'
        if (-not (Test-Path -LiteralPath $git -PathType Container)) { continue }
        $gitConfig = Join-Path $git 'config'
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
    param([Parameter(Mandatory)][string[]]$Sources, [Parameter(Mandatory)][string]$PasswordFile,
          [Parameter(Mandatory)][string]$PingScript,
          [string]$Distro = 'AgentDev', [string]$RepoPath = $script:BackrestRepoPath)
    $schedule = { param($cron) [ordered]@{ cron = $cron; clock = 'CLOCK_LOCAL' } }
    # The password itself never goes into the config: restic reads it from the owner-only file.
    # Backrest initializes the repository at start, so that file must exist before the first start.
    $repo = [ordered]@{
        id = 'restic'
        uri = $RepoPath
        env = @("RESTIC_PASSWORD_FILE=$PasswordFile")
        autoInitialize = $true
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
    # Healthchecks.io: job-ping.ps1 reads the check URL from the owner-provisioned pings file at run
    # time, so no URL is ever written into this config. Backrest runs hook commands in PowerShell on
    # Windows. A ping can never fail the backup (the script always exits 0; ON_ERROR_IGNORE).
    $ping = {
        param($condition, $result)
        [ordered]@{
            conditions = @($condition)
            onError = 'ON_ERROR_IGNORE'
            actionCommand = [ordered]@{ command = "powershell.exe -NoProfile -NonInteractive -File `"$PingScript`" -Check $script:BackrestPingCheck -Result $result" }
        }
    }
    $plan = [ordered]@{
        id = 'agentdev-daily'
        repo = 'restic'
        paths = @($Sources)
        excludes = @($script:BackrestExcludes)
        schedule = (& $schedule $script:BackrestBackupCron)
        retention = [ordered]@{ policyTimeBucketed = [ordered]@{ daily = 7; weekly = 4; monthly = 6 } }
        hooks = @($startDistro, (& $ping 'CONDITION_SNAPSHOT_SUCCESS' 'ok'), (& $ping 'CONDITION_SNAPSHOT_ERROR' 'fail'))
    }
    return [ordered]@{ modno = 1; version = 6; instance = 'founder-windows'; repos = @($repo); plans = @($plan) }
}

function Merge-BackrestConfig {
    # Keeps whatever Backrest wrote itself (auth user, other repos and plans) and replaces only the
    # generated plan; the repo is added if missing, otherwise kept as is except that its
    # RESTIC_PASSWORD_FILE entry is set to the generated one.
    param([string]$ExistingJson, [Parameter(Mandatory)]$Generated)
    if ([string]::IsNullOrWhiteSpace($ExistingJson)) { return $Generated }
    $existing = $ExistingJson | ConvertFrom-Json
    $repos = @($existing.repos | Where-Object { $null -ne $_ })
    $ours = $repos | Where-Object id -eq 'restic'
    if (-not $ours) {
        $repos += [pscustomobject]$Generated.repos[0]
    } else {
        $current = $ours.PSObject.Properties['env']
        $entries = @()
        if ($null -ne $current) { $entries = @(@($current.Value) | Where-Object { $_ -and $_ -notlike 'RESTIC_PASSWORD_FILE=*' }) }
        $entries += $Generated.repos[0].env[0]
        $ours | Add-Member -NotePropertyName env -NotePropertyValue $entries -Force
    }
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

function ConvertFrom-SecureStringPlain {
    param([Parameter(Mandatory)][securestring]$Secret)
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secret)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
}

function Read-ConfirmedPassword {
    # Typed twice; never echoed, logged or returned as plain text.
    $first = Read-Host -AsSecureString -Prompt 'Choose the backup repository password (keep a copy in your password manager)'
    $second = Read-Host -AsSecureString -Prompt 'Type it again to confirm'
    $a = ConvertFrom-SecureStringPlain -Secret $first
    $b = ConvertFrom-SecureStringPlain -Secret $second
    if ($a.Length -eq 0) { throw 'An empty password is not allowed.' }
    if (-not [string]::Equals($a, $b, [StringComparison]::Ordinal)) { throw 'The two passwords differ; nothing written.' }
    return $first
}

function New-ResticPasswordFile {
    # Creates the file empty, restricts it to the current user only, and only then writes the secret.
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][securestring]$Password)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    [IO.File]::WriteAllBytes($Path, [byte[]]@())
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.WindowsIdentity]::GetCurrent().User, 'FullControl', 'Allow'))
    Set-Acl -LiteralPath $Path -AclObject $acl
    [IO.File]::WriteAllText($Path, (ConvertFrom-SecureStringPlain -Secret $Password), [Text.UTF8Encoding]::new($false))
}

function Expand-VerifiedZip {
    # Downloads a pinned zip, checks its SHA-256 and only then extracts it.
    param([Parameter(Mandatory)][string]$Url, [Parameter(Mandatory)][string]$Sha256,
          [Parameter(Mandatory)][string]$Destination, [Parameter(Mandatory)][string]$Label)
    $zip = Join-Path ([IO.Path]::GetTempPath()) ('bootstrap-' + [Guid]::NewGuid().ToString('N') + '.zip')
    try {
        Invoke-WebRequest -Uri $Url -OutFile $zip -UseBasicParsing
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant() -ne $Sha256) {
            throw "$Label download hash mismatch; nothing installed."
        }
        New-Item -ItemType Directory -Force -Path $Destination | Out-Null
        Expand-Archive -LiteralPath $zip -DestinationPath $Destination -Force
    } finally { Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue }
}

function Install-BackrestBinary {
    # backrest.exe and restic.exe side by side in $InstallDirectory.
    param([Parameter(Mandatory)][string]$InstallDirectory)
    Expand-VerifiedZip -Url $script:BackrestUrl -Sha256 $script:BackrestSha256 -Destination $InstallDirectory -Label 'Backrest'
    if (-not (Test-Path -LiteralPath (Join-Path $InstallDirectory 'backrest.exe'))) { throw 'backrest.exe missing after extraction.' }
    $stage = Join-Path ([IO.Path]::GetTempPath()) ('bootstrap-restic-' + [Guid]::NewGuid().ToString('N'))
    try {
        Expand-VerifiedZip -Url $script:ResticUrl -Sha256 $script:ResticSha256 -Destination $stage -Label 'restic'
        $extracted = Join-Path $stage $script:ResticZipEntry
        if (-not (Test-Path -LiteralPath $extracted)) { throw "$script:ResticZipEntry missing from the restic zip." }
        Copy-Item -LiteralPath $extracted -Destination (Join-Path $InstallDirectory 'restic.exe') -Force
    } finally { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
}

function Get-BackrestLauncherText {
    # Everything explicit: working directory, config, data, restic binary and bind address, so
    # nothing resolves relative to wherever the launcher was started from.
    param([Parameter(Mandatory)][string]$Base, [Parameter(Mandatory)][string]$ConfigFile,
          [Parameter(Mandatory)][string]$BinDirectory, [string]$Port = '127.0.0.1:9898')
    $data = Join-Path $Base 'data'
    $backrest = Join-Path $BinDirectory 'backrest.exe'
    $restic = Join-Path $BinDirectory 'restic.exe'
    return @"
Set-Location -LiteralPath '$Base'
`$env:BACKREST_CONFIG = '$ConfigFile'
`$env:BACKREST_DATA = '$data'
`$env:BACKREST_RESTIC_COMMAND = '$restic'
`$env:BACKREST_PORT = '$Port'
& '$backrest'
"@
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
    Write-Output 'Backup sources:'
    foreach ($source in $sources) {
        Write-Output "  $source"
        if (-not (Test-Path -LiteralPath $source)) { Write-Warning "Backup source not found: $source" }
    }

    # Backrest initializes the repository at start, so the password must exist before the first start.
    $passwordFile = Join-Path $base 'restic-password.txt'
    if (Test-Path -LiteralPath $passwordFile) {
        Write-Output "Keeping the existing repository password file: $passwordFile"
    } else {
        New-ResticPasswordFile -Path $passwordFile -Password (Read-ConfirmedPassword)
        Write-Output "Repository password stored (current user only): $passwordFile"
    }

    Install-BackrestBinary -InstallDirectory $binDirectory
    New-Item -ItemType Directory -Force -Path $script:BackrestRepoPath, (Join-Path $base 'data') | Out-Null

    $existing = if (Test-Path -LiteralPath $configFile) { Get-Content -LiteralPath $configFile -Raw } else { $null }
    if ($existing -and -not $Force) {
        throw "$configFile exists. Re-run with -Force to refresh only the agentdev-daily plan (a backup is kept)."
    }
    if ($existing) { Copy-Item -LiteralPath $configFile -Destination ($configFile + '.bootstrap-' + [Guid]::NewGuid().ToString('N') + '.bak') }
    $pingScript = Join-Path $base 'job-ping.ps1'
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'job-ping.ps1') -Destination $pingScript -Force
    $config = Merge-BackrestConfig -ExistingJson $existing -Generated (New-BackrestConfig -Sources $sources -PasswordFile $passwordFile -PingScript $pingScript -Distro $DistroName)
    [IO.File]::WriteAllText($configFile, (ConvertTo-BackrestJson -Config $config), [Text.UTF8Encoding]::new($false))

    $launcher = Join-Path $base 'start-backrest.ps1'
    $launcherText = Get-BackrestLauncherText -Base $base -ConfigFile $configFile -BinDirectory $binDirectory
    [IO.File]::WriteAllText($launcher, $launcherText, [Text.UTF8Encoding]::new($false))
    Register-BackrestLogonTask -Launcher $launcher
    Start-ScheduledTask -TaskName $script:BackrestTaskName

    Write-Output "Backrest $script:BackrestVersion installed in $binDirectory; config $configFile."
    Write-Output 'NEXT (you): open http://127.0.0.1:9898 and create the Backrest user. Keep the repository password in your password manager too: losing it means losing the backups.'
}
if ($MyInvocation.InvocationName -ne '.') { Install-Backrest }
