Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) {
    throw 'These regression tests must run in Windows PowerShell 5.1.'
}
. "$PSScriptRoot/../windows/common.ps1"

function Assert-Equal {
    param($Actual, $Expected, [string]$Label)
    if ($Actual -cne $Expected) { throw "$Label`: expected '$Expected', got '$Actual'." }
    Write-Output "PASS: $Label"
}
Assert-Equal (ConvertTo-WslBasePath '\\?\D:\wsl\X') 'D:\wsl\X' 'extended drive path'
Assert-Equal (ConvertTo-WslBasePath 'D:\wsl\X') 'D:\wsl\X' 'plain drive path'
Assert-Equal (ConvertTo-WslBasePath '%LOCALAPPDATA%\X') ([IO.Path]::GetFullPath($env:LOCALAPPDATA + '\X')) 'environment expansion'
Assert-Equal (ConvertTo-WslBasePath '\\?\UNC\srv\share\X') '\\srv\share\X' 'extended UNC path'
Assert-Equal (@(ConvertTo-WslBasePath $null).Count) 0 'null path skipped'

# Registry fixtures exercise missing BasePath under strict mode without host changes.
function Test-Path {
    param([string]$LiteralPath)
    return $LiteralPath -eq 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss'
}
function Get-ChildItem {
    param([string]$LiteralPath)
    if ($LiteralPath -ne 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss') { throw 'Unexpected registry query.' }
    foreach ($name in 'missing', 'null', 'valid') { [pscustomobject]@{PSPath=$name} }
}
function Get-ItemProperty {
    param([string]$LiteralPath)
    switch ($LiteralPath) {
        'missing' { [pscustomobject]@{DistributionName='Missing'; Version=2} }
        'null' { [pscustomobject]@{DistributionName='Null'; Version=2; BasePath=$null} }
        'valid' { [pscustomobject]@{DistributionName='Valid'; Version=2; BasePath='\\?\C:\wsl\X'} }
        default { throw 'Unexpected registry entry.' }
    }
}
$results = @(Get-WslDisks 3>&1)
$warnings = @($results | Where-Object { $_ -is [System.Management.Automation.WarningRecord] })
$disks = @($results | Where-Object { $_ -isnot [System.Management.Automation.WarningRecord] })
Assert-Equal $warnings.Count 2 'missing and null BasePath warn'
Assert-Equal $disks.Count 1 'missing and null registry entries skipped'
Assert-Equal $disks[0].BasePath 'C:\wsl\X' 'registry BasePath normalized'
Assert-Equal $disks[0].Vhdx 'C:\wsl\X\ext4.vhdx' 'normalized VHDX path'
Remove-Item function:\Test-Path, function:\Get-ChildItem, function:\Get-ItemProperty

function Assert-Throws {
    param([scriptblock]$Action, [string]$Pattern, [string]$Label)
    $errorMessage = $null
    try { & $Action | Out-Null } catch { $errorMessage=$_.Exception.Message }
    if ($null -eq $errorMessage -or $errorMessage -notlike $Pattern) { throw "$Label`: expected $Pattern, got $errorMessage" }
    Write-Output "PASS: $Label"
}

$fixtureDirectory = Join-Path $env:TEMP ('bootstrap-tests-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($fixtureDirectory) | Out-Null
try {
    $configFile = Join-Path $fixtureDirectory '.wslconfig'
    $original = "; keep this comment`r`n[wsl2]`r`nmemory=8GB`r`ndefaultVhdSize=1000GB`r`n[experimental]`r`nsparseVhd=false`r`n"
    [IO.File]::WriteAllText($configFile, $original)
    Set-WslVhdCap -MaxSizeGB 800 -Path $configFile | Out-Null
    $merged = [IO.File]::ReadAllText($configFile)
    Assert-Equal ($merged.Contains('memory=8GB') -and $merged.Contains('; keep this comment')) $true 'merge preserves unrelated settings'
    Assert-Equal ($merged.Contains('defaultVhdSize=800GB')) $true 'cap merged into wsl2'
    $backups = @(Get-ChildItem -LiteralPath $fixtureDirectory -Filter '*.bak')
    Assert-Equal $backups.Count 1 'existing config backed up once'
    Assert-Equal ([IO.File]::ReadAllText($backups[0].FullName)) $original 'backup retains exact source'
    Set-WslVhdCap -MaxSizeGB 800 -Path $configFile | Out-Null
    Assert-Equal ([IO.File]::ReadAllText($configFile)) $merged 'repeat merge unchanged'
    Assert-Equal (@(Get-ChildItem -LiteralPath $fixtureDirectory -Filter '*.bak').Count) 1 'repeat merge creates no backup'
    Assert-Equal ((Merge-WslVhdCap -Text '[experimental]' -MaxSizeGB 900).Contains("[wsl2]`r`ndefaultVhdSize=900GB")) $true 'missing wsl2 section added'
    Assert-Throws { Merge-WslVhdCap -Text "[experimental]`nsparseVhd=true" -MaxSizeGB 800 } '*enables sparseVhd*' 'unsafe global sparse opt-in refused'
} finally { Remove-Item -LiteralPath $fixtureDirectory -Recurse -Force }

# Run the actual creation script with WSL, registry and file operations mocked.
foreach ($version in 2, 1) {
    & {
        param([int]$Version)
        $testState = @{Installed=$false; Version=$Version; Events=[System.Collections.Generic.List[string]]::new()}
        $testProfile = Join-Path $env:TEMP ('bootstrap-profile-' + [Guid]::NewGuid().ToString('N'))
        [IO.Directory]::CreateDirectory($testProfile) | Out-Null
        $originalProfile = $env:USERPROFILE
        $env:USERPROFILE = $testProfile
        function wsl.exe {
            $global:LASTEXITCODE = 0
            switch ($args[0]) {
                '--version' { 'WSL version: 3.0.1'; return }
                '--help' { '--from-file --location --name'; return }
                '--install' {
                    Assert-Equal ([IO.File]::ReadAllText((Join-Path $testProfile '.wslconfig')).Contains('defaultVhdSize=800GB')) $true 'cap set before install'
                    $testState.Installed = $true; $testState.Events.Add('install')
                }
                '--set-version' {
                    if ($testState.Version -eq 2) { throw 'Redundant version conversion must not run.' }
                    Assert-Equal ($args -join ' ') '--set-version BootstrapTest 2' 'conversion arguments'
                    $testState.Events.Add('set-version')
                    $testState.Version = 2
                }
                '--terminate' {
                    Assert-Equal ($args -join ' ') '--terminate BootstrapTest' 'terminate arguments'
                    $testState.Events.Add('terminate')
                }
                '-d' {
                    Assert-Equal ($args -join ' ') '-d BootstrapTest -u root -- df -B1 --output=size,used /' 'cap read-back command'
                    $testState.Events.Add('cap')
                    'Size Used'; '848256040960 1073741824'
                }
                '--manage' { throw 'Sparse mode must never be enabled.' }
                default { throw 'Unexpected WSL call.' }
            }
        }
        function Test-Path {
            param([string]$LiteralPath, $PathType)
            return $LiteralPath -eq 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' -or
                $LiteralPath -eq 'D:\' -or $LiteralPath.EndsWith('ubuntu-24.04.5-wsl-amd64.wsl') -or
                ($LiteralPath.EndsWith('ext4.vhdx') -and $PathType -eq 'Leaf') -or [IO.File]::Exists($LiteralPath)
        }
        function Get-Item { param($LiteralPath) [pscustomobject]@{Length=20GB; Attributes=[IO.FileAttributes]::Normal} }
        function Get-ChildItem {
            param([string]$LiteralPath)
            if ($LiteralPath -ne 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss') { throw 'Unexpected registry query.' }
            if ($testState.Installed) { [pscustomobject]@{PSPath='created'} }
        }
        function Get-ItemProperty {
            param([string]$LiteralPath)
            if ($LiteralPath -ne 'created') { throw 'Unexpected registry entry.' }
            [pscustomobject]@{DistributionName='BootstrapTest'; Version=$testState.Version; BasePath='D:\wsl\BootstrapTest'}
        }
        function New-Item { param($ItemType, [switch]$Force, $Path) }
        function Get-FileHash {
            param($Algorithm, $LiteralPath)
            [pscustomobject]@{Hash='bb415d824822c4b878125729af451a5d18fb13d1cf5cbed9a7393ad64ac6039e'}
        }
        function Invoke-WebRequest { throw 'Unexpected download.' }
        function Set-Content {
            param($LiteralPath, $Value, $Encoding, [switch]$NoNewline)
            Assert-Equal $LiteralPath 'D:\wsl\BootstrapTest\.bootstrap-image.sha256' 'marker path'
            Assert-Equal $Value 'bb415d824822c4b878125729af451a5d18fb13d1cf5cbed9a7393ad64ac6039e' 'marker hash'
            $testState.Events.Add('marker')
        }
        try {
            & "$PSScriptRoot/../windows/new-distro.ps1" -Name BootstrapTest
            $expectedEvents = if ($Version -eq 2) { 'install,marker,cap,terminate' } else { 'install,set-version,marker,cap,terminate' }
            Assert-Equal ($testState.Events -join ',') $expectedEvents "WSL $Version conversion decision and capped continuation"
        } finally {
            $env:USERPROFILE = $originalProfile
            [IO.Directory]::Delete($testProfile, $true)
        }
    } $version
}

& {
    function Get-WslFilesystemUsage { param($Name) [pscustomobject]@{CapBytes=801GB; UsedBytes=1GB} }
    Assert-Throws { Assert-WslCap -Name Test -MaxSizeGB 800 } '*exceeds*cap*' 'oversized filesystem refused'
}

# Real compaction functions; only host interactions are mocked.
foreach ($scenario in 'missing', 'wsl1', 'sparse', 'file-missing', 'non-ascii', 'other-idle', 'other-manual', 'list-error', 'malformed-list', 'busy', 'became-busy', 'idle', 'lock-timeout', 'diskpart-error') {
    & {
        param($Scenario)
        $state = @{Events=[System.Collections.Generic.List[string]]::new(); Probe=0; Bytes=20GB; Attached=$false; Scenario=$Scenario; Waited=$false; Listed=$false}
        function Assert-BootstrapAdministrator {}
        function Get-WslDisks {
            if ($state.Scenario -eq 'missing') { return }
            [pscustomobject]@{Name='Test'; WslVersion=$(if ($state.Scenario -eq 'wsl1') {1} else {2}); IsSparse=($state.Scenario -eq 'sparse'); Vhdx=$(if ($state.Scenario -eq 'non-ascii') {'C:\mock\'+[char]0x00e9+'\ext4.vhdx'} else {'C:\mock\ext4.vhdx'})}
        }
        function Test-Path { param($LiteralPath, $PathType) return $state.Scenario -ne 'file-missing' }
        function Get-Item { param($LiteralPath) [pscustomobject]@{Length=$state.Bytes} }
        function Get-DiskImage { param($ImagePath) [pscustomobject]@{Attached=$state.Attached} }
        function Wait-WslVhdDetached {
            param($Vhdx)
            if ($state.Scenario -eq 'lock-timeout') { throw 'Timed out waiting for exclusive read access.' }
            $state.Waited=$true
        }
        function Start-Sleep { param($Seconds) Assert-Equal $Seconds 15 'diskpart retry shutdown interval' | Out-Null }
        function wsl.exe {
            $global:LASTEXITCODE = 0
            if (($args -join ' ') -eq '--list --verbose') {
                $state.Listed=$true
                if ($state.Scenario -eq 'list-error') { $global:LASTEXITCODE=1; return }
                $listing = "  NAME STATE VERSION`n* Test Running 2`n  Sleeping Stopped 2"
                if ($state.Scenario -eq 'malformed-list') { $listing += "`n  Other ???" }
                if ($state.Scenario -in 'other-idle', 'other-manual') { $listing += "`n  Other Distro Running 2`n  Legacy Running 1" }
                return ($listing.ToCharArray() -join [string][char]0)
            }
            Assert-Equal $args[1] 'Test' 'only selected distro touched' | Out-Null
            if ($args[0] -eq '--terminate') { $state.Events.Add('terminate'); return }
            if ($args[5] -eq 'python3') {
                $state.Probe++; $state.Events.Add('probe')
                if ($state.Scenario -eq 'busy' -or ($state.Scenario -eq 'became-busy' -and $state.Probe -eq 2)) { 'BUSY' } else { 'IDLE' }
                return
            }
            if ($args[5] -ne 'fstrim' -or $args[-1] -ne '/') { throw 'Unexpected WSL call.' }
            $state.Events.Add('trim')
        }
        function diskpart.exe {
            $global:LASTEXITCODE=0
            Assert-Equal $state.Waited $true 'exclusive-open wait precedes diskpart'
            Assert-Equal $args[0] '/s' 'diskpart scripted invocation'
            $bytes = [IO.File]::ReadAllBytes($args[1])
            Assert-Equal (@($bytes | Where-Object { $_ -gt 127 -or $_ -eq 0 }).Count) 0 'diskpart script is ASCII without BOM or UTF-16 NULs'
            $commands = [IO.File]::ReadAllText($args[1])
            Assert-Equal ($commands.StartsWith('select vdisk file="C:\mock\ext4.vhdx"')) $true 'diskpart selects only target file'
            if ($commands.Contains('compact vdisk')) {
                Assert-Equal $commands "select vdisk file=`"C:\mock\ext4.vhdx`"`r`nattach vdisk readonly`r`ncompact vdisk`r`ndetach vdisk`r`n" 'readonly compact sequence'
                $state.Events.Add('diskpart')
                if ($state.Scenario -eq 'diskpart-error') { $state.Attached=$true; $global:LASTEXITCODE=1; return }
                $state.Bytes=15GB
            } else {
                Assert-Equal $commands "select vdisk file=`"C:\mock\ext4.vhdx`"`r`ndetach vdisk`r`n" 'failure cleanup detaches target'
                $state.Events.Add('detach-cleanup'); $state.Attached=$false
            }
        }
        if ($Scenario -in 'missing', 'wsl1') {
            Assert-Throws { Invoke-WslCompaction -Name Test -IfIdle } '*existing WSL 2*' "$Scenario refused"
            Assert-Equal $state.Events.Count 0 'refusal has no host actions'
        } elseif ($Scenario -eq 'sparse') {
            Assert-Throws { Invoke-WslCompaction -Name Test -IfIdle } '*sparse*' 'sparse disk refused'
            Assert-Equal $state.Events.Count 0 'sparse refusal has no host actions'
        } elseif ($Scenario -eq 'file-missing') {
            Assert-Throws { Invoke-WslCompaction -Name Test -IfIdle } '*VHDX is missing*' 'missing file refused'
        } elseif ($Scenario -eq 'non-ascii') {
            Assert-Throws { Invoke-WslCompaction -Name Test -IfIdle } '*non-ASCII*' 'non-ASCII VHDX path refused'
            Assert-Equal $state.Events.Count 0 'non-ASCII refusal has no WSL or diskpart actions'
        } elseif ($Scenario -eq 'other-idle') {
            Assert-Equal ((Invoke-WslCompaction -Name Test -IfIdle) -join '') 'SKIP: WSL VM held by Other Distro, Legacy' 'other running distros cause an idle skip'
            Assert-Equal $state.Events.Count 0 'other-distro skip performs no trim, probe, terminate or diskpart'
        } elseif ($Scenario -eq 'other-manual') {
            Assert-Throws { Invoke-WslCompaction -Name Test } '*WSL VM held by Other Distro, Legacy*' 'manual compaction names other running distros'
            Assert-Equal $state.Events.Count 0 'other-distro refusal performs no trim, terminate or diskpart'
        } elseif ($Scenario -eq 'list-error') {
            Assert-Throws { Invoke-WslCompaction -Name Test -IfIdle } '*wsl.exe failed*' 'failed distro listing blocks compaction'
            Assert-Equal $state.Events.Count 0 'listing failure performs no trim or terminate'
        } elseif ($Scenario -eq 'malformed-list') {
            Assert-Throws { Invoke-WslCompaction -Name Test -IfIdle } '*Could not parse*' 'unrecognized distro listing blocks compaction'
            Assert-Equal $state.Events.Count 0 'malformed listing performs no trim or terminate'
        } elseif ($Scenario -eq 'lock-timeout') {
            Assert-Throws { Invoke-WslCompaction -Name Test -IfIdle } '*Timed out*' 'exclusive-open timeout blocks diskpart'
            Assert-Equal ($state.Events -join ',') 'probe,trim,probe,terminate' 'locked VHDX never reaches diskpart'
        } elseif ($Scenario -eq 'diskpart-error') {
            Assert-Throws { Invoke-WslCompaction -Name Test -IfIdle } '*diskpart failed*' 'diskpart error propagated'
            Assert-Equal ($state.Events -join ',') 'probe,trim,probe,terminate,diskpart,detach-cleanup' 'failure detaches own disk'
        } else {
            Invoke-WslCompaction -Name Test -IfIdle
            $expected = switch ($Scenario) { 'busy' {'probe'} 'became-busy' {'probe,trim,probe'} 'idle' {'probe,trim,probe,terminate,diskpart'} }
            Assert-Equal ($state.Events -join ',') $expected "$Scenario compaction sequence"
        }
    } $scenario
}
& {
    $lockPath = Join-Path $env:TEMP ('bootstrap-lock-' + [Guid]::NewGuid().ToString('N') + '.vhdx')
    [IO.File]::WriteAllBytes($lockPath, [byte[]]@(1,2,3))
    $lockState = @{Held=$null; Sleeps=0; Release=$true}
    function Start-Sleep {
        param($Seconds)
        $lockState.Sleeps++
        if ($lockState.Release) { $lockState.Held.Dispose() }
    }
    function Get-DiskImage { throw 'DiskImage attachment state must not decide whether the VHDX is unlocked.' }
    try {
        $lockState.Held = [IO.File]::Open($lockPath, 'Open', 'Read', 'None')
        Wait-WslVhdDetached -Vhdx $lockPath
        Assert-Equal $lockState.Sleeps 1 'exclusive-open wait polls until the held file is released'
        $lockState.Release=$false
        # This succeeds only if the wait closed its successful exclusive open.
        $lockState.Held = [IO.File]::Open($lockPath, 'Open', 'Read', 'None')
        Assert-Throws { Wait-WslVhdDetached -Vhdx $lockPath -TimeoutSeconds 0 } '*Timed out*' 'locked-file wait is bounded'
        $timeout = (Get-Command Wait-WslVhdDetached).ScriptBlock.Ast.Find({ param($node) $node -is [Management.Automation.Language.ParameterAst] -and $node.Name.VariablePath.UserPath -eq 'TimeoutSeconds' }, $true)
        Assert-Equal $timeout.DefaultValue.Value 300 'exclusive-open wait defaults to the 300-second VM idle window'
    } finally {
        if ($null -ne $lockState.Held) { $lockState.Held.Dispose() }
        Remove-Item -LiteralPath $lockPath -Force
    }
}

& {
    . "$PSScriptRoot/../windows/install-compaction-task.ps1" -Name Test
    $tasks = @{}
    function Assert-BootstrapAdministrator {}
    function Get-CompactableWslDisk { param($Name) [pscustomobject]@{Name=$Name} }
    function New-Item { param($ItemType, [switch]$Force, $Path) }
    function Set-Acl { param($LiteralPath, $AclObject) Assert-Equal $AclObject.AreAccessRulesProtected $true 'task code ACL protected' }
    function Copy-Item { param($LiteralPath, $Destination, [switch]$Force) }
    function New-ScheduledTaskAction {
        param($Execute, $Argument)
        Assert-Equal ($Execute.EndsWith('\powershell.exe')) $true 'task uses Windows PowerShell'
        Assert-Equal $Argument '-NoProfile -NonInteractive -File "C:\ProgramData\machine-bootstrap\compact-distro.ps1" -Name "Test" -IfIdle -LogPath "C:\ProgramData\machine-bootstrap\compact.log"' 'task idle mode and stable logging'
        [pscustomobject]@{Execute=$Execute; Arguments=$Argument}
    }
    function New-ScheduledTaskTrigger {
        param([switch]$Weekly, $DaysOfWeek, $At)
        Assert-Equal ([bool]$Weekly -and $DaysOfWeek -eq 'Sunday' -and $At -eq '03:30') $true 'weekly Sunday 0330 trigger'
        [pscustomobject]@{At=$At}
    }
    function New-ScheduledTaskPrincipal {
        param($UserId, $LogonType, $RunLevel)
        Assert-Equal $UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) 'task runs as distro owner'
        Assert-Equal "$LogonType/$RunLevel" 'S4U/Highest' 'highest privileges without stored password'
        [pscustomobject]@{UserId=$UserId}
    }
    function New-ScheduledTaskSettingsSet { param($MultipleInstances, $ExecutionTimeLimit) [pscustomobject]@{MultipleInstances=$MultipleInstances} }
    function Register-ScheduledTask {
        param($TaskName, $Action, $Trigger, $Principal, $Settings, [switch]$Force)
        Assert-Equal ([bool]$Force) $true 'task registration converges'
        $tasks[$TaskName]=[pscustomobject]@{TaskName=$TaskName; State='Ready'}
    }
    function Get-ScheduledTask { param($TaskName) $tasks[$TaskName] }
    Install-WslCompactionTask -Name Test
    Install-WslCompactionTask -Name Test
    Assert-Equal $tasks.Count 1 'repeat installation retains one task'
}

& {
    $reportState = @{Calls=[System.Collections.Generic.List[string]]::new(); Rows=[System.Collections.Generic.List[object]]::new()}
    function wsl.exe {
        $global:LASTEXITCODE=0
        $reportState.Calls.Add(($args -join ' '))
        if (($args -join ' ') -eq '--list --verbose') {
            # WSL outputs UTF-16 with embedded NULs under Windows PowerShell 5.1.
            $listing = "  NAME           STATE      VERSION`n  Sleeping       Stopped    2`n* Active         Running    2`n  Legacy         Running    1"
            return ($listing.ToCharArray() -join [string][char]0)
        }
        if (($args -join ' ') -ne '-d Active -u root -- df -B1 --output=size,used /') { throw 'Reporter attempted to start a stopped or WSL 1 distro.' }
        'Size Used'; '848256040960 1073741824'
    }
    function Test-Path { param($LiteralPath) return $LiteralPath -eq 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' }
    function Get-ChildItem {
        param($LiteralPath)
        foreach ($name in 'Sleeping', 'Active', 'Legacy') { [pscustomobject]@{PSPath=$name} }
    }
    function Get-ItemProperty {
        param($LiteralPath)
        [pscustomobject]@{DistributionName=$LiteralPath; Version=$(if ($LiteralPath -eq 'Legacy') {1} else {2}); BasePath=('C:\mock\'+$LiteralPath)}
    }
    function Format-Table {
        param([switch]$AutoSize, [Parameter(ValueFromPipeline)]$InputObject)
        process { $reportState.Rows.Add($InputObject) }
    }
    & "$PSScriptRoot/../windows/vhdx-size.ps1"
    Assert-Equal ($reportState.Calls -join ',') '--list --verbose,-d Active -u root -- df -B1 --output=size,used /' 'only running WSL 2 queried; stopped distro gets no -d call'
    Assert-Equal $reportState.Rows.Count 3 'all registry distros remain in report'
    $stopped = @($reportState.Rows | Where-Object Name -eq 'Sleeping')[0]
    $running = @($reportState.Rows | Where-Object Name -eq 'Active')[0]
    $legacy = @($reportState.Rows | Where-Object Name -eq 'Legacy')[0]
    Assert-Equal $stopped.UsedBytes $null 'stopped used bytes empty'
    Assert-Equal $stopped.CapBytes $null 'stopped cap bytes empty'
    Assert-Equal $running.UsedBytes 1GB 'running used bytes reported'
    Assert-Equal $running.CapBytes 790GB 'running cap bytes reported'
    Assert-Equal $legacy.UsedBytes $null 'WSL 1 usage remains empty'
}

# Exercise the owner-side PE probe without launching WSL or a Windows PE.
& {
    . "$PSScriptRoot/../checks/compact-check.ps1" -Name Test
    $checkState = @{Calls=0}
    function Assert-BootstrapAdministrator {}
    function Get-CompactableWslDisk { param($Name) [pscustomobject]@{Vhdx='C:\mock\ext4.vhdx'} }
    function Assert-WslCap { throw 'compact-check queried the target before refusing other running distros.' }
    function wsl.exe {
        if (($args -join ' ') -ne '--list --verbose') { throw 'compact-check started or wrote to a distro before refusing.' }
        $checkState.Calls++
        $global:LASTEXITCODE=0
        "  NAME STATE VERSION`n* Test Running 2`n  Other Distro Running 2"
    }
    Assert-Throws { Invoke-WslCompactCheck -Name Test } '*other WSL distros are running: Other Distro*' 'compact-check refuses other running distros before writing'
    Assert-Equal $checkState.Calls 1 'compact-check only lists distros on refusal'
}

foreach ($case in 'blocked', 'success', 'output', 'timeout', 'copy-error') {
    & {
        . "$PSScriptRoot/../windows/interop-probe.ps1"
        $probeState = @{Case=$case; Paths=@{}; Removed=[System.Collections.Generic.List[string]]::new(); Killed=$false; Disposed=$false}
        function Test-Path { param($LiteralPath) return $LiteralPath -eq '\\wsl.localhost\Test\var\tmp' -or $probeState.Paths.ContainsKey($LiteralPath) }
        function Copy-Item {
            param($LiteralPath, $Destination)
            Assert-Equal $LiteralPath (Join-Path $env:SystemRoot 'System32\whoami.exe') 'probe copies the host whoami executable'
            $probeState.Paths[$Destination]=$true
            if ($probeState.Case -eq 'copy-error') { throw 'mock copy failed' }
        }
        function wsl.exe {
            $command = $args -join ' '
            if ($command -notmatch '^-d Test -u root chmod 755 -- /var/tmp/interop-probe-[a-f0-9]+\.exe$' -and
                $command -notmatch '^-d Test -u agent test -f (/var/tmp/interop-probe-[a-f0-9]+\.exe) -a -x \1$') { throw ("Unexpected probe WSL call: " + $command) }
            $global:LASTEXITCODE=0
        }
        function Start-Process {
            param($FilePath, $ArgumentList, $WindowStyle, [switch]$PassThru, $RedirectStandardOutput, $RedirectStandardError)
            Assert-Equal "$FilePath/$WindowStyle/$PassThru" 'wsl.exe/Hidden/True' 'probe launches a hidden WSL process' | Out-Null
            Assert-Equal ($ArgumentList -match '^-d Test -u agent -- bash -lc ') $true 'probe runs in the agent login shell' | Out-Null
            [IO.File]::WriteAllText($RedirectStandardOutput, $(if ($probeState.Case -eq 'output') {'mock Windows output'} else {''}))
            [IO.File]::WriteAllText($RedirectStandardError, 'mock blocked-launch diagnostic')
            foreach ($path in $RedirectStandardOutput, $RedirectStandardError) { $probeState.Paths[$path]=$true }
            $process = [pscustomobject]@{ExitCode=$(if ($probeState.Case -eq 'success') {0} else {1})}
            $process | Add-Member ScriptMethod WaitForExit { param($Milliseconds) return $probeState.Case -ne 'timeout' }
            $process | Add-Member ScriptMethod Kill { $probeState.Killed=$true }
            $process | Add-Member ScriptMethod Dispose { $probeState.Disposed=$true }
            return $process
        }
        function Remove-Item {
            param($LiteralPath, [switch]$Force, $ErrorAction)
            $probeState.Removed.Add($LiteralPath)
            $probeState.Paths.Remove($LiteralPath) | Out-Null
            if (-not $LiteralPath.StartsWith('\\wsl.localhost\')) { Microsoft.PowerShell.Management\Remove-Item -LiteralPath $LiteralPath -Force -ErrorAction SilentlyContinue }
        }
        switch ($case) {
            blocked { Invoke-InteropProbe -Name Test }
            success { Assert-Throws { Invoke-InteropProbe -Name Test } '*ran successfully*' 'successful Windows launch rejected' }
            output { Assert-Throws { Invoke-InteropProbe -Name Test } '*produced stdout*' 'Windows output rejected even with nonzero exit' }
            timeout { Assert-Throws { Invoke-InteropProbe -Name Test } '*Timed out*' 'timeout rejected as inconclusive' }
            copy-error { Assert-Throws { Invoke-InteropProbe -Name Test } '*mock copy failed*' 'copy failure reported' }
        }
        Assert-Equal $probeState.Removed.Count 3 "$case removes executable and both output files"
        Assert-Equal $probeState.Paths.Count 0 "$case leaves no probe files"
        if ($case -ne 'copy-error') { Assert-Equal $probeState.Disposed $true "$case disposes the process" }
        Assert-Equal $probeState.Killed ($case -eq 'timeout') "$case kills only a timed-out process"
    }
}
