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
$resolved=Get-Command -Name Get-WslDisks -CommandType Function
Assert-Equal $resolved.Name Get-WslDisks 'inventory imports shared WSL registry helper'
$parsed=@(ConvertFrom-WslList $listing)
Assert-Equal $parsed.Count 2 'WSL list parsed'
Assert-Equal $parsed[1].State 'Stopped' 'stopped state parsed'
$validRepoTask=[pscustomobject]@{Actions=@([pscustomobject]@{Execute='C:\Windows\System32\wsl.exe';Arguments='-d AgentDev -u agent -- /home/agent/dev_workspace/sample/repo-job';WorkingDirectory=''})}
Assert-True (Test-ApprovedAgentTaskAction $validRepoTask) 'approved absolute WSL repo-job launcher shape'
$badRepoTask=[pscustomobject]@{Actions=@([pscustomobject]@{Execute='C:\Windows\System32\wsl.exe';Arguments='-d AgentDev -u agent -- /home/agent/dev_workspace/../outside/job';WorkingDirectory=''})}
Assert-Equal (Test-ApprovedAgentTaskAction $badRepoTask) $false 'WSL project task traversal rejected'
Assert-Equal (Get-TaskExecutableName '"C:\Program Files\NVIDIA Corporation\NVIDIA App\NVIDIA App.exe"') 'NVIDIA App.exe' 'quoted Execute trimmed before GetFileName'
$quotedRepoTask=[pscustomobject]@{Actions=@([pscustomobject]@{Execute='"C:\Windows\System32\wsl.exe"';Arguments='-d AgentDev -u agent -- /home/agent/dev_workspace/sample/repo-job';WorkingDirectory=''})}
Assert-True (Test-ApprovedAgentTaskAction $quotedRepoTask) 'quoted absolute WSL launcher recognised'
& {
 $synthetic='fixture-argument-must-not-be-serialized'
 $powershell='C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
 function Get-ScheduledTask {param($ErrorAction)
  @(
   [pscustomobject]@{TaskName='MachineBootstrap-Inventory';TaskPath='\';State='Ready';Actions=@([pscustomobject]@{Execute=$powershell;Arguments='-NoProfile -NonInteractive -File "C:\ProgramData\machine-bootstrap\inventory.ps1" -OutputDirectory "C:\ProgramData\machine-bootstrap\inventory" -ExportDirectory "C:\ProgramData\machine-bootstrap\export"';WorkingDirectory='C:\ProgramData\machine-bootstrap'})},
   [pscustomobject]@{TaskName='MachineBootstrap-Compact-Test';TaskPath='\';State='Ready';Actions=@([pscustomobject]@{Execute=$powershell;Arguments='-NoProfile -NonInteractive -File "C:\ProgramData\machine-bootstrap\check-compaction.ps1" -Name "Test" -LogPath "C:\ProgramData\machine-bootstrap\compact.log"';WorkingDirectory='C:\ProgramData\machine-bootstrap'})},
   [pscustomobject]@{TaskName='MachineBootstrap-Evil';TaskPath='\';State='Ready';Actions=@([pscustomobject]@{Execute=$powershell;Arguments=$synthetic;WorkingDirectory='C:\ProgramData\machine-bootstrap'})},
   [pscustomobject]@{TaskName='Custom-Outside';TaskPath='\';State='Ready';Actions=@([pscustomobject]@{Execute='powershell.exe';Arguments=$synthetic;WorkingDirectory='C:\Users\tester\Documents\Codex\repo'})},
   [pscustomobject]@{TaskName='AgentRepoJob-Approved';TaskPath='\';State='Ready';Actions=@([pscustomobject]@{Execute='wsl.exe';Arguments='-d AgentDev -u agent -- /home/agent/dev_workspace/sample/repo-job';WorkingDirectory=''})},
   [pscustomobject]@{TaskName='NvidiaApp-Quoted';TaskPath='\';State='Ready';Actions=@([pscustomobject]@{Execute='"C:\Program Files\NVIDIA Corporation\NVIDIA App\NVIDIA App.exe"';Arguments='';WorkingDirectory=''})},
   [pscustomobject]@{TaskName='Unparseable-Execute';TaskPath='\';State='Ready';Actions=@([pscustomobject]@{Execute='"C:\bad<|>"name.exe';Arguments='';WorkingDirectory=''})}
  )
 }
 $review=Get-ScheduledTaskReview
 Assert-Equal $review.Status 'DRIFT' 'outside-root custom task reported as drift'
 Assert-Equal $review.OwnerOnlyMaintenance.Count 2 'only two declared maintenance tasks are exempt'
 Assert-Equal $review.OwnerOnlyMaintenance[0].Name MachineBootstrap-Inventory 'expected inventory task identity retained'
 Assert-Equal $review.OwnerOnlyMaintenance[1].Name MachineBootstrap-Compact-Test 'expected compaction task identity retained'
 Assert-Equal $review.Tasks[0].Name MachineBootstrap-Evil 'spoofed maintenance task remains visible'
 Assert-True ($review.Tasks[0].Findings -contains 'machine-maintenance-task-identity-or-action-mismatch') 'spoofed maintenance identity flagged'
 Assert-Equal $review.Tasks[1].WorkingRoot 'documents-codex' 'Documents Codex working directory classified'
 Assert-Equal $review.Tasks[2].ActionShape 'wsl-agent-approved-repo-job' 'approved WSL job shape recorded'
 Assert-Equal $review.Tasks[3].Name NvidiaApp-Quoted 'quoted third-party Execute does not fail the review'
 Assert-Equal $review.Tasks[3].ActionShape 'other' 'quoted third-party Execute classified as other'
 Assert-Equal $review.Tasks[4].ActionShape 'other' 'unparseable Execute classified as other'
 $serialized=$review|ConvertTo-Json -Depth 8
 Assert-Equal $serialized.Contains($synthetic) $false 'scheduled task arguments never serialized'
}
& {
 $powershell='C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
 function Get-ScheduledTask {param($ErrorAction)
  [pscustomobject]@{TaskName='MachineBootstrap-Inventory';State='Ready';Actions=@([pscustomobject]@{Execute=$powershell;Arguments='-NoProfile -NonInteractive -File "C:\ProgramData\machine-bootstrap\inventory.ps1" -OutputDirectory "C:\ProgramData\machine-bootstrap\inventory" -ExportDirectory "C:\ProgramData\machine-bootstrap\export"';WorkingDirectory='C:\ProgramData\machine-bootstrap'})}
 }
 $review=Get-ScheduledTaskReview
 Assert-Equal $review.Status 'UNAVAILABLE' 'missing task path is never reported clean'
 Assert-Equal $review.OwnerOnlyMaintenance.Count 0 'missing task path prevents maintenance exemption'
 Assert-True ($review.Tasks[0].Findings -contains 'scheduled-task-metadata-unavailable') 'missing task metadata is visible'
}
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
 Write-Record (Join-Path $fixture '2026-10-01.json') -At '2026-10-01T12:00:00+02:00'
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
  $inventoryFixtureState=@{Calls=[collections.generic.list[string]]::new();Running=$false;Partial=$false;ProbeCalls=0;AgentDriftCalls=0}
  function Get-WslDisks {[pscustomobject]@{Name='Test';WslVersion=2;Vhdx='C:\mock\ext4.vhdx';FileBytes=900;BasePath='C:\mock'}}
  function Invoke-BoundedNative {param($FilePath,$Arguments,$TimeoutMilliseconds,$MaximumOutputCharacters)
   $inventoryFixtureState.Calls.Add(($Arguments -join ' '))
   if($Arguments[0] -eq '--list'){$out=' NAME STATE VERSION'+[Environment]::NewLine+$(if($inventoryFixtureState.Running){'* Test Running 2'}else{'* Test Stopped 2'});return [pscustomobject]@{Status='Success';Output=$out;ExitCode=0}}
   if($Arguments[0] -eq '--version'){return [pscustomobject]@{Status='Success';Output='WSL version: 2.5.0';ExitCode=0}}
   [pscustomobject]@{Status='Failed';Output='sensitive-like diagnostic';ExitCode=1}
  }
  function Invoke-WslInventoryProbe {param($Name);$inventoryFixtureState.ProbeCalls++;[pscustomobject]@{Status=$(if($inventoryFixtureState.Partial){'Partial'}else{'Success'});Usage=[pscustomobject]@{capBytes=1000;usedBytes=800};Largest=@([pscustomobject]@{path='/home/agent';bytes=50});Errors=$(if($inventoryFixtureState.Partial){@('missing-var')}else{@()})}}
  function Invoke-WslAgentDrift {param($Name);$inventoryFixtureState.AgentDriftCalls++;[pscustomobject]@{Status='DRIFT';Findings=@('global-cli-outside-bootstrap:foreign')}}
  function Get-PSDrive {param($Name,$PSProvider,$ErrorAction);[pscustomobject]@{Free=123}}
  function Test-Path {param($LiteralPath,$PathType);if($LiteralPath -like 'HKLM:*'){return $false};if($PathType){Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath -PathType $PathType -ErrorAction SilentlyContinue}else{Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath -ErrorAction SilentlyContinue}}
  function Get-ItemProperty {throw 'No registry value'}
  function Get-ScheduledTask {param($TaskName,$ErrorAction);[pscustomobject]@{TaskName='MachineBootstrap-Inventory';TaskPath='\';State='Ready';Actions=@([pscustomobject]@{Execute=(Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe');Arguments='-NoProfile -NonInteractive -File "C:\ProgramData\machine-bootstrap\inventory.ps1" -OutputDirectory "C:\ProgramData\machine-bootstrap\inventory" -ExportDirectory "C:\ProgramData\machine-bootstrap\export"';WorkingDirectory='C:\ProgramData\machine-bootstrap'})}}
  $v=Get-MachineInventoryValue -OutputDirectory $probeDir -CurrentTime ([datetime]'2026-10-01')
  Assert-Equal $inventoryFixtureState.ProbeCalls 0 'stopped distro never probed'
  Assert-Equal $inventoryFixtureState.AgentDriftCalls 0 'stopped distro never queried for drift'
  Assert-Equal $v.distros[0].AgentDriftStatus 'SKIP' 'stopped distro drift status is SKIP'
  Assert-Equal ($inventoryFixtureState.Calls -match '^-d') $false 'stopped state has no distro invocation'
  Assert-Equal $v.distros[0].Status StoppedNoCache 'missing cache explicit'
  Assert-Equal $v.distros[0].UsedBytes $null 'no invented stopped usage'
  Assert-Equal $v.taskStates[0].Name MachineBootstrap-Inventory 'task metadata captured'
  function Write-CacheForProbe {
   $d=[pscustomobject]@{Name='Test';Vhdx='C:\mock\ext4.vhdx';usedBytes=700;capBytes=1000;largestDirectories=@();usageRecordedAtISO='2026-10-01T01:00:00+02:00'}
   $r=[pscustomobject]@{schemaVersion=1;createdAtISO='2026-10-01T01:00:00+02:00';distros=@($d)}
   [IO.File]::WriteAllText((Join-Path $probeDir '2026-10-01.json'),($r|ConvertTo-Json -Depth 6))
  }
  Write-CacheForProbe
  $v=Get-MachineInventoryValue -OutputDirectory $probeDir -CurrentTime ([datetime]'2026-10-01T02:00:00')
  Assert-Equal $v.distros[0].UsageSource Cache 'stopped usage source cache'
  Assert-Equal $v.distros[0].UsedBytes 700 'cached usage retained'
  Assert-Equal $inventoryFixtureState.ProbeCalls 0 'cache never launches stopped distro'
  $inventoryFixtureState.Running=$true
  $v=Get-MachineInventoryValue -OutputDirectory $probeDir -CurrentTime ([datetime]'2026-10-01T03:00:00')
  Assert-Equal $inventoryFixtureState.ProbeCalls 1 'running distro gets one bounded probe'
  Assert-Equal $inventoryFixtureState.AgentDriftCalls 1 'running distro gets one drift query'
  Assert-Equal $v.distros[0].AgentDriftStatus 'DRIFT' 'running distro drift status is read back'
  Assert-Equal $v.distros[0].AgentDriftFindings[0] 'global-cli-outside-bootstrap:foreign' 'running distro reports fixed drift code'
  Assert-Equal $v.distros[0].UsageSource Live 'running usage source live'
  Assert-Equal $v.distros[0].largestDirectories.Count 1 'directory summary returned'
  $inventoryFixtureState.Partial=$true;$v=Get-MachineInventoryValue -OutputDirectory $probeDir -CurrentTime ([datetime]'2026-10-01T04:00:00')
  Assert-Equal $v.distros[0].Status Partial 'partial directory scan is visible'
  Assert-Equal $v.distros[0].UsedBytes 800 'partial scan preserves valid filesystem usage'
  Assert-Equal $v.distros[0].DirectoryErrors[0] missing-var 'partial scan exposes fixed error code'
  Assert-Equal (($v|ConvertTo-Json -Depth 8).Contains('sensitive-like diagnostic')) $false 'native diagnostics never enter inventory'
 }
} finally {Remove-Item -LiteralPath $fixture -Recurse -Force}

& {
 . "$PSScriptRoot/../windows/check-compaction.ps1" -Name Test
 $log=Join-Path $env:TEMP ('bootstrap-compaction-cache-'+[guid]::NewGuid().ToString('N')+'.log')
 function Get-WslDisks {[pscustomobject]@{Name='Test';WslVersion=2;Vhdx='C:\mock\ext4.vhdx';FileBytes=850GB}}
 $calls=[collections.generic.list[string]]::new()
 function Invoke-BoundedNative {param($FilePath,$Arguments,$TimeoutMilliseconds,$MaximumOutputCharacters)
  $calls.Add(($Arguments -join ' '))
  if($Arguments[0] -eq '--list'){return [pscustomobject]@{Status='Success';Output=(' NAME STATE VERSION'+[Environment]::NewLine+'* Test Stopped 2');ExitCode=0}}
  throw 'Stopped distro must never invoke a distro command.'
 }
 function Get-LatestCachedDistro {param($Directory,$Name,$Vhdx,$CurrentDate);[pscustomobject]@{At=[datetimeoffset]::Now;Distro=[pscustomobject]@{usedBytes=800GB;capBytes=900GB;usageRecordedAtISO='2026-09-30T02:30:00+02:00'}}}
 try {
  Invoke-WslCompactionCheck -Name Test -LogPath $log -InventoryDirectory $fixture|Out-Null
  $text=[IO.File]::ReadAllText($log)
  Assert-True ($text.Contains('Source=Cache')) 'cache provenance in compaction log'
  Assert-True ($text.Contains('RecordedAt=2026-09-30T02:30:00+02:00')) 'cache timestamp in log'
  Assert-True ($text.Contains('COMPACTION RECOMMENDED:')) 'cached cap threshold applies while stopped'
  Assert-Equal ($calls -join ',') '--list --verbose' 'stopped check only lists states and never invokes distro'
 } finally {Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue}
}

& {
 . "$PSScriptRoot/../windows/install-inventory-task.ps1"
 $inventoryTaskFixtureState=@{Tasks=0;Copied=[collections.generic.list[string]]::new();Acls=@{}}
 function Assert-BootstrapAdministrator {}
 function wsl.exe {throw 'Inventory task installer must never call wsl.exe.'}
 function New-Item {param($ItemType,[switch]$Force,$Path)}
 function Set-Acl {param($LiteralPath,$AclObject);Assert-Equal $AclObject.AreAccessRulesProtected $true 'inventory task directory ACL protected'|Out-Null;$inventoryTaskFixtureState.Acls[$LiteralPath]=$AclObject}
 function Copy-Item {param($LiteralPath,$Destination,[switch]$Force);$inventoryTaskFixtureState.Copied.Add([IO.Path]::GetFileName($LiteralPath))}
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
  Assert-Equal ($doc.SelectSingleNode('//t:Exec/t:Arguments',$ns).InnerText.EndsWith('-ExportDirectory "C:\ProgramData\machine-bootstrap\export"')) $true 'task exports latest.json to the Windows-owned export folder'|Out-Null
  $inventoryTaskFixtureState.Tasks++
 }
 function Get-ScheduledTask {param($TaskName);[pscustomobject]@{TaskName=$TaskName;State='Ready'}}
 Install-MachineInventoryTask
 Install-MachineInventoryTask
 Assert-Equal $inventoryTaskFixtureState.Tasks 2 'repeat task install converges same task'
 $exportAcl=$inventoryTaskFixtureState.Acls['C:\ProgramData\machine-bootstrap\export']
 Assert-True ($null -ne $exportAcl) 'export folder ACL set'
 $owner=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
 $rules=@{};foreach($rule in $exportAcl.Access){$rules[$rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value]=$rule.FileSystemRights}
 Assert-Equal ((@($rules.Keys)|Sort-Object) -join ',') ((@('S-1-5-18','S-1-5-32-544',$owner)|Sort-Object) -join ',') 'export folder grants only SYSTEM, Administrators and the owner'
 $full=[Security.AccessControl.FileSystemRights]::FullControl;$write=[Security.AccessControl.FileSystemRights]::Write -bor [Security.AccessControl.FileSystemRights]::Delete
 Assert-True (($rules['S-1-5-18'] -band $full) -eq $full -and ($rules['S-1-5-32-544'] -band $full) -eq $full) 'SYSTEM and Administrators keep full control of the export folder'
 Assert-True (($rules[$owner] -band [Security.AccessControl.FileSystemRights]::ReadData) -ne 0 -and ($rules[$owner] -band $write) -eq 0) 'owner account can read but not write or delete the export'
 Assert-Equal ($inventoryTaskFixtureState.Copied -join ',') 'common.ps1,inventory.ps1,common.ps1,inventory.ps1' 'only fixed inventory scripts copied'
}
& {
 . "$PSScriptRoot/../windows/inventory.ps1"
 $probeFailure=$false
 function Invoke-BoundedNative {param($FilePath,$Arguments,$TimeoutMilliseconds,$MaximumOutputCharacters)
  if($probeFailure){return [pscustomobject]@{Status='Success';Output='{"usage":null,"largestDirectories":[],"errors":["usage-unavailable"]}';ExitCode=0}}
  $dirs=@();for($i=1;$i -le 12;$i++){$dirs+=@{path="/home/u$i";bytes=$i}}
  [pscustomobject]@{Status='Success';Output=(@{usage=@{capBytes=1000;usedBytes=400};largestDirectories=$dirs;errors=@()}|ConvertTo-Json -Depth 5 -Compress);ExitCode=0}
 }
 $result=Invoke-WslInventoryProbe -Name Test
 Assert-Equal $result.Largest.Count 10 'probe keeps only ten largest directories'
 Assert-Equal $result.Largest[0].path '/home/u12' 'probe sorts largest directories first'
 $probeFailure=$true;$result=Invoke-WslInventoryProbe -Name Test
 Assert-Equal $result.Status Partial 'missing df data is a partial probe'
 Assert-Equal $result.Usage $null 'missing df data does not fabricate usage'
 Assert-Equal $result.Errors[0] usage-unavailable 'df failure has a fixed status code'
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


& {
 Assert-True ($script:GhTokenExpiryPython.Contains('--include')) 'gh expiry probe requests response headers'
 Assert-True ($script:GhTokenExpiryPython.Contains('--silent')) 'gh expiry probe suppresses response body'
 Assert-True ($script:GhTokenExpiryPython.Contains('stdout=subprocess.PIPE')) 'raw headers are captured inside the AgentDev process'
 Assert-True ($script:GhTokenExpiryPython.Contains('stderr=subprocess.DEVNULL')) 'gh diagnostics are discarded inside AgentDev'
 Assert-True ($script:GhTokenExpiryPython.Contains('GitHub-Authentication-Token-Expiration')) 'only the documented expiry header is parsed'
 Assert-Equal ($script:GhTokenExpiryPython.Contains('print(result.stdout)')) $false 'Python probe never prints raw headers'
 Assert-Equal ($script:GhTokenExpiryPython.Contains('gh auth token')) $false 'Python probe never requests token contents'
}
foreach($case in @(
 @{Name='exactly-fourteen-days';AgentState='Running';ProbeStatus='Success';Expires='2026-10-15T00:00:00Z';ExpectedStatus='AVAILABLE';ExpectedWarning='expires-within-14-days';ExpectedCalls=1},
 @{Name='more-than-fourteen-days';AgentState='Running';ProbeStatus='Success';Expires='2026-10-16T00:00:00Z';ExpectedStatus='AVAILABLE';ExpectedWarning=$null;ExpectedCalls=1},
 @{Name='already-expired';AgentState='Running';ProbeStatus='Success';Expires='2026-09-30T23:59:59Z';ExpectedStatus='AVAILABLE';ExpectedWarning='expires-within-14-days';ExpectedCalls=1},
 @{Name='missing-header';AgentState='Running';ProbeStatus='Success';Expires=$null;ExpectedStatus='UNKNOWN';ExpectedWarning=$null;ExpectedCalls=1},
 @{Name='invalid-date';AgentState='Running';ProbeStatus='Success';Expires='not-a-date';ExpectedStatus='UNAVAILABLE';ExpectedWarning=$null;ExpectedCalls=1},
 @{Name='probe-timeout';AgentState='Running';ProbeStatus='Timeout';Expires=$null;ExpectedStatus='UNAVAILABLE';ExpectedWarning=$null;ExpectedCalls=1},
 @{Name='stopped-agentdev';AgentState='Stopped';ProbeStatus='Success';Expires='2026-10-02T00:00:00Z';ExpectedStatus='SKIP';ExpectedWarning=$null;ExpectedCalls=0},
 @{Name='unknown-agentdev';AgentState='Unknown';ProbeStatus='Success';Expires='2026-10-02T00:00:00Z';ExpectedStatus='SKIP';ExpectedWarning=$null;ExpectedCalls=0},
 @{Name='stops-before-gh-query';AgentState='Running';FreshAgentState='Stopped';ProbeStatus='Success';Expires='2026-10-02T00:00:00Z';ExpectedStatus='SKIP';ExpectedWarning=$null;ExpectedCalls=0}
)) {
 & {
  $inventoryExpiryState=@{Case=$case;GhCalls=[collections.generic.list[string]]::new();ProbeCalls=0;DriftCalls=0;ListCalls=0}
  function Get-WslDisks {
   @(
    [pscustomobject]@{Name='AgentDev';WslVersion=2;Vhdx='C:\mock\agentdev.vhdx';FileBytes=900;BasePath='C:\mock'},
    [pscustomobject]@{Name='Other';WslVersion=2;Vhdx='C:\mock\other.vhdx';FileBytes=800;BasePath='C:\mock'},
    [pscustomobject]@{Name='Sleeping';WslVersion=2;Vhdx='C:\mock\sleeping.vhdx';FileBytes=700;BasePath='C:\mock'}
   )
  }
  function Invoke-BoundedNative {
   param($FilePath,$Arguments,$TimeoutMilliseconds,$MaximumOutputCharacters)
   if($Arguments[0] -eq '--list'){
    $inventoryExpiryState.ListCalls++
    $lines=@(' NAME STATE VERSION')
    $agentState=$case.AgentState
    if($inventoryExpiryState.ListCalls -gt 1 -and $case.ContainsKey('FreshAgentState') -and $case['FreshAgentState']){$agentState=$case['FreshAgentState']}
    if($agentState -ne 'Unknown'){$lines+=('* AgentDev '+$agentState+' 2')}
    $lines+='  Other Running 2';$lines+='  Sleeping Stopped 2'
    return [pscustomobject]@{Status='Success';Output=($lines -join [Environment]::NewLine);ExitCode=0}
   }
   if($Arguments[0] -eq '--version'){return [pscustomobject]@{Status='Success';Output='WSL version: 2.5.0';ExitCode=0}}
   if($Arguments -contains '-c'){
    $inventoryExpiryState.GhCalls.Add(($Arguments -join ' '))
    if($case.ProbeStatus -eq 'Timeout'){return [pscustomobject]@{Status='Timeout';Output=$null;ExitCode=$null}}
    $status=if($null -eq $case.Expires){'UNKNOWN'}else{'AVAILABLE'}
    $payload=[ordered]@{schemaVersion=1;status=$status;expiresAtISO=$case.Expires;secretMarker='gho-never-serialize';diagnostic='raw gh output must not be copied'}
    return [pscustomobject]@{Status='Success';Output=($payload|ConvertTo-Json -Compress);ExitCode=0}
   }
   [pscustomobject]@{Status='Failed';Output='discarded diagnostic';ExitCode=1}
  }
  function Invoke-WslInventoryProbe {param($Name);$inventoryExpiryState.ProbeCalls++;[pscustomobject]@{Status='Success';Usage=[pscustomobject]@{capBytes=1000;usedBytes=500};Largest=@();Errors=@()}}
  function Invoke-WslAgentDrift {param($Name);$inventoryExpiryState.DriftCalls++;[pscustomobject]@{Status='CLEAN';Findings=@()}}
  function Get-LatestCachedDistro {param($Directory,$Name,$Vhdx,$CurrentDate);$null}
  function Get-PSDrive {param($Name,$PSProvider,$ErrorAction);[pscustomobject]@{Free=123}}
  function Test-Path {param($LiteralPath,$PathType);if($LiteralPath -like 'HKLM:*'){return $false};$false}
  function Get-ItemProperty {throw 'No registry value'}
  function Get-ScheduledTask {param($TaskName,$ErrorAction);@()}
  $now=[datetime]::SpecifyKind([datetime]'2026-10-01T00:00:00',[DateTimeKind]::Utc)
  $v=Get-MachineInventoryValue -OutputDirectory $env:TEMP -CurrentTime $now
  $agent=@($v.distros|Where-Object{$_.Name -eq 'AgentDev'})[0]
  Assert-Equal $inventoryExpiryState.GhCalls.Count $case.ExpectedCalls "$($case.Name) AgentDev-only query gate"
  Assert-Equal $agent.GhTokenExpiryStatus $case.ExpectedStatus "$($case.Name) status"
  Assert-Equal $agent.GhTokenExpiryWarning $case.ExpectedWarning "$($case.Name) warning threshold"
  if($case.ExpectedCalls -eq 1){Assert-True ([bool]$agent.GhTokenExpiryRecordedAtISO) "$($case.Name) records query time"}else{Assert-Equal $agent.GhTokenExpiryRecordedAtISO $null "$($case.Name) skipped query has no recorded time"}
  if($case.ExpectedStatus -eq 'AVAILABLE' -and $case.Name -ne 'invalid-date'){
   Assert-Equal $agent.GhTokenExpiresAtISO $case.Expires "$($case.Name) safe expiry date recorded"
  }else{Assert-Equal $agent.GhTokenExpiresAtISO $null "$($case.Name) has no untrusted expiry date"}
  $other=@($v.distros|Where-Object{$_.Name -eq 'Other'})[0]
  Assert-Equal $other.GhTokenExpiryStatus 'SKIP' "$($case.Name) other distro never receives token metadata"
  $serialized=$v|ConvertTo-Json -Depth 8
  Assert-Equal $serialized.Contains('gho-never-serialize') $false "$($case.Name) extra token-like field is discarded"
  Assert-Equal $serialized.Contains('raw gh output') $false "$($case.Name) raw diagnostic field is discarded"
 }
}
& {
 $dir=Join-Path $env:TEMP ('bootstrap-gh-expiry-warning-'+[guid]::NewGuid().ToString('N'))
 function Get-MachineInventoryValue {param($OutputDirectory,$CurrentTime);[pscustomobject]@{schemaVersion=2;createdAtISO=$CurrentTime.ToString('o');distros=@([pscustomobject]@{Name='AgentDev';GhTokenExpiryStatus='AVAILABLE';GhTokenExpiresAtISO='2026-10-14T23:59:59Z';GhTokenExpiryWarning='expires-within-14-days'})}}
 try {
  $lines=@(Invoke-MachineInventory -OutputDirectory $dir -At ([datetime]'2026-10-01T00:00:00Z'))
  Assert-Equal (@($lines|Where-Object{$_ -like 'WARNING: AgentDev gh token expires within 14 days:*'}).Count) 1 'inventory emits a fixed warning line'
  Assert-Equal (($lines -join [Environment]::NewLine).Contains('gho-')) $false 'warning line contains no credential value'
 } finally {Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue}
}

# The export only writes a Windows-owned file: no distro command, no UNC path.
& {
 $dir=Join-Path $env:TEMP ('bootstrap-inventory-export-'+[guid]::NewGuid().ToString('N'))
 $export=Join-Path $env:TEMP ('bootstrap-inventory-export-out-'+[guid]::NewGuid().ToString('N'));$parent=$null
 function Get-MachineInventoryValue {param($OutputDirectory,$CurrentTime);[pscustomobject]@{schemaVersion=2;createdAtISO=$CurrentTime.ToString('o');distros=@()}}
 function Invoke-BoundedNative {throw 'Export must never run a native command.'}
 function wsl.exe {throw 'Export must never call wsl.exe.'}
 try {
  Invoke-MachineInventory -OutputDirectory $dir -ExportDirectory $export -At ([datetime]'2026-10-03T02:30:00')|Out-Null
  $target=Join-Path $export 'latest.json'
  Assert-Equal ([IO.File]::ReadAllText($target)) ([IO.File]::ReadAllText((Join-Path $dir 'latest.json'))) 'export is the exact Windows latest snapshot'
  Assert-Equal ([IO.File]::ReadAllBytes($target)[0] -eq 123) $true 'export has no UTF-8 BOM'
  Invoke-MachineInventory -OutputDirectory $dir -ExportDirectory $export -At ([datetime]'2026-10-04T02:30:00')|Out-Null
  Assert-True ([IO.File]::ReadAllText($target).Contains('2026-10-04')) 'next run atomically replaces the export'
  Assert-Equal (@(Get-ChildItem -LiteralPath $export -Force).Count) 1 'export folder holds only latest.json'
  Assert-Equal (@(Get-ChildItem -LiteralPath $export -Filter '2026-*.json' -Force).Count) 0 'dated history is never exported'
  $parent=Join-Path $env:TEMP ('bootstrap-inventory-no-export-'+[guid]::NewGuid().ToString('N'))
  Invoke-MachineInventory -OutputDirectory (Join-Path $parent 'inventory') -At ([datetime]'2026-10-03T02:30:00')|Out-Null
  Assert-Equal (@(Get-ChildItem -LiteralPath $parent -Directory -Force).Count) 1 'no export folder is created unless requested'
 } finally {foreach($path in $dir,$export,$parent){if($path){Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue}}}
 $source=[IO.File]::ReadAllText((Join-Path $PSScriptRoot '../windows/inventory.ps1'))
 Assert-Equal $source.Contains('wsl.localhost') $false 'inventory never references the WSL UNC share'
}
