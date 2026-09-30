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
    foreach ($file in 'common.ps1', 'compact-distro.ps1') {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination (Join-Path $directory $file) -Force
    }
    $scriptPath = Join-Path $directory 'compact-distro.ps1'
    $logPath = Join-Path $directory 'compact.log'
    $action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument ('-NoProfile -NonInteractive -File "' + $scriptPath + '" -Name "' + $Name + '" -IfIdle -LogPath "' + $logPath + '"')
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '03:30'
    $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType S4U -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    $taskName = 'MachineBootstrap-Compact-' + $Name
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
    Write-Output "Weekly Sunday 03:30 compaction registered for $Name. Log: $logPath"
}
if ($MyInvocation.InvocationName -ne '.') { Install-WslCompactionTask -Name $Name }
