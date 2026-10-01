[CmdletBinding()]
param([Parameter(Mandatory)][ValidatePattern('^[A-Za-z][A-Za-z0-9_-]{0,47}$')][string]$Name)
. "$PSScriptRoot/common.ps1"
function Install-WslCompactionTask {
    param([string]$Name)
    Assert-BootstrapAdministrator
    Get-CompactableWslDisk -Name $Name | Out-Null
    $directory = 'C:\ProgramData\machine-bootstrap'
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    # Task code must not be writable by unprivileged Windows users.
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in 'S-1-5-18', 'S-1-5-32-544') {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid), 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $directory -AclObject $acl
    foreach ($file in 'common.ps1', 'check-compaction.ps1', 'inventory.ps1') {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination (Join-Path $directory $file) -Force
    }

    $scriptPath = Join-Path $directory 'check-compaction.ps1'
    $logPath = Join-Path $directory 'compact.log'
    $powerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $account = [Security.SecurityElement]::Escape([Security.Principal.WindowsIdentity]::GetCurrent().Name)
    $scriptXml = [Security.SecurityElement]::Escape($scriptPath)
    $nameXml = [Security.SecurityElement]::Escape($Name)
    $logXml = [Security.SecurityElement]::Escape($logPath)
    $commandXml = [Security.SecurityElement]::Escape($powerShell)
    $startBoundary = (Get-Date).ToString('yyyy-MM-dd') + 'T03:30:00'
    $monthNodes = '<January/><February/><March/><April/><May/><June/><July/><August/><September/><October/><November/><December/>'
    $taskXml = @"
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Monthly WSL VHDX compaction recommendation check for $nameXml.</Description></RegistrationInfo>
  <Triggers><CalendarTrigger><StartBoundary>$startBoundary</StartBoundary><Enabled>true</Enabled>
    <ScheduleByMonth><DaysOfMonth><Day>1</Day></DaysOfMonth><Months>$monthNodes</Months></ScheduleByMonth>
  </CalendarTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>$account</UserId><LogonType>S4U</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <Enabled>true</Enabled><ExecutionTimeLimit>PT2H</ExecutionTimeLimit><Priority>7</Priority>
  </Settings>
  <Actions Context="Author"><Exec><Command>$commandXml</Command>
    <Arguments>-NoProfile -NonInteractive -File &quot;$scriptXml&quot; -Name &quot;$nameXml&quot; -LogPath &quot;$logXml&quot;</Arguments>
    <WorkingDirectory>$directory</WorkingDirectory>
  </Exec></Actions>
</Task>
"@
    $taskName = 'MachineBootstrap-Compact-' + $Name
    Register-ScheduledTask -TaskName $taskName -Xml $taskXml -Force | Out-Null
    Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
    Write-Output "Monthly recommendation check on day 1 at 03:30 local time registered for $Name. Log: $logPath"
}
if ($MyInvocation.InvocationName -ne '.') { Install-WslCompactionTask -Name $Name }
