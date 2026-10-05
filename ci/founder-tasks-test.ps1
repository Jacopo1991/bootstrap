Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) { throw 'Founder task tests require Windows PowerShell 5.1.' }
function Assert-Equal { param($Actual, $Expected, [string]$Label); if ($Actual -cne $Expected) { throw "$Label expected '$Expected', got '$Actual'." }; Write-Output "PASS: $Label" }
function Assert-True { param([bool]$Value, [string]$Label); if (-not $Value) { throw "FAIL: $Label" }; Write-Output "PASS: $Label" }
$fixturePath = Join-Path $PSScriptRoot 'fixtures/billing-usage.json'

# Threshold logic of the minutes watchdog, with a fixture instead of the GitHub API.
& {
    . "$PSScriptRoot/../windows/minutes-watchdog.ps1"
    $usage = Get-Content -LiteralPath $fixturePath -Raw | ConvertFrom-Json
    Assert-Equal (Get-ActionsMinutesUsed -Usage $usage) 1600.0 'only Actions minutes are summed (storage and other products ignored)'
    Assert-Equal (Get-MinutesLevel -Used 1499 -Included 3000) 0 'below 50% is quiet'
    Assert-Equal (Get-MinutesLevel -Used 1500 -Included 3000) 50 '50% is announced'
    Assert-Equal (Get-MinutesLevel -Used 2399 -Included 3000) 50 'just below 80% stays at 50'
    Assert-Equal (Get-MinutesLevel -Used 2400 -Included 3000) 80 '80% is announced'
    Assert-Equal (Get-MinutesLevel -Used 9000 -Included 3000) 80 'over the allowance stays at 80'
    Assert-Equal (Get-MinutesLevel -Used 500 -Included 1000) 50 'configurable allowance'
    Assert-Equal (Get-MinutesAlert -Level 50 -Month '2026-10' -State $null) 50 'first crossing announces'
    Assert-Equal (Get-MinutesAlert -Level 50 -Month '2026-10' -State ([pscustomobject]@{ month = '2026-10'; notified = 50 })) 0 'same level is announced once per month'
    Assert-Equal (Get-MinutesAlert -Level 80 -Month '2026-10' -State ([pscustomobject]@{ month = '2026-10'; notified = 50 })) 80 'next level is announced'
    Assert-Equal (Get-MinutesAlert -Level 50 -Month '2026-11' -State ([pscustomobject]@{ month = '2026-10'; notified = 80 })) 50 'a new month starts again'
    Assert-Equal (Get-MinutesAlert -Level 0 -Month '2026-10' -State $null) 0 'no level, no alert'

    $directory = Join-Path $env:TEMP ('bootstrap-watchdog-test-' + [guid]::NewGuid().ToString('N'))
    try {
        $script:used = 1600
        $script:shown = [System.Collections.Generic.List[string]]::new()
        $script:fail = $false
        function Get-BillingUsage {
            if ($script:fail) { throw 'fixture API failure' }
            $value = Get-Content -LiteralPath $fixturePath -Raw | ConvertFrom-Json
            $value.usageItems[0].quantity = $script:used - 600
            return $value
        }
        function Show-WatchdogNotification { param([string]$Title, [string]$Message); $script:shown.Add($Title) }
        $now = [datetime]'2026-10-15T09:45:00'
        Assert-Equal (Invoke-MinutesWatchdog -Included 3000 -Directory $directory -Now $now) 0 'check succeeds'
        Assert-Equal $script:shown.Count 1 'first check at 53% notifies'
        Assert-True ($script:shown[0] -like '*50%*') 'notification names the 50% level'
        Invoke-MinutesWatchdog -Included 3000 -Directory $directory -Now $now | Out-Null
        Assert-Equal $script:shown.Count 1 'repeat check at the same level stays quiet'
        $script:used = 2500
        Invoke-MinutesWatchdog -Included 3000 -Directory $directory -Now $now | Out-Null
        Assert-Equal $script:shown.Count 2 'crossing 80% notifies again'
        Assert-True ($script:shown[1] -like '*80%*') 'notification names the 80% level'
        $script:fail = $true
        Assert-Equal (Invoke-MinutesWatchdog -Included 3000 -Directory $directory -Now $now) 1 'API failure returns non-zero'
        Assert-Equal $script:shown.Count 2 'API failure shows no notification'
        $log = @(Get-Content -LiteralPath (Join-Path $directory 'minutes-watchdog.log'))
        Assert-Equal $log.Count 4 'every check is logged'
        Assert-True ($log[0] -like '*used=1600*percent=53.3*level=50*alert=50*') 'log line carries used, percent, level and alert'
        Assert-True ($log[3] -like '*ERROR fixture API failure*') 'failure is logged'
    } finally {
        if (Test-Path -LiteralPath $directory) { Remove-Item -LiteralPath $directory -Recurse -Force }
    }
}

# Task shape and the drift allowlist: the registered tasks are the declared maintenance tasks.
& {
    . "$PSScriptRoot/../windows/inventory.ps1"
    . "$PSScriptRoot/../windows/founder-task.ps1"
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $directory = $script:FounderTaskDirectory
    function New-FixtureTask { param([string]$Name, [string]$Arguments, [string]$Working = $directory)
        [pscustomobject]@{ TaskName = $Name; TaskPath = '\'; State = 'Ready'; Actions = @([pscustomobject]@{ Execute = $powershell; Arguments = $Arguments; WorkingDirectory = $Working }) } }
    $skillsArguments = Get-FounderTaskArguments -ScriptPath "$directory\skills-update.ps1"
    $watchdogArguments = Get-FounderTaskArguments -ScriptPath "$directory\minutes-watchdog.ps1" -ExtraArguments '-IncludedMinutes 3000'
    Assert-True (Test-ExpectedMaintenanceTask (New-FixtureTask 'MachineBootstrap-Skills-Update' $skillsArguments)) 'skills update task is an expected maintenance task'
    Assert-True (Test-ExpectedMaintenanceTask (New-FixtureTask 'MachineBootstrap-Minutes-Watchdog' $watchdogArguments)) 'minutes watchdog task is an expected maintenance task'
    Assert-Equal (Test-ExpectedMaintenanceTask (New-FixtureTask 'MachineBootstrap-Minutes-Watchdog' ($watchdogArguments + ' -Evil 1'))) $false 'extra watchdog arguments are drift'
    Assert-Equal (Test-ExpectedMaintenanceTask (New-FixtureTask 'MachineBootstrap-Skills-Update' $skillsArguments 'C:\Users\tester')) $false 'wrong working directory is drift'
    Assert-Equal (Test-ExpectedMaintenanceTask (New-FixtureTask 'MachineBootstrap-Skills-Update' ($skillsArguments + ' -Evil'))) $false 'extra skills arguments are drift'

    $xml = [xml](New-FounderTaskXml -Description 'Test & <check>' -ScriptPath "$directory\skills-update.ps1" -WorkingDirectory $directory -Account 'HOST\founder')
    $ns = [System.Xml.XmlNamespaceManager]::new($xml.NameTable)
    $ns.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
    Assert-Equal $xml.SelectSingleNode('//t:Principal/t:LogonType', $ns).InnerText 'InteractiveToken' 'task runs in the founder login session'
    Assert-Equal $xml.SelectSingleNode('//t:Principal/t:RunLevel', $ns).InnerText 'LeastPrivilege' 'task is not elevated'
    Assert-Equal $xml.SelectSingleNode('//t:ScheduleByDay/t:DaysInterval', $ns).InnerText '1' 'task is daily'
    Assert-Equal $xml.SelectSingleNode('//t:Exec/t:Arguments', $ns).InnerText $skillsArguments 'task arguments are the allowlisted ones'
    Assert-Equal $xml.SelectSingleNode('//t:RegistrationInfo/t:Description', $ns).InnerText 'Test & <check>' 'description is escaped'
}

# skills-update.ps1 reports a failed update.
& {
    . "$PSScriptRoot/../windows/skills-update.ps1"
    $log = Join-Path $env:TEMP ('bootstrap-skills-update-test-' + [guid]::NewGuid().ToString('N') + '.log')
    try {
        $LogPath = $log
        function Get-Command { param($Name, $CommandType, $ErrorAction); [pscustomobject]@{ Source = 'gh-fixture.exe' } }
        function gh-fixture.exe { $global:LASTEXITCODE = $script:ghExit; $script:ghOutput }
        $script:ghExit = 0; $script:ghOutput = 'Updated 1 skill'
        Assert-Equal (Invoke-SkillsUpdate) 0 'successful update returns 0'
        $script:ghExit = 1; $script:ghOutput = 'could not reach github.com'
        Assert-Equal (Invoke-SkillsUpdate) 1 'failed update returns 1'
        $script:ghExit = 0; $script:ghOutput = 'failed to update github-ci'
        Assert-Equal (Invoke-SkillsUpdate) 1 'reported failure returns 1 even with exit 0'
        Assert-True ((Get-Content -LiteralPath $log -Raw) -like '*ERROR*') 'failure is logged'
    } finally {
        if (Test-Path -LiteralPath $log) { Remove-Item -LiteralPath $log -Force }
    }
}
