[CmdletBinding()]
param([string]$Check, [string]$Result = 'ok',
      [string]$PingsFile = (Join-Path $env:LOCALAPPDATA 'machine-bootstrap\pings.env'))
Set-StrictMode -Version Latest

# Tell Healthchecks.io how a scheduled job went. Dot-source it and call Send-JobPing, or run it
# as a script (the Backrest hooks do): job-ping.ps1 -Check <check-name> -Result ok|fail.
# The URLs are in an owner-provisioned file outside Git, one KEY=URL per line, KEY being the
# check name in capitals with - turned into _ and _URL added (backrest-backup -> BACKREST_BACKUP_URL):
#   %LOCALAPPDATA%\machine-bootstrap\pings.env
# A missing file, a missing key or a value that is not an https:// URL is skipped silently.
# The ping can never fail or delay the job: a short timeout, no retries, every error swallowed.
# The URL is never printed or logged.

function Get-JobPingUrl {
    param([Parameter(Mandatory)][string]$Check, [Parameter(Mandatory)][string]$PingsFile)
    if ($Check -cnotmatch '^[a-z0-9][a-z0-9-]*$') { return $null }
    if (-not (Test-Path -LiteralPath $PingsFile -PathType Leaf)) { return $null }
    $key = ($Check.ToUpperInvariant() -replace '-', '_') + '_URL'
    $url = $null
    foreach ($line in [IO.File]::ReadAllLines($PingsFile)) {
        if ($line.StartsWith($key + '=', [StringComparison]::Ordinal)) { $url = $line.Substring($key.Length + 1) }
    }
    if ($null -eq $url) { return $null }
    $url = $url.Trim().Trim([char[]]@('"', "'")).TrimEnd('/')
    if ($url -cnotmatch '^https://[^\s"\\]+$') { return $null }
    return $url
}

function Send-JobPing {
    param([Parameter(Mandatory)][string]$Check, [bool]$Success = $true,
          [string]$PingsFile = (Join-Path $env:LOCALAPPDATA 'machine-bootstrap\pings.env'),
          [int]$TimeoutSeconds = 5)
    try {
        $ProgressPreference = 'SilentlyContinue'
        $url = Get-JobPingUrl -Check $Check -PingsFile $PingsFile
        if ($null -eq $url) { return }
        if (-not $Success) { $url += '/fail' }
        Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec $TimeoutSeconds -ErrorAction Stop | Out-Null
    } catch { }
}

if ($MyInvocation.InvocationName -ne '.') {
    if ($Check) { Send-JobPing -Check $Check -Success ($Result -in 'ok', 'success', '0') -PingsFile $PingsFile }
    exit 0
}
