Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1){throw 'Inventory tests require Windows PowerShell 5.1.'}
. "$PSScriptRoot/../windows/inventory.ps1"
function Assert-Equal {param($Actual,$Expected,[string]$Label);if($Actual -cne $Expected){throw "$Label expected '$Expected', got '$Actual'."};Write-Output "PASS: $Label"}
function Assert-True {param([bool]$Value,[string]$Label);if(-not $Value){throw "FAIL: $Label"};Write-Output "PASS: $Label"}
Assert-Equal (ConvertTo-WindowsArgument 'plain') 'plain' 'plain argument'
Assert-Equal (ConvertTo-WindowsArgument 'name with spaces') '"name with spaces"' 'spaces quoted'
Assert-Equal (ConvertTo-WindowsArgument 'a"b') '"a\"b"' 'embedded quote escaped'
Assert-Equal (ConvertTo-WindowsArgument 'C:\ends with space\') '"C:\ends with space\\"' 'quoted trailing slash'

$listing='  NAME           STATE      VERSION'+[Environment]::NewLine+'* Active         Running    2'+[Environment]::NewLine+'  Sleeping       Stopped    2'
$parsed=@(ConvertFrom-WslList $listing)
Assert-Equal $parsed.Count 2 'WSL list parsed'
Assert-Equal $parsed[1].State 'Stopped' 'stopped state parsed'
Assert-Equal (@(ConvertFrom-WslList ($listing.ToCharArray() -join [string][char]0)).Count) 2 'NUL-separated WSL list parsed'
$fixture=Join-Path $env:TEMP ('bootstrap-inventory-tests-'+[guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($fixture)|Out-Null
try {
 function Write-Record {
  param([string]$Path,[string]$Name='Test',[string]$Vhdx='C:\mock\ext4.vhdx',[long]$Used=100,[Nullable[long]]$Cap=800,[string]$At='2026-09-30T12:00:00+02:00')
  $d=[pscustomobject]@{Name=$Name;Vhdx=$Vhdx;usedBytes=$Used;capBytes=$Cap;largestDirectories=@();usageRecordedAtISO=$At}
  $r=[pscustomobject]@{schemaVersion=1;createdAtISO=$At;distros=@($d)}
  [IO.File]::WriteAllText($Path,($r|ConvertTo-Json -Depth 6),[text.UTF8Encoding]::new($false))
 }
 Write-Record (Join-Path $fixture '2026-09-29.json') -Used 99
 Write-Record (Join-Path $fixture '2026-09-30.json') -Used 120
 [IO.File]::WriteAllText((Join-Path $fixture '2026-09-28.json'),'not-json')
 Write-Record (Join-Path $fixture '2026-09-27.json') -Name Other -Used 555
 $found=Get-LatestCachedDistro -Directory $fixture -Name Test -Vhdx 'C:\mock\ext4.vhdx' -CurrentDate ([datetime]'2026-09-30')
 Assert-Equal $found.Distro.usedBytes 120 'newest matching cache chosen'
 $wrong=Get-LatestCachedDistro -Directory $fixture -Name Test -Vhdx 'C:\other\ext4.vhdx' -CurrentDate ([datetime]'2026-09-30')
 Assert-Equal $null $wrong 'cache identity includes VHDX path'
 Write-Record (Join-Path $probeDir '2026-10-01.json') -At '2026-10-01T12:00:00+02:00'
 $future=Get-LatestCachedDistro -Directory $fixture -Name Test -Vhdx 'C:\mock\ext4.vhdx' -CurrentDate ([datetime]'2026-09-30')
 Assert-Equal $future.Distro.usedBytes 120 'future cache excluded'
 $target=Join-Path $fixture 'atomic.json'
 Write-AtomicInventoryJson -Path $target -Json '{"schemaVersion":1}'
 Write-AtomicInventoryJson -Path $target -Json '{"schemaVersion":2}'
 Assert-Equal ([IO.File]::ReadAllText($target)) '{"schemaVersion":2}' 'atomic replace updates existing record'
 Assert-Equal ([IO.File]::ReadAllBytes($target)[0] -eq 123) $true 'JSON has no UTF-8 BOM'
 $today=(Get-Date).Date;$old=$today.AddDays(-30).ToString('yyyy-MM-dd');$keep=$today.AddDays(-29).ToString('yyyy-MM-dd');$futureDay=$today.AddDays(1).ToString('yyyy-MM-dd')
 foreach($n in "$old.json","$keep.json","$futureDay.json",'latest.json','2020-01-01.json.bak','unrelated.json'){[IO.File]::WriteAllText((Join-Path $fixture $n),'{}')}
 Remove-ExpiredInventoryRecords -Directory $fixture -CurrentDate $today
 Assert-Equal (Test-Path (Join-Path $fixture "$old.json")) $false 'old date file removed'
 foreach($n in "$keep.json","$futureDay.json",'latest.json','2020-01-01.json.bak','unrelated.json'){Assert-Equal (Test-Path (Join-Path $fixture $n)) $true "preserves $n"}

 & {
  $mock=[pscustomobject]@{Killed=$false;Disposed=$false}
  function Start-Process {param($FilePath,$ArgumentList,$WindowStyle,$RedirectStandardOutput,$RedirectStandardError,[switch]$PassThru)
   Assert-Equal $WindowStyle Hidden 'native process hidden'|Out-Null
   $p=[pscustomobject]@{ExitCode=0};$p|Add-Member ScriptMethod WaitForExit {param($Ms) return $false}
   $p|Add-Member ScriptMethod Kill {$mock.Killed=$true};$p|Add-Member ScriptMethod Dispose {$mock.Disposed=$true};$p
  }
  $r=Invoke-BoundedNative -FilePath wsl.exe -Arguments @('-d','Test','-u','root') -TimeoutMilliseconds 1
  Assert-Equal $r.Status Timeout 'timeout status returned';Assert-Equal $mock.Killed $true 'only child process killed';Assert-Equal $mock.Disposed $true 'child process disposed'
 }
 & {
  $probeDir=Join-Path $fixture 'probe';[IO.Directory]::CreateDirectory($probeDir)|Out-Null
  $state=@{Calls=[collections.generic.list[string]]::new();Running=$false;ProbeCalls=0}
  function Get-WslDisks {[pscustomobject]@{Name='Test';WslVersion=2;Vhdx='C:\mock\ext4.vhdx';FileBytes=900;BasePath='C:\mock'}}
  function Invoke-BoundedNative {param($FilePath,$Arguments,$TimeoutMilliseconds,$MaximumOutputCharacters)
   $state.Calls.Add(($Arguments -join ' '))
   if($Arguments[0] -eq '--list'){$out=' NAME STATE VERSION'+[Environment]::NewLine+$(if($state.Running){'* Test Running 2'}else{'* Test Stopped 2'});return [pscustomobject]@{Status='Success';Output=$out;ExitCode=0}}
   if($Arguments[0] -eq '--version'){return [pscustomobject]@{Status='Success';Output='WSL version: 2.5.0';ExitCode=0}}
   [pscustomobject]@{Status='Failed';Output='sensitive-like diagnostic';ExitCode=1}
  }
  function Invoke-WslInventoryProbe {param($Name);$state.ProbeCalls++;[pscustomobject]@{Status='Success';Usage=[pscustomobject]@{capBytes=1000;usedBytes=800};Largest=@([pscustomobject]@{path='/home/agent';bytes=50});Errors=@()}}
  function Get-PSDrive {param($Name,$PSProvider,$ErrorAction);[pscustomobject]@{Free=123}}
  function Test-Path {param($LiteralPath,$PathType);if($LiteralPath -like 'HKLM:*'){return $false};if($PathType){Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath -PathType $PathType -ErrorAction SilentlyContinue}else{Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath -ErrorAction SilentlyContinue}}
  function Get-ItemProperty {throw 'No registry value'}
  function Get-ScheduledTask {param($TaskName,$ErrorAction);[pscustomobject]@{TaskName='MachineBootstrap-Inventory';State='Ready'}}
  $v=Get-MachineInventoryValue -OutputDirectory $probeDir -CurrentTime ([datetime]'2026-10-01')
  Assert-Equal $state.ProbeCalls 0 'stopped distro never probed'
  Assert-Equal ($state.Calls -match '^-d') $false 'stopped state has no distro invocation'
  Assert-Equal $v.distros[0].Status StoppedNoCache 'missing cache explicit'
  Assert-Equal $v.distros[0].UsedBytes $null 'no invented stopped usage'
  Assert-Equal $v.taskStates[0].Name MachineBootstrap-Inventory 'task metadata captured'
  function Write-CacheForProbe {
   $d=[pscustomobject]@{Name='Test';Vhdx='C:\mock\ext4.vhdx';usedBytes=700;capBytes=1000;largestDirectories=@();usageRecordedAtISO='2026-10-01T01:00:00+02:00'}
   $r=[pscustomobject]@{schemaVersion=1;createdAtISO='2026-10-01T01:00:00+02:00';distros=@($d)}
   [IO.File]::WriteAllText((Join-Path $fixture '2026-10-01.json'),($r|ConvertTo-Json -Depth 6))
  }
  Write-CacheForProbe
  $v=Get-MachineInventoryValue -OutputDirectory $probeDir -CurrentTime ([datetime]'2026-10-01T02:00:00')
  Assert-Equal $v.distros[0].UsageSource Cache 'stopped usage source cache'
  Assert-Equal $v.distros[0].UsedBytes 700 'cached usage retained'
  Assert-Equal $state.ProbeCalls 0 'cache never launches stopped distro'
  $state.Running=$true
  $v=Get-MachineInventoryValue -OutputDirectory $probeDir -CurrentTime ([datetime]'2026-10-01T03:00:00')
  Assert-Equal $state.ProbeCalls 1 'running distro gets one bounded probe'
  Assert-Equal $v.distros[0].UsageSource Live 'running usage source live'
  Assert-Equal $v.distros[0].largestDirectories.Count 1 'directory summary returned'
  Assert-Equal (($v|ConvertTo-Json -Depth 8).Contains('sensitive-like diagnostic')) $false 'native diagnostics never enter inventory'
 }
} finally {Remove-Item -LiteralPath $fixture -Recurse -Force}

& {
 . "$PSScriptRoot/../windows/check-compaction.ps1" -Name Test
 $log=Join-Path $env:TEMP ('bootstrap-compaction-cache-'+[guid]::NewGuid().ToString('N')+'.log')
 function Get-WslDisks {[pscustomobject]@{Name='Test';WslVersion=2;Vhdx='C:\mock\ext4.vhdx';FileBytes=850GB}}
 function Get-WslDistroState {param($Name);[pscustomobject]@{State='Stopped';Version=2}}
 function Invoke-Wsl {throw 'Stopped distro must never launch.'}
 function Get-LatestCachedDistro {param($Directory,$Name,$Vhdx,$CurrentDate);[pscustomobject]@{At=[datetimeoffset]::Now;Distro=[pscustomobject]@{usedBytes=800GB;capBytes=900GB;usageRecordedAtISO='2026-09-30T02:30:00+02:00'}}}
 try {
  Invoke-WslCompactionCheck -Name Test -LogPath $log -InventoryDirectory $fixture|Out-Null
  $text=[IO.File]::ReadAllText($log)
  Assert-True ($text.Contains('Source=Cache')) 'cache provenance in compaction log'
  Assert-True ($text.Contains('RecordedAt=2026-09-30T02:30:00+02:00')) 'cache timestamp in log'
  Assert-True ($text.Contains('COMPACTION RECOMMENDED:')) 'cached cap threshold applies while stopped'
 } finally {Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue}
}

& {
 . "$PSScriptRoot/../windows/install-inventory-task.ps1"
 $state=@{Tasks=0;Copied=[collections.generic.list[string]]::new()}
 function Assert-BootstrapAdministrator {}
 function New-Item {param($ItemType,[switch]$Force,$Path)}
 function Set-Acl {param($LiteralPath,$AclObject);Assert-Equal $AclObject.AreAccessRulesProtected $true 'inventory task directory ACL protected'|Out-Null}
 function Copy-Item {param($LiteralPath,$Destination,[switch]$Force);$state.Copied.Add([IO.Path]::GetFileName($LiteralPath))}
 function Register-ScheduledTask {param($TaskName,$Xml,[switch]$Force)
  Assert-Equal $TaskName MachineBootstrap-Inventory 'stable task identity'|Out-Null
  Assert-Equal ([bool]$Force) $true 'task registration converges'|Out-Null
  $doc=[xml]$Xml;$ns=[xml.xmlnamespacemanager]::new($doc.NameTable);$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
  Assert-Equal $doc.SelectSingleNode('//t:ScheduleByDay/t:DaysInterval',$ns).InnerText '1' 'inventory task runs daily'|Out-Null
  Assert-Equal ($doc.SelectSingleNode('//t:CalendarTrigger/t:StartBoundary',$ns).InnerText -match 'T02:30:00$') $true 'inventory task scheduled 02:30 local'|Out-Null
  Assert-Equal $doc.SelectSingleNode('//t:Principal/t:UserId',$ns).InnerText ([Security.Principal.WindowsIdentity]::GetCurrent().Name) 'inventory task owner'|Out-Null
  Assert-Equal $doc.SelectSingleNode('//t:Principal/t:LogonType',$ns).InnerText S4U 'inventory task stores no password'|Out-Null
  Assert-Equal $doc.SelectSingleNode('//t:Principal/t:RunLevel',$ns).InnerText HighestAvailable 'inventory task elevated'|Out-Null
  Assert-Equal ($doc.SelectSingleNode('//t:Exec/t:Arguments',$ns).InnerText.Contains('-OutputDirectory')) $true 'task writes to inventory folder'|Out-Null
  $state.Tasks++
 }
 function Get-ScheduledTask {param($TaskName);[pscustomobject]@{TaskName=$TaskName;State='Ready'}}
 Install-MachineInventoryTask
 Install-MachineInventoryTask
 Assert-Equal $state.Tasks 2 'repeat task install converges same task'
 Assert-Equal ($state.Copied -join ',') 'common.ps1,inventory.ps1,common.ps1,inventory.ps1' 'only fixed inventory scripts copied'
}
& {
 . "$PSScriptRoot/../windows/inventory.ps1"
 function Invoke-BoundedNative {param($FilePath,$Arguments,$TimeoutMilliseconds,$MaximumOutputCharacters)
  $dirs=@();for($i=1;$i -le 12;$i++){$dirs+=@{path="/home/u$i";bytes=$i}}
  [pscustomobject]@{Status='Success';Output=(@{usage=@{capBytes=1000;usedBytes=400};largestDirectories=$dirs;errors=@()}|ConvertTo-Json -Depth 5 -Compress);ExitCode=0}
 }
 $result=Invoke-WslInventoryProbe -Name Test
 Assert-Equal $result.Largest.Count 10 'probe keeps only ten largest directories'
 Assert-Equal $result.Largest[0].path '/home/u12' 'probe sorts largest directories first'
}

& {
 $dir=Join-Path $env:TEMP ('bootstrap-inventory-write-'+[guid]::NewGuid().ToString('N'))
 function Get-MachineInventoryValue {param($OutputDirectory,$CurrentTime);[pscustomobject]@{schemaVersion=1;createdAtISO=$CurrentTime.ToString('o');distros=@()}}
 try {
  $at=[datetime]'2026-10-01T02:30:00'
  Invoke-MachineInventory -OutputDirectory $dir -At $at|Out-Null
  $date=Join-Path $dir '2026-10-01.json';$latest=Join-Path $dir 'latest.json'
  Assert-Equal (Test-Path $date) $true 'dated JSON record written'
  Assert-Equal ([IO.File]::ReadAllText($date)) ([IO.File]::ReadAllText($latest)) 'latest is same complete snapshot'
  Assert-Equal ([IO.File]::ReadAllBytes($date)[0] -eq 123) $true 'dated output has no BOM'
 } finally {Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue}
}
