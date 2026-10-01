[CmdletBinding()]
param()
. "$PSScriptRoot/common.ps1"
function Install-MachineInventoryTask {
 [CmdletBinding()]param()
 Assert-BootstrapAdministrator
 $directory='C:\ProgramData\machine-bootstrap'
 New-Item -ItemType Directory -Force -Path $directory|Out-Null
 $acl=[Security.AccessControl.DirectorySecurity]::new();$acl.SetAccessRuleProtection($true,$false)
 foreach($sid in 'S-1-5-18','S-1-5-32-544'){
  $rule=[Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','ContainerInherit,ObjectInherit','None','Allow')
  $acl.AddAccessRule($rule)
 }
 Set-Acl -LiteralPath $directory -AclObject $acl
 foreach($file in 'common.ps1','inventory.ps1'){Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination (Join-Path $directory $file) -Force}
 $inventoryDirectory=Join-Path $directory 'inventory';New-Item -ItemType Directory -Force -Path $inventoryDirectory|Out-Null
 $scriptPath=Join-Path $directory 'inventory.ps1';$outputDirectory=$inventoryDirectory
 $powerShell=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
 $account=[Security.SecurityElement]::Escape([Security.Principal.WindowsIdentity]::GetCurrent().Name)
 $scriptXml=[Security.SecurityElement]::Escape($scriptPath);$outputXml=[Security.SecurityElement]::Escape($outputDirectory);$commandXml=[Security.SecurityElement]::Escape($powerShell);$directoryXml=[Security.SecurityElement]::Escape($directory)
 $start=(Get-Date).ToString('yyyy-MM-dd')+'T02:30:00'
 $xml=@"
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
 <RegistrationInfo><Description>Nightly machine bootstrap inventory collection.</Description></RegistrationInfo>
 <Triggers><CalendarTrigger><StartBoundary>$start</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
 <Principals><Principal id="Author"><UserId>$account</UserId><LogonType>S4U</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
 <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><Enabled>true</Enabled><ExecutionTimeLimit>PT2H</ExecutionTimeLimit><Priority>7</Priority></Settings>
 <Actions Context="Author"><Exec><Command>$commandXml</Command><Arguments>-NoProfile -NonInteractive -File &quot;$scriptXml&quot; -OutputDirectory &quot;$outputXml&quot;</Arguments><WorkingDirectory>$directoryXml</WorkingDirectory></Exec></Actions>
</Task>
"@
 Register-ScheduledTask -TaskName 'MachineBootstrap-Inventory' -Xml $xml -Force|Out-Null
 Get-ScheduledTask -TaskName 'MachineBootstrap-Inventory'|Select-Object TaskName,State
 Write-Output "Nightly inventory collection at 02:30 local time registered. Output: $outputDirectory"
}
if($MyInvocation.InvocationName -ne '.') {Install-MachineInventoryTask}
