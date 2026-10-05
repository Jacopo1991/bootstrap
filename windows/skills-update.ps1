[CmdletBinding()]
param([string]$LogPath = (Join-Path $env:LOCALAPPDATA 'machine-bootstrap\skills-update.log'))
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Daily task: update the cortex-core skills installed with `gh skill` (plain update, no agent
# runs). Each run is logged; a failure is logged and makes the task's last result non-zero.

function Write-SkillsLog {
    param([string]$Message, [string]$Path = $LogPath)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    Add-Content -LiteralPath $Path -Value ((Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK') + ' ' + $Message)
}

function Invoke-SkillsUpdate {
    $gh = Get-Command gh -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $gh) { Write-SkillsLog 'ERROR gh not found on PATH'; return 1 }
    $ErrorActionPreference = 'Continue'  # Windows PowerShell 5.1 turns native stderr into terminating errors under 'Stop'
    $output = @(& $gh.Source skill update --all 2>&1 | ForEach-Object { [string]$_ })
    $code = $LASTEXITCODE
    foreach ($line in $output) { Write-SkillsLog ('gh: ' + $line) }
    if ($code -ne 0) { Write-SkillsLog "ERROR gh skill update --all exited with status $code"; return 1 }
    if (($output -join "`n") -match '(?im)(^|[^a-z])(failed|error)([^a-z]|$)') {
        Write-SkillsLog 'ERROR gh skill update --all reported a failure above'; return 1
    }
    Write-SkillsLog 'OK skills current'
    return 0
}

if ($MyInvocation.InvocationName -ne '.') { exit (Invoke-SkillsUpdate) }
