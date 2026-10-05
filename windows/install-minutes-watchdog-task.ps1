[CmdletBinding()]
param([ValidateRange(1, 10000000)][int]$IncludedMinutes = 3000)
. "$PSScriptRoot/common.ps1"
. "$PSScriptRoot/founder-task.ps1"

# Run by the founder from elevated Windows PowerShell, as the account whose gh login can read
# the account's billing usage: registers the daily GitHub Actions minutes watchdog.
# -IncludedMinutes is the plan's monthly allowance (default 3000); re-run to change it.
function Install-MinutesWatchdogTask {
    [CmdletBinding()]param()
    Assert-BootstrapAdministrator
    Initialize-FounderTaskDirectory
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'minutes-watchdog.ps1') -Destination (Join-Path $script:FounderTaskDirectory 'minutes-watchdog.ps1') -Force
    Register-FounderTask -Name 'MachineBootstrap-Minutes-Watchdog' -ScriptFile 'minutes-watchdog.ps1' `
        -ExtraArguments "-IncludedMinutes $IncludedMinutes" -Time '09:45' `
        -Description 'Daily GitHub Actions minutes check; notifies at 50% and 80% of the included minutes.'
    Write-Output "Daily minutes watchdog registered (09:45, plan allowance $IncludedMinutes minutes, runs when you are logged in). Log: $env:LOCALAPPDATA\machine-bootstrap\minutes-watchdog.log"
}
if ($MyInvocation.InvocationName -ne '.') { Install-MinutesWatchdogTask }
