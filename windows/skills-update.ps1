[CmdletBinding()]
param([string]$LogPath = (Join-Path $env:LOCALAPPDATA 'machine-bootstrap\skills-update.log'))
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Daily task: install the cortex-core skills that are new in the repository (without --force, so
# nothing already installed is touched), then update the installed ones with `gh skill update
# --all` (plain installs and updates, no agent runs). Each run is logged; a failure is logged and
# makes the task's last result non-zero. The result is also sent to Healthchecks.io (check
# windows-skills-update) when the owner-provisioned pings file has its URL; see job-ping.ps1.
#
# `gh skill install --all` cannot be used for the new ones: without --force it refuses and installs
# nothing as soon as one skill is already present. So the repository's skills are listed and each
# one is installed by name for both agents; "already installed" means nothing to do.

$jobPing = Join-Path $PSScriptRoot 'job-ping.ps1'
if (Test-Path -LiteralPath $jobPing) { . $jobPing } else { function Send-JobPing { param($Check, $Success) } }

$script:SkillsRepository = 'Jacopo1991/cortex-core'

function Write-SkillsLog {
    param([string]$Message, [string]$Path = $LogPath)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    Add-Content -LiteralPath $Path -Value ((Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK') + ' ' + $Message)
}

function Install-NewSkills {
    param([Parameter(Mandatory)][string]$Gh)
    $ErrorActionPreference = 'Continue'  # Windows PowerShell 5.1 turns native stderr into terminating errors under 'Stop'
    # Listing goes to stdout as "name<TAB>description" when not on a terminal; progress is stderr.
    $listing = @(& $Gh skill install $script:SkillsRepository 2>$null | ForEach-Object { [string]$_ })
    if ($LASTEXITCODE -ne 0) { Write-SkillsLog "ERROR gh skill install $script:SkillsRepository (listing the skills) failed"; return $false }
    $names = @(foreach ($line in $listing) {
        $name = ($line -split "`t")[0]
        if ($name -cmatch '^[a-z0-9][a-z0-9._-]*$') { $name }
    })
    if ($names.Count -eq 0) { Write-SkillsLog "ERROR gh skill install $script:SkillsRepository listed no skills"; return $false }
    $ok = $true
    foreach ($name in $names) {
        foreach ($agent in 'claude-code', 'codex') {
            $output = @(& $Gh skill install $script:SkillsRepository $name --agent $agent --scope user 2>&1 | ForEach-Object { [string]$_ })
            $code = $LASTEXITCODE
            if ($code -eq 0) {
                Write-SkillsLog "Installed new skill $name for $agent."
            } elseif (($output -join "`n") -match '(?i)already installed') {
                continue
            } else {
                foreach ($line in $output) { Write-SkillsLog ('gh: ' + $line) }
                Write-SkillsLog "ERROR installing new skill $name for $agent failed (status $code)"
                $ok = $false
            }
        }
    }
    return $ok
}

function Invoke-SkillsUpdate {
    $gh = Get-Command gh -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $gh) { Write-SkillsLog 'ERROR gh not found on PATH'; return 1 }
    $failed = -not (Install-NewSkills -Gh $gh.Source)
    $ErrorActionPreference = 'Continue'  # Windows PowerShell 5.1 turns native stderr into terminating errors under 'Stop'
    $output = @(& $gh.Source skill update --all 2>&1 | ForEach-Object { [string]$_ })
    $code = $LASTEXITCODE
    foreach ($line in $output) { Write-SkillsLog ('gh: ' + $line) }
    if ($code -ne 0) {
        Write-SkillsLog "ERROR gh skill update --all exited with status $code"; $failed = $true
    } elseif (($output -join "`n") -match '(?im)(^|[^a-z])(failed|error)([^a-z]|$)') {
        Write-SkillsLog 'ERROR gh skill update --all reported a failure above'; $failed = $true
    }
    if ($failed) { return 1 }
    Write-SkillsLog 'OK skills current'
    return 0
}

if ($MyInvocation.InvocationName -ne '.') {
    $exitCode = Invoke-SkillsUpdate
    Send-JobPing -Check 'windows-skills-update' -Success ($exitCode -eq 0)
    exit $exitCode
}
