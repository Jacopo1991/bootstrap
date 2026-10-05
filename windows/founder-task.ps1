Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Shared by install-skills-task.ps1 and install-minutes-watchdog-task.ps1: scheduled tasks that
# run as the founder with the founder's own login (they need the founder's gh login and desktop
# notifications), unlike the S4U inventory task. The scripts live in a protected folder that only
# an elevated token can change; the limited token the task runs with can read and run them.

$script:FounderTaskDirectory = 'C:\ProgramData\machine-bootstrap\founder'

function Initialize-FounderTaskDirectory {
    param([string]$Directory = $script:FounderTaskDirectory)
    New-Item -ItemType Directory -Force -Path $Directory | Out-Null
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in 'S-1-5-18', 'S-1-5-32-544') {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid), 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    }
    # A non-elevated token has Administrators as deny-only, so the owner account needs its own read rule.
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.WindowsIdentity]::GetCurrent().User, 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    Set-Acl -LiteralPath $Directory -AclObject $acl
}

function Get-FounderTaskArguments {
    param([Parameter(Mandatory)][string]$ScriptPath, [string]$ExtraArguments = '')
    $text = '-NoProfile -NonInteractive -File "' + $ScriptPath + '"'
    if ($ExtraArguments) { $text += ' ' + $ExtraArguments }
    return $text
}

function New-FounderTaskXml {
    param([Parameter(Mandatory)][string]$Description, [Parameter(Mandatory)][string]$ScriptPath,
          [string]$ExtraArguments = '', [Parameter(Mandatory)][string]$WorkingDirectory,
          [Parameter(Mandatory)][string]$Account, [string]$Time = '09:30')
    $powerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $escape = { param($value) [Security.SecurityElement]::Escape($value) }
    $start = (Get-Date).ToString('yyyy-MM-dd') + 'T' + $Time + ':00'
    $arguments = & $escape (Get-FounderTaskArguments -ScriptPath $ScriptPath -ExtraArguments $ExtraArguments)
    $command = & $escape $powerShell
    $directory = & $escape $WorkingDirectory
    $user = & $escape $Account
    $text = & $escape $Description
    # StartWhenAvailable runs a missed daily start (machine off or logged out) at the next login.
    return @"
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
 <RegistrationInfo><Description>$text</Description></RegistrationInfo>
 <Triggers><CalendarTrigger><StartBoundary>$start</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
 <Principals><Principal id="Author"><UserId>$user</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
 <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><Enabled>true</Enabled><ExecutionTimeLimit>PT10M</ExecutionTimeLimit><Priority>7</Priority></Settings>
 <Actions Context="Author"><Exec><Command>$command</Command><Arguments>$arguments</Arguments><WorkingDirectory>$directory</WorkingDirectory></Exec></Actions>
</Task>
"@
}

function Register-FounderTask {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$Description,
          [Parameter(Mandatory)][string]$ScriptFile, [string]$ExtraArguments = '', [string]$Time = '09:30')
    $directory = $script:FounderTaskDirectory
    $account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $xml = New-FounderTaskXml -Description $Description -ScriptPath (Join-Path $directory $ScriptFile) `
        -ExtraArguments $ExtraArguments -WorkingDirectory $directory -Account $account -Time $Time
    Register-ScheduledTask -TaskName $Name -Xml $xml -Force | Out-Null
    Get-ScheduledTask -TaskName $Name | Select-Object TaskName, State
}
