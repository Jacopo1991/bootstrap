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
    Assert-Equal (Get-ActionsMinutesUsed -Usage $usage -Month '2026-10') 2100.0 'only this month''s Actions minutes count, Windows at 2x (storage, other products and September ignored)'
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
            $value.usageItems[0].quantity = $script:used - 1100  # other October items: 500 Windows x2 + 100 Linux
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
        # Behaves like the real `gh skill`: the listing is "name<TAB>description" on stdout; installing
        # by name refuses with exit 1 when the skill is already there; --all refuses as soon as any
        # skill is present, so the script must never use it (the fixture fails the call with 64).
        function gh-fixture.exe {
            $script:ghCalls.Add(($args -join ' '))
            $global:LASTEXITCODE = 0
            if ($args[1] -eq 'install' -and $args.Count -eq 3) {
                $global:LASTEXITCODE = $script:listExit
                if ($script:listExit -eq 0) { $script:listing }
                return
            }
            if ($args[1] -eq 'install') {
                if (($args -contains '--all') -or ($args -contains '--force')) { $global:LASTEXITCODE = 64; return }
                $key = "$($args[3]) $($args[5])"
                if ($script:installed -contains $key) { $global:LASTEXITCODE = 1; "skills already installed: $($args[3]) (use --force to overwrite)"; return }
                if ($script:broken -contains $key) { $global:LASTEXITCODE = 2; "boom installing $($args[3])"; return }
                "Installed $($args[3])"
                return
            }
            $global:LASTEXITCODE = $script:ghExit
            $script:ghOutput
        }
        function Reset-Fixture {
            $script:ghCalls = [System.Collections.Generic.List[string]]::new()
            $script:ghExit = 0; $script:ghOutput = 'Updated 1 skill'; $script:listExit = 0
            $script:listing = @('Using ref v0.5.0 (146f526b)', "github-ci`tRules for GitHub Actions.", "research`tHow Jacopo wants research done.")
            $script:installed = @('github-ci claude-code', 'github-ci codex', 'research claude-code', 'research codex')
            $script:broken = @()
            if (Test-Path -LiteralPath $log) { Remove-Item -LiteralPath $log -Force }
        }
        Reset-Fixture
        Assert-Equal (Invoke-SkillsUpdate) 0 'successful update returns 0'
        Assert-Equal $script:ghCalls.Count (1 + 2 * 2 + 1) 'nothing new: listing, every skill for both agents, update'
        Assert-Equal $script:ghCalls[0] 'skill install Jacopo1991/cortex-core' 'the repository is listed first'
        Assert-Equal $script:ghCalls[$script:ghCalls.Count - 1] 'skill update --all' 'update --all runs last'
        Assert-True (-not (@($script:ghCalls | Where-Object { $_ -like '*--force*' -or ($_ -like '*--all*' -and $_ -ne 'skill update --all') }).Count)) 'install never uses --all or --force'
        Assert-True ((Get-Content -LiteralPath $log -Raw) -notlike '*Installed new skill*') 'nothing new is installed'

        Reset-Fixture
        $script:listing += "brand-new`tA skill added later."
        Assert-Equal (Invoke-SkillsUpdate) 0 'a new skill is installed and the run succeeds'
        $text = Get-Content -LiteralPath $log -Raw
        Assert-True ($text -like '*Installed new skill brand-new for claude-code.*') 'new skill is installed for claude-code'
        Assert-True ($text -like '*Installed new skill brand-new for codex.*') 'new skill is installed for codex'
        Assert-True ($script:ghCalls -contains 'skill install Jacopo1991/cortex-core brand-new --agent codex --scope user') 'install is by name, user scope, no --force'
        Assert-Equal $script:ghCalls[$script:ghCalls.Count - 1] 'skill update --all' 'update --all still runs last'

        Reset-Fixture
        $script:installed = @('github-ci claude-code', 'github-ci codex', 'research claude-code')
        Invoke-SkillsUpdate | Out-Null
        $text = Get-Content -LiteralPath $log -Raw
        Assert-True ($text -like '*Installed new skill research for codex.*' -and $text -notlike '*for claude-code*') 'a skill missing for one agent is installed for that agent only'

        Reset-Fixture
        $script:listing += "brand-new`tA skill added later."
        $script:broken = @('brand-new codex')
        Assert-Equal (Invoke-SkillsUpdate) 1 'a failed install returns 1'
        Assert-Equal $script:ghCalls[$script:ghCalls.Count - 1] 'skill update --all' 'update --all still runs after a failed install'
        Assert-True ((Get-Content -LiteralPath $log -Raw) -like '*ERROR installing new skill brand-new for codex*') 'failed install is logged'

        Reset-Fixture
        $script:listExit = 1
        Assert-Equal (Invoke-SkillsUpdate) 1 'a failed listing returns 1'
        Assert-Equal $script:ghCalls[$script:ghCalls.Count - 1] 'skill update --all' 'update --all still runs after a failed listing'

        Reset-Fixture
        $script:listing = @('Using ref v0.5.0 (146f526b)')
        Assert-Equal (Invoke-SkillsUpdate) 1 'an empty listing returns 1'

        Reset-Fixture
        $script:ghExit = 1; $script:ghOutput = 'could not reach github.com'
        Assert-Equal (Invoke-SkillsUpdate) 1 'failed update returns 1'
        Reset-Fixture
        $script:ghOutput = 'failed to update github-ci'
        Assert-Equal (Invoke-SkillsUpdate) 1 'reported failure returns 1 even with exit 0'
        Assert-True ((Get-Content -LiteralPath $log -Raw) -like '*ERROR*') 'failure is logged'
    } finally {
        if (Test-Path -LiteralPath $log) { Remove-Item -LiteralPath $log -Force }
    }
}

# job-ping.ps1: which URL is pinged, silent skips, and that a ping never fails or delays a job.
& {
    . "$PSScriptRoot/../windows/job-ping.ps1"
    $directory = Join-Path $env:TEMP ('bootstrap-job-ping-test-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    try {
        $file = Join-Path $directory 'pings.env'
        Assert-Equal (Get-JobPingUrl -Check 'backrest-backup' -PingsFile $file) $null 'no pings file: nothing to ping'
        Set-Content -LiteralPath $file -Value @('# comment', 'WINDOWS_SKILLS_UPDATE_URL=https://hc.example/skills', 'BACKREST_BACKUP_URL="https://hc.example/backup/"', 'BACKREST_BACKUP_URL_EXTRA=https://hc.example/other')
        Assert-Equal (Get-JobPingUrl -Check 'backrest-backup' -PingsFile $file) 'https://hc.example/backup' 'quotes and trailing slash are removed, a longer key is not the key'
        Assert-Equal (Get-JobPingUrl -Check 'windows-skills-update' -PingsFile $file) 'https://hc.example/skills' 'each check uses its own key'
        Assert-Equal (Get-JobPingUrl -Check 'windows-minutes-watchdog' -PingsFile $file) $null 'missing key is skipped'
        foreach ($check in 'Backrest-Backup', '-x', 'a b', 'a;b', '..\x', 'backrest_backup') {
            Assert-Equal (Get-JobPingUrl -Check $check -PingsFile $file) $null "bad check name '$check' is ignored"
        }
        foreach ($value in 'http://hc.example/a', 'hello', 'ftp://hc.example/a', 'https://hc.example/a b', 'https://hc.example/a"b', '') {
            Set-Content -LiteralPath $file -Value "BACKREST_BACKUP_URL=$value"
            Assert-Equal (Get-JobPingUrl -Check 'backrest-backup' -PingsFile $file) $null "value '$value' is skipped"
        }
        # A dead network and a missing file never throw and cost next to nothing.
        Set-Content -LiteralPath $file -Value 'BACKREST_BACKUP_URL=https://127.0.0.1:1/ping'
        $watch = [Diagnostics.Stopwatch]::StartNew()
        Send-JobPing -Check 'backrest-backup' -Success $false -PingsFile $file -TimeoutSeconds 2
        Send-JobPing -Check 'backrest-backup' -Success $true -PingsFile (Join-Path $directory 'missing.env')
        Assert-True ($watch.Elapsed.TotalSeconds -lt 10) 'an unreachable server does not delay the job'
        # Run as a script (the Backrest hook form): always exit 0, even for a bad check name.
        $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        & $powershell -NoProfile -NonInteractive -File "$PSScriptRoot\..\windows\job-ping.ps1" -Check 'backrest-backup' -Result fail -PingsFile $file | Out-Null
        Assert-Equal $LASTEXITCODE 0 'script form exits 0 when the ping fails'
        & $powershell -NoProfile -NonInteractive -File "$PSScriptRoot\..\windows\job-ping.ps1" -Check 'NOT A CHECK' -Result ok -PingsFile $file | Out-Null
        Assert-Equal $LASTEXITCODE 0 'script form exits 0 for a bad check name'
    } finally {
        if (Test-Path -LiteralPath $directory) { Remove-Item -LiteralPath $directory -Recurse -Force }
    }
}
