[CmdletBinding()]
param([Parameter(Mandatory)][ValidatePattern('^[A-Za-z][A-Za-z0-9_-]{0,47}$')][string]$Name,
      [switch]$IfIdle,
      [string]$LogPath)
. "$PSScriptRoot/common.ps1"
try {
    if ($LogPath) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null
        "START: $(Get-Date -Format o) $Name" | Add-Content -LiteralPath $LogPath -Encoding utf8
        Invoke-WslCompaction -Name $Name -IfIdle:$IfIdle | ForEach-Object {
            $_ | Add-Content -LiteralPath $LogPath -Encoding utf8
            Write-Output $_
        }
    } else { Invoke-WslCompaction -Name $Name -IfIdle:$IfIdle }
} catch {
    if ($LogPath) { "FAIL: $(Get-Date -Format o) $Name $($_.Exception.Message)" | Add-Content -LiteralPath $LogPath -Encoding utf8 }
    throw
}
