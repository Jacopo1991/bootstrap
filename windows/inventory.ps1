[CmdletBinding()]
param([string]$OutputDirectory='C:\ProgramData\machine-bootstrap\inventory',[datetime]$Now=(Get-Date))
. "$PSScriptRoot/common.ps1"
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'

function ConvertTo-WindowsArgument {
 param([AllowEmptyString()][string]$Value)
 if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
 $b=New-Object Text.StringBuilder; [void]$b.Append('"'); $slashes=0
 foreach($c in $Value.ToCharArray()) {
  if($c -eq '\') {$slashes++;continue}
  if($c -eq '"') {[void]$b.Append(('\'*(2*$slashes+1)));[void]$b.Append('"');$slashes=0;continue}
  if($slashes){[void]$b.Append(('\'*$slashes));$slashes=0};[void]$b.Append($c)
 }
 if($slashes){[void]$b.Append(('\'*(2*$slashes)))};[void]$b.Append('"');$b.ToString()
}
function Invoke-BoundedNative {
 param([string]$FilePath,[string[]]$Arguments,[int]$TimeoutMilliseconds=10000,[int]$MaximumOutputCharacters=65536)
 $out=Join-Path $env:TEMP ('mb-out-'+[guid]::NewGuid().ToString('N')+'.tmp');$err=Join-Path $env:TEMP ('mb-err-'+[guid]::NewGuid().ToString('N')+'.tmp');$p=$null
 try {
  $argsText=@($Arguments|%{ConvertTo-WindowsArgument $_}) -join ' '
  $p=Start-Process -FilePath $FilePath -ArgumentList $argsText -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
  if(-not $p.WaitForExit($TimeoutMilliseconds)){try{$p.Kill()}catch{};try{$p.WaitForExit(5000)|Out-Null}catch{};return [pscustomobject]@{Status='Timeout';Output=$null;ExitCode=$null}}
  $text=if(Test-Path -LiteralPath $out){[IO.File]::ReadAllText($out)}else{''}
  if($text.Length -gt $MaximumOutputCharacters){return [pscustomobject]@{Status='OutputTooLarge';Output=$null;ExitCode=$p.ExitCode}}
  [pscustomobject]@{Status=if($p.ExitCode -eq 0){'Success'}else{'Failed'};Output=$text.Replace([string][char]0,'');ExitCode=[int]$p.ExitCode}
 }catch{[pscustomobject]@{Status='Unavailable';Output=$null;ExitCode=$null}}
 finally{if($p){$p.Dispose()};foreach($f in $out,$err){if(Test-Path -LiteralPath $f){Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue}}}
}

$script:InventoryPython=@'
import json,os,pathlib,subprocess,time
usage=None
try:
 r=subprocess.run(["df","-B1","--output=size,used","/"],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5,text=True)
 f=r.stdout.splitlines()[-1].split()
 if r.returncode==0 and len(f)>=2: usage={"capBytes":int(f[0]),"usedBytes":int(f[1])}
except Exception: pass
deadline=time.monotonic()+54
sizes=[];errors=[]
for root in ("/home","/var"):
 try: children=list(pathlib.Path(root).iterdir())
 except FileNotFoundError: errors.append("missing-"+root[1:]);continue
 except Exception: errors.append("unreadable-"+root[1:]);continue
 for child in children:
  if time.monotonic()>deadline: raise TimeoutError()
  try:
   if child.is_symlink() or not child.is_dir(): continue
   total=0
   for current,dirs,files in os.walk(str(child),followlinks=False):
    if time.monotonic()>deadline: raise TimeoutError()
    dirs[:]=[x for x in dirs if not os.path.islink(os.path.join(current,x))]
    for name in dirs+files:
     p=pathlib.Path(current)/name
     try:
      if not p.is_symlink(): total+=int(getattr(p.stat(follow_symlinks=False),"st_blocks",0))*512
     except Exception: pass
   sizes.append({"path":str(child),"bytes":total})
  except TimeoutError: raise
  except Exception: errors.append("scan-error")
sizes.sort(key=lambda x:(-x["bytes"],x["path"]))
print(json.dumps({"usage":usage,"largestDirectories":sizes[:10],"errors":sorted(set(errors))},separators=(",",":")))
'@

function ConvertFrom-WslList {
 param([string]$Text)
 $Text=$Text.Replace([string][char]0,'')
 $items=[collections.generic.list[object]]::new();$header=$false
 foreach($line in ($Text -split '\r?\n')){
  if($line -match '^\s*NAME\s+STATE\s+VERSION\s*$'){$header=$true;continue}
  if([string]::IsNullOrWhiteSpace($line)){continue};if(-not $header){throw 'Malformed'}
  if($line -notmatch '^\s*(?:\*\s*)?(.+?)\s+(Running|Stopped)\s+(\d+)\s*$'){throw 'Malformed'}
  $items.Add([pscustomobject]@{Name=$Matches[1].Trim();State=$Matches[2];WslVersion=[int]$Matches[3]})
 }
 if(-not $header){throw 'Malformed'};$items.ToArray()
}
function Get-ValidInventoryRecord {
 param([string]$Path)
 try{$r=Get-Content -LiteralPath $Path -Raw|ConvertFrom-Json;if($r.schemaVersion -ne 1 -or -not $r.createdAtISO -or $null -eq $r.distros){return};$null=[datetimeoffset]::Parse([string]$r.createdAtISO);$r}catch{}
}
function Get-LatestCachedDistro {
 param([string]$Directory,[string]$Name,[string]$Vhdx,[datetime]$CurrentDate)
 if(-not(Test-Path -LiteralPath $Directory -PathType Container)){return}
 $min=$CurrentDate.Date.AddDays(-29);$matches=[collections.generic.list[object]]::new()
 foreach($f in Get-ChildItem -LiteralPath $Directory -File){
  if($f.Name -notmatch '^\d{4}-\d{2}-\d{2}\.json$'){continue}
  try{$date=[datetime]::ParseExact($f.BaseName,'yyyy-MM-dd',[globalization.cultureinfo]::InvariantCulture)}catch{continue}
  if($date.Date -lt $min -or $date.Date -gt $CurrentDate.Date){continue};$r=Get-ValidInventoryRecord $f.FullName;if($null -eq $r){continue}
  try{$recordAt=[datetimeoffset]::Parse([string]$r.createdAtISO)}catch{continue}
  if($recordAt.Date -ne $date.Date -or $recordAt.Date -lt $min -or $recordAt.Date -gt $CurrentDate.Date){continue}
  foreach($d in $r.distros){
   if($null -eq $d){continue}
   $nameProperty=$d.PSObject.Properties['Name'];$pathProperty=$d.PSObject.Properties['Vhdx'];$usedProperty=$d.PSObject.Properties['usedBytes'];$measuredProperty=$d.PSObject.Properties['usageRecordedAtISO']
   if($null -eq $nameProperty -or $null -eq $pathProperty -or $null -eq $usedProperty -or $null -eq $measuredProperty){continue}
   if(-not [string]::Equals([string]$nameProperty.Value,$Name,'OrdinalIgnoreCase') -or -not [string]::Equals([string]$pathProperty.Value,$Vhdx,'OrdinalIgnoreCase') -or $null -eq $usedProperty.Value){continue}
   try{$measuredAt=[datetimeoffset]::Parse([string]$measuredProperty.Value)}catch{continue}
   if($measuredAt.Date -lt $min -or $measuredAt.Date -gt $CurrentDate.Date){continue}
   $matches.Add([pscustomobject]@{At=$recordAt;Distro=$d})
  }
 }
 $matches|Sort-Object At -Descending|Select-Object -First 1
}
function Invoke-WslInventoryProbe {
 param([string]$Name)
 $wsl=Join-Path $env:SystemRoot 'System32\wsl.exe'
 $r=Invoke-BoundedNative $wsl @('-d',$Name,'-u','root','--','python3','-c',$script:InventoryPython) 60000 262144
 if($r.Status -ne 'Success'){return [pscustomobject]@{Status=$r.Status;Usage=$null;Largest=@();Errors=@()}}
 try{$d=$r.Output|ConvertFrom-Json;$u=$d.usage;if($u -and ($u.capBytes -lt 1 -or $u.usedBytes -lt 0)){throw 'bad'}
  $dirs=@($d.largestDirectories|?{$_.path -match '^/(home|var)/[^/]+$' -and $_.bytes -ge 0}|select -First 10)
  [pscustomobject]@{Status=if(@($d.errors).Count -gt 0){'Partial'}else{'Success'};Usage=$u;Largest=$dirs;Errors=@($d.errors|Where-Object{$_ -in 'missing-home','missing-var','unreadable-home','unreadable-var','scan-error'})}
 }catch{[pscustomobject]@{Status='InvalidProbeOutput';Usage=$null;Largest=@();Errors=@()}}
}
function Get-MachineInventoryValue {
 param([string]$OutputDirectory,[datetime]$CurrentTime)
 $wsl=Join-Path $env:SystemRoot 'System32\wsl.exe'
 $lr=Invoke-BoundedNative $wsl @('--list','--verbose') 10000
 $live=@();$ws=$lr.Status
 if($lr.Status -eq 'Success'){try{$live=@(ConvertFrom-WslList $lr.Output)}catch{$ws='InvalidList'}}
 $distros=[collections.generic.list[object]]::new()
 foreach($disk in @(Get-WslDisks)){
  $sr=@($live|?{[string]::Equals($_.Name,$disk.Name,'OrdinalIgnoreCase')}|select -First 1)
  $state=if($sr.Count){$sr[0].State}else{'Unknown'};$ver=if($sr.Count){[int]$sr[0].WslVersion}else{[int]$disk.WslVersion}
  $e=[ordered]@{Name=[string]$disk.Name;State=$state;WslVersion=$ver;Vhdx=[string]$disk.Vhdx;FileBytes=$disk.FileBytes;UsedBytes=$null;CapBytes=$null;LargestDirectories=@();DirectoryErrors=@();Status='Unavailable';UsageSource='Unavailable';UsageRecordedAtISO=$null;CacheRecordDate=$null}
  if($state -eq 'Running' -and $ver -eq 2 -and $disk.WslVersion -eq 2){
   $p=Invoke-WslInventoryProbe $disk.Name;$e.Status=$p.Status;$e.DirectoryErrors=@($p.Errors)
   if(($p.Status -in @('Success','Partial')) -and $null -ne $p.Usage){$e.UsedBytes=[long]$p.Usage.usedBytes;$e.CapBytes=[long]$p.Usage.capBytes;$e.LargestDirectories=@($p.Largest);if($p.Status -eq 'Success'){$e.Status='Success'}else{$e.Status='Partial'};$e.UsageSource='Live';$e.UsageRecordedAtISO=$CurrentTime.ToString('o')}
  }elseif($state -eq 'Stopped' -and $ver -eq 2 -and $disk.WslVersion -eq 2){
   $c=Get-LatestCachedDistro $OutputDirectory $disk.Name $disk.Vhdx $CurrentTime
   if($c){$e.UsedBytes=[long]$c.Distro.usedBytes;$cp=$c.Distro.PSObject.Properties['capBytes'];if($null -ne $cp -and $null -ne $cp.Value){$e.CapBytes=[long]$cp.Value};$dp=$c.Distro.PSObject.Properties['largestDirectories'];if($null -ne $dp){$e.LargestDirectories=@($dp.Value)};$ep=$c.Distro.PSObject.Properties['directoryErrors'];if($null -ne $ep){$e.DirectoryErrors=@($ep.Value)};$e.Status='Cached';$e.UsageSource='Cache';$e.UsageRecordedAtISO=[string]$c.Distro.usageRecordedAtISO;$e.CacheRecordDate=$c.At.ToString('o')}else{$e.Status='StoppedNoCache'}
  }elseif($ver -ne 2 -or $disk.WslVersion -ne 2){$e.Status='UnsupportedWslVersion'}elseif($state -eq 'Unknown'){$e.Status='StateUnavailable'}
  $distros.Add([pscustomobject]$e)
 }
 $drives=[ordered]@{}
 foreach($l in 'C','D'){$d=Get-PSDrive -Name $l -PSProvider FileSystem -ErrorAction SilentlyContinue;$drives[$l]=[pscustomobject]@{FreeBytes=if($d){[long]$d.Free}else{$null};Status=if($d){'Available'}else{'Unavailable'}}}
 $vr=Invoke-BoundedNative $wsl @('--version') 10000;$wv=$null;if($vr.Status -eq 'Success' -and $vr.Output -match '(?im)^WSL version:\s*([0-9A-Za-z.-]+)\s*$'){$wv=$Matches[1]}
 $nr=Invoke-BoundedNative 'nvidia-smi.exe' @('--query-gpu=driver_version','--format=csv,noheader') 10000 8192;$nv=@()
 if($nr.Status -eq 'Success'){$nv=@($nr.Output -split '\r?\n'|%{$_.Trim()}|?{$_ -match '^\d{1,4}\.\d{1,4}(?:\.\d{1,4})?$'}|select -Unique)}
 $reboot=[ordered]@{ComponentBasedServicing=(Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending');WindowsUpdate=(Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired');PendingFileRenameOperations=$false}
 try{$reboot.PendingFileRenameOperations=$null -ne (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations -ErrorAction Stop).PendingFileRenameOperations}catch{}
 $tasks=@();try{$tasks=@(Get-ScheduledTask -TaskName 'MachineBootstrap-*' -ErrorAction Stop|%{[pscustomobject]@{Name=[string]$_.TaskName;State=[string]$_.State}}|sort Name)}catch{}
 [pscustomobject]@{schemaVersion=1;createdAtISO=$CurrentTime.ToString('o');wslVersion=$wv;wslStatus=$ws;nvidiaDriverVersions=$nv;nvidiaStatus=$nr.Status;drives=$drives;pendingReboot=$reboot;taskStates=$tasks;distros=$distros.ToArray()}
}
function Write-AtomicInventoryJson {
 param([string]$Path,[string]$Json)
 $tmp=Join-Path (Split-Path -Parent $Path) ('.inventory-'+[guid]::NewGuid().ToString('N')+'.tmp')
 try{[IO.File]::WriteAllText($tmp,$Json,[text.UTF8Encoding]::new($false));if([IO.File]::Exists($Path)){[IO.File]::Replace($tmp,$Path,$null)}else{[IO.File]::Move($tmp,$Path)}}finally{if([IO.File]::Exists($tmp)){[IO.File]::Delete($tmp)}}
}
function Remove-ExpiredInventoryRecords {
 param([string]$Directory,[datetime]$CurrentDate)
 $cut=$CurrentDate.Date.AddDays(-29)
 foreach($f in Get-ChildItem -LiteralPath $Directory -File){if($f.Name -notmatch '^\d{4}-\d{2}-\d{2}\.json$'){continue};try{$d=[datetime]::ParseExact($f.BaseName,'yyyy-MM-dd',[globalization.cultureinfo]::InvariantCulture)}catch{continue};if($d.Date -lt $cut){Remove-Item -LiteralPath $f.FullName -Force}}
}
function Invoke-MachineInventory {
 [CmdletBinding()]param([string]$OutputDirectory='C:\ProgramData\machine-bootstrap\inventory',[datetime]$At=(Get-Date))
 [IO.Directory]::CreateDirectory($OutputDirectory)|Out-Null
 $v=Get-MachineInventoryValue $OutputDirectory $At;$json=$v|ConvertTo-Json -Depth 8;$date=Join-Path $OutputDirectory ($At.ToString('yyyy-MM-dd')+'.json')
 Write-AtomicInventoryJson $date $json;Write-AtomicInventoryJson (Join-Path $OutputDirectory 'latest.json') $json
 Remove-ExpiredInventoryRecords $OutputDirectory $At;Write-Output ('Inventory recorded: '+$date)
}
if($MyInvocation.InvocationName -ne '.') {Invoke-MachineInventory -OutputDirectory $OutputDirectory -At $Now}
