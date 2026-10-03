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

$script:GhTokenExpiryPython=@'
import datetime,json,os,re,subprocess
def emit(status,expires_at=None):
    print(json.dumps({"schemaVersion":1,"status":status,"expiresAtISO":expires_at},separators=(",",":")))
try:
    env=dict(os.environ)
    env.pop("GH_DEBUG",None)
    result=subprocess.run(["/usr/bin/gh","api","--hostname","github.com","--include","--silent","user"],cwd="/",env=env,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=15,text=True)
    if result.returncode!=0:
        emit("UNAVAILABLE")
    else:
        values=[]
        for line in result.stdout.splitlines():
            match=re.match(r"(?i)^GitHub-Authentication-Token-Expiration:\s*(.*?)\s*$",line)
            if match:
                values.append(match.group(1))
        if len(values)!=1:
            emit("UNKNOWN" if not values else "UNAVAILABLE")
        else:
            value=values[0]
            try:
                parsed=datetime.datetime.strptime(value,"%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=datetime.timezone.utc)
            except ValueError:
                try:
                    parsed=datetime.datetime.fromisoformat(value.replace("Z","+00:00"))
                    if parsed.tzinfo is None:
                        raise ValueError("timezone required")
                    parsed=parsed.astimezone(datetime.timezone.utc)
                except Exception:
                    emit("UNAVAILABLE")
                    raise SystemExit(0)
            emit("AVAILABLE",parsed.replace(microsecond=0).isoformat().replace("+00:00","Z"))
except Exception:
    emit("UNAVAILABLE")
'@
$script:InventoryPython=@'
import json,os,pathlib,subprocess,time
usage=None
sizes=[];errors=[]
try:
 r=subprocess.run(["df","-B1","--output=size,used","/"],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5,text=True)
 f=r.stdout.splitlines()[-1].split()
 if r.returncode==0 and len(f)>=2: usage={"capBytes":int(f[0]),"usedBytes":int(f[1])}
except Exception: pass
if usage is None: errors.append("usage-unavailable")
deadline=time.monotonic()+54
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
 $min=$CurrentDate.Date.AddDays(-29);$cacheCandidates=[collections.generic.list[object]]::new()
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
   $cacheCandidates.Add([pscustomobject]@{At=$recordAt;Distro=$d})
  }
 }
 $cacheCandidates|Sort-Object At -Descending|Select-Object -First 1
}
function Invoke-WslInventoryProbe {
 param([string]$Name)
 $wsl=Join-Path $env:SystemRoot 'System32\wsl.exe'
 $r=Invoke-BoundedNative $wsl @('-d',$Name,'-u','root','--','python3','-c',$script:InventoryPython) 60000 262144
 if($r.Status -ne 'Success'){return [pscustomobject]@{Status=$r.Status;Usage=$null;Largest=@();Errors=@()}}
 try{$d=$r.Output|ConvertFrom-Json;$u=$d.usage;if($u -and ($u.capBytes -lt 1 -or $u.usedBytes -lt 0)){throw 'bad'}
  $dirs=@($d.largestDirectories|Where-Object{$_.path -match '^/(home|var)/[^/]+$' -and $_.bytes -ge 0}|Sort-Object @{Expression='bytes';Descending=$true},@{Expression='path';Descending=$false}|Select-Object -First 10)
  [pscustomobject]@{Status=if(@($d.errors).Count -gt 0){'Partial'}else{'Success'};Usage=$u;Largest=$dirs;Errors=@($d.errors|Where-Object{$_ -in 'missing-home','missing-var','unreadable-home','unreadable-var','scan-error','usage-unavailable'})}
 }catch{[pscustomobject]@{Status='InvalidProbeOutput';Usage=$null;Largest=@();Errors=@()}}
}
function Get-SafeTaskName {
 param([string]$Name)
 $safe=$Name -replace '[^A-Za-z0-9_. -]','_'
 if($safe.Length -gt 96){$safe=$safe.Substring(0,96)}
 $safe
}
function Get-TaskWorkingRoot {
 param([string]$Path)
 if([string]::IsNullOrWhiteSpace($Path)){return 'unset'}
 $p=$Path.TrimEnd('\')
 if($p -match '(?i)^C:\\ProgramData\\machine-bootstrap(?:\\|$)'){return 'machine-maintenance'}
 if($p -match '(?i)^C:\\backups\\[^\\]+(?:\\|$)'){return 'owner-backup'}
 if($p -match '(?i)^C:\\Users\\[^\\]+\\Documents\\Codex(?:\\|$)'){return 'documents-codex'}
 if($p -match '(?i)^D:\\wsl\\[^\\]+(?:\\|$)'){return 'wsl-storage'}
 'outside-approved-root'
}
function Test-ApprovedAgentTaskAction {
 param($Task)
 $actions=@($Task.Actions)
 if($actions.Count -ne 1){return $false}
 $action=$actions[0]
 $executeProperty=$action.PSObject.Properties['Execute'];$argumentsProperty=$action.PSObject.Properties['Arguments'];$workingProperty=$action.PSObject.Properties['WorkingDirectory']
 if($null -eq $executeProperty -or $null -eq $argumentsProperty -or $null -eq $workingProperty){return $false}
 $exe=[IO.Path]::GetFileName([string]$executeProperty.Value)
 $args=[string]$argumentsProperty.Value
 $wd=[string]$workingProperty.Value
 if(-not [string]::Equals($exe,'wsl.exe','OrdinalIgnoreCase') -or -not [string]::IsNullOrWhiteSpace($wd)){return $false}
 if($args -notmatch '^-d\s+AgentDev\s+-u\s+agent\s+--\s+/home/agent/dev_workspace/[A-Za-z0-9._/-]+$'){return $false}
 $args -notmatch '(^|/)\.\.(/|$)'
}
function Test-ExpectedMaintenanceTask {
 param($Task)
 $nameProperty=$Task.PSObject.Properties['TaskName'];$pathProperty=$Task.PSObject.Properties['TaskPath'];$actionsProperty=$Task.PSObject.Properties['Actions']
 if($null -eq $nameProperty -or $null -eq $pathProperty -or $null -eq $actionsProperty){return $false}
 $name=[string]$nameProperty.Value;$taskPath=[string]$pathProperty.Value
 if(-not [string]::Equals($taskPath,'\','OrdinalIgnoreCase')){return $false}
 $actions=@($actionsProperty.Value);if($actions.Count -ne 1){return $false}
 $action=$actions[0]
 $executeProperty=$action.PSObject.Properties['Execute'];$argumentsProperty=$action.PSObject.Properties['Arguments'];$workingProperty=$action.PSObject.Properties['WorkingDirectory']
 if($null -eq $executeProperty -or $null -eq $argumentsProperty -or $null -eq $workingProperty){return $false}
 $expectedPowerShell=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
 if(-not [string]::Equals([string]$executeProperty.Value,$expectedPowerShell,'OrdinalIgnoreCase')){return $false}
 if(-not [string]::Equals([string]$workingProperty.Value,'C:\ProgramData\machine-bootstrap','OrdinalIgnoreCase')){return $false}
 if([string]::Equals($name,'MachineBootstrap-Inventory','OrdinalIgnoreCase')){
  $expected='-NoProfile -NonInteractive -File "C:\ProgramData\machine-bootstrap\inventory.ps1" -OutputDirectory "C:\ProgramData\machine-bootstrap\inventory"'
  return [string]::Equals([string]$argumentsProperty.Value,$expected,'OrdinalIgnoreCase')
 }
 if($name -match '^MachineBootstrap-Compact-([A-Za-z][A-Za-z0-9_-]{0,47})$'){
  $compactName=$Matches[1]
  $expected='-NoProfile -NonInteractive -File "C:\ProgramData\machine-bootstrap\check-compaction.ps1" -Name "'+$compactName+'" -LogPath "C:\ProgramData\machine-bootstrap\compact.log"'
  return [string]::Equals([string]$argumentsProperty.Value,$expected,'OrdinalIgnoreCase')
 }
 $false
}
function Get-ScheduledTaskReview {
 $projectTasks=[collections.generic.list[object]]::new()
 $ownerTasks=[collections.generic.list[object]]::new()
 $hasUnknownTaskPath=$false
 try{$all=@(Get-ScheduledTask -ErrorAction Stop)}catch{return [pscustomobject]@{Status='UNAVAILABLE';Tasks=@();OwnerOnlyMaintenance=@()}}
 foreach($task in $all){
  $nameProperty=$task.PSObject.Properties['TaskName'];$pathProperty=$task.PSObject.Properties['TaskPath'];$stateProperty=$task.PSObject.Properties['State'];$actionsProperty=$task.PSObject.Properties['Actions']
  if($null -eq $nameProperty -or $null -eq $pathProperty){$hasUnknownTaskPath=$true}
  $name=if($null -ne $nameProperty){[string]$nameProperty.Value}else{''}
  $safe=if($name){Get-SafeTaskName $name}else{'unknown-task'}
  if(Test-ExpectedMaintenanceTask $task){
   $ownerTasks.Add([pscustomobject]@{Name=$safe;State=if($null -ne $stateProperty){[string]$stateProperty.Value}else{'Unknown'};Scope='owner-only-maintenance'})
   continue
  }
  $taskPath=if($null -ne $pathProperty){[string]$pathProperty.Value}else{''}
  if($pathProperty -and $taskPath -like '\Microsoft\Windows\*'){continue}
  $actions=@()
  if($null -ne $actionsProperty){$actions=@($actionsProperty.Value)}
  $shape='other'
  if($actions.Count -eq 1){
   $executeProperty=$actions[0].PSObject.Properties['Execute'];$exe=if($null -ne $executeProperty){[IO.Path]::GetFileName([string]$executeProperty.Value)}else{''}
   if([string]::Equals($exe,'wsl.exe','OrdinalIgnoreCase')){
    if(Test-ApprovedAgentTaskAction ([pscustomobject]@{Actions=$actions} )){$shape='wsl-agent-approved-repo-job'}else{$shape='wsl-agent-invalid-shape'}
   }elseif([string]::Equals($exe,'powershell.exe','OrdinalIgnoreCase') -or [string]::Equals($exe,'pwsh.exe','OrdinalIgnoreCase') -or [string]::Equals($exe,'cmd.exe','OrdinalIgnoreCase')){$shape='windows-host-action'}
  }
  $working='unset'
  if($actions.Count -eq 1){$working=Get-TaskWorkingRoot ([string]$actions[0].WorkingDirectory)}
  $findings=@()
  if($shape -ne 'wsl-agent-approved-repo-job'){$findings+='unapproved-project-task'}
  if($working -in @('documents-codex','outside-approved-root','wsl-storage')){$findings+='working-directory-outside-agent-code-root'}
  if($name -like 'MachineBootstrap-*'){$findings+='machine-maintenance-task-identity-or-action-mismatch'}
  if($null -eq $pathProperty -or $null -eq $nameProperty){$findings+='scheduled-task-metadata-unavailable'}
  $projectTasks.Add([pscustomobject]@{Name=$safe;State=if($null -ne $stateProperty){[string]$stateProperty.Value}else{'Unknown'};ActionShape=$shape;WorkingRoot=$working;Findings=@($findings|Select-Object -Unique)})
 }
 $status=if($hasUnknownTaskPath){'UNAVAILABLE'}elseif($projectTasks.Count){if(@($projectTasks|Where-Object{$_.Findings.Count -gt 0}).Count){'DRIFT'}else{'CLEAN'}}else{'SKIP'}
 [pscustomobject]@{Status=$status;Tasks=$projectTasks.ToArray();OwnerOnlyMaintenance=$ownerTasks.ToArray()}
}
function Invoke-WslAgentDrift {
 param([string]$Name)
 $wsl=Join-Path $env:SystemRoot 'System32\wsl.exe'
 $r=Invoke-BoundedNative $wsl @('-d',$Name,'-u','agent','--','python3','/opt/machine-bootstrap/current/checks/agent-drift.py') 30000 32768
 if($r.Status -ne 'Success'){return [pscustomobject]@{Status='UNAVAILABLE';Findings=@('drift-probe-unavailable')}}
 try{
  $d=$r.Output|ConvertFrom-Json
  if($d.schemaVersion -ne 1 -or $d.status -notin @('CLEAN','DRIFT') -or $null -eq $d.findings){throw 'invalid'}
  $codes=@()
  foreach($finding in @($d.findings)){
   if([string]$finding -match '^[A-Za-z0-9:@._-]+$'){$codes+=([string]$finding)}
  }
  if($d.status -eq 'DRIFT' -and -not $codes.Count){throw 'invalid'}
  [pscustomobject]@{Status=[string]$d.status;Findings=@($codes|Select-Object -First 100)}
 }catch{[pscustomobject]@{Status='UNAVAILABLE';Findings=@('invalid-drift-probe-output')}}
}

function Invoke-WslGhTokenExpiry {
 param([string]$Name)
 if(-not [string]::Equals($Name,'AgentDev','OrdinalIgnoreCase')){return [pscustomobject]@{Status='SKIP';ExpiresAtISO=$null}}
 $wsl=Join-Path $env:SystemRoot 'System32\wsl.exe'
 $lr=Invoke-BoundedNative $wsl @('--list','--verbose') 10000
 if($lr.Status -ne 'Success'){return [pscustomobject]@{Status='SKIP';ExpiresAtISO=$null}}
 try{
  $current=@(ConvertFrom-WslList $lr.Output|Where-Object{[string]::Equals($_.Name,'AgentDev','OrdinalIgnoreCase')})
  if($current.Count -ne 1 -or $current[0].State -ne 'Running' -or $current[0].WslVersion -ne 2){return [pscustomobject]@{Status='SKIP';ExpiresAtISO=$null}}
 }catch{return [pscustomobject]@{Status='SKIP';ExpiresAtISO=$null}}
 $r=Invoke-BoundedNative $wsl @('-d','AgentDev','-u','agent','--','python3','-c',$script:GhTokenExpiryPython) 25000 4096
 if($r.Status -ne 'Success'){return [pscustomobject]@{Status='UNAVAILABLE';ExpiresAtISO=$null}}
 try{
  $d=$r.Output|ConvertFrom-Json
  if($d.schemaVersion -ne 1 -or $d.status -notin @('AVAILABLE','UNKNOWN','UNAVAILABLE')){throw 'invalid'}
  if($d.status -ne 'AVAILABLE'){return [pscustomobject]@{Status=[string]$d.status;ExpiresAtISO=$null}}
  $raw=[string]$d.expiresAtISO
  $parsed=[datetimeoffset]::MinValue
  if($raw -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$' -or -not [datetimeoffset]::TryParseExact($raw,'yyyy-MM-ddTHH:mm:ssZ',[globalization.cultureinfo]::InvariantCulture,[globalization.datetimestyles]::AssumeUniversal,[ref]$parsed)){throw 'invalid'}
  [pscustomobject]@{Status='AVAILABLE';ExpiresAtISO=$parsed.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")}
 }catch{[pscustomobject]@{Status='UNAVAILABLE';ExpiresAtISO=$null}}
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
  $e=[ordered]@{Name=[string]$disk.Name;State=$state;WslVersion=$ver;Vhdx=[string]$disk.Vhdx;FileBytes=$disk.FileBytes;UsedBytes=$null;CapBytes=$null;LargestDirectories=@();DirectoryErrors=@();Status='Unavailable';UsageSource='Unavailable';UsageRecordedAtISO=$null;CacheRecordDate=$null;AgentDriftStatus='UNAVAILABLE';AgentDriftFindings=@();GhTokenExpiryStatus='SKIP';GhTokenExpiresAtISO=$null;GhTokenExpiryRecordedAtISO=$null;GhTokenExpiryWarning=$null}
  if($state -eq 'Running' -and $ver -eq 2 -and $disk.WslVersion -eq 2){
   $p=Invoke-WslInventoryProbe $disk.Name;$e.Status=$p.Status;$e.DirectoryErrors=@($p.Errors)
   if(($p.Status -in @('Success','Partial')) -and $null -ne $p.Usage){$e.UsedBytes=[long]$p.Usage.usedBytes;$e.CapBytes=[long]$p.Usage.capBytes;$e.LargestDirectories=@($p.Largest);if($p.Status -eq 'Success'){$e.Status='Success'}else{$e.Status='Partial'};$e.UsageSource='Live';$e.UsageRecordedAtISO=$CurrentTime.ToString('o')}
   $ad=Invoke-WslAgentDrift $disk.Name;$e.AgentDriftStatus=$ad.Status;$e.AgentDriftFindings=@($ad.Findings)
   if([string]::Equals([string]$disk.Name,'AgentDev','OrdinalIgnoreCase')){
    $expiry=Invoke-WslGhTokenExpiry $disk.Name;$e.GhTokenExpiryStatus=$expiry.Status
    if($expiry.Status -ne 'SKIP'){$e.GhTokenExpiryRecordedAtISO=$CurrentTime.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')}
    if($expiry.Status -eq 'AVAILABLE' -and $expiry.ExpiresAtISO){
     $e.GhTokenExpiresAtISO=$expiry.ExpiresAtISO
     $expiryAt=[datetimeoffset]::ParseExact($expiry.ExpiresAtISO,'yyyy-MM-ddTHH:mm:ssZ',[globalization.cultureinfo]::InvariantCulture,[globalization.datetimestyles]::AssumeUniversal)
     if($expiryAt -le ([datetimeoffset]$CurrentTime.ToUniversalTime()).AddDays(14)){$e.GhTokenExpiryWarning='expires-within-14-days'}
    }
   }
  }elseif($state -eq 'Stopped' -and $ver -eq 2 -and $disk.WslVersion -eq 2){
   $e.AgentDriftStatus='SKIP';$e.AgentDriftFindings=@('distro-stopped')
   $c=Get-LatestCachedDistro $OutputDirectory $disk.Name $disk.Vhdx $CurrentTime
   if($c){$e.UsedBytes=[long]$c.Distro.usedBytes;$cp=$c.Distro.PSObject.Properties['capBytes'];if($null -ne $cp -and $null -ne $cp.Value){$e.CapBytes=[long]$cp.Value};$dp=$c.Distro.PSObject.Properties['largestDirectories'];if($null -ne $dp){$e.LargestDirectories=@($dp.Value)};$ep=$c.Distro.PSObject.Properties['directoryErrors'];if($null -ne $ep){$e.DirectoryErrors=@($ep.Value)};$e.Status='Cached';$e.UsageSource='Cache';$e.UsageRecordedAtISO=[string]$c.Distro.usageRecordedAtISO;$e.CacheRecordDate=$c.At.ToString('o')}else{$e.Status='StoppedNoCache'}
  }elseif($ver -ne 2 -or $disk.WslVersion -ne 2){$e.Status='UnsupportedWslVersion';$e.AgentDriftStatus='SKIP';$e.AgentDriftFindings=@('unsupported-wsl-version')}elseif($state -eq 'Unknown'){$e.Status='StateUnavailable';$e.AgentDriftStatus='UNAVAILABLE';$e.AgentDriftFindings=@('distro-state-unavailable')}
  $distros.Add([pscustomobject]$e)
 }
 $drives=[ordered]@{}
 foreach($l in 'C','D'){$d=Get-PSDrive -Name $l -PSProvider FileSystem -ErrorAction SilentlyContinue;$drives[$l]=[pscustomobject]@{FreeBytes=if($d){[long]$d.Free}else{$null};Status=if($d){'Available'}else{'Unavailable'}}}
 $vr=Invoke-BoundedNative $wsl @('--version') 10000;$wv=$null;if($vr.Status -eq 'Success' -and $vr.Output -match '(?im)^WSL version:\s*([0-9A-Za-z.-]+)\s*$'){$wv=$Matches[1]}
 $nr=Invoke-BoundedNative 'nvidia-smi.exe' @('--query-gpu=driver_version','--format=csv,noheader') 10000 8192;$nv=@()
 if($nr.Status -eq 'Success'){$nv=@($nr.Output -split '\r?\n'|%{$_.Trim()}|?{$_ -match '^\d{1,4}\.\d{1,4}(?:\.\d{1,4})?$'}|select -Unique)}
 $reboot=[ordered]@{ComponentBasedServicing=(Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending');WindowsUpdate=(Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired');PendingFileRenameOperations=$false}
 try{$reboot.PendingFileRenameOperations=$null -ne (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations -ErrorAction Stop).PendingFileRenameOperations}catch{}
 $taskReview=Get-ScheduledTaskReview;$tasks=@($taskReview.OwnerOnlyMaintenance|Sort-Object Name)
 [pscustomobject]@{schemaVersion=2;createdAtISO=$CurrentTime.ToString('o');wslVersion=$wv;wslStatus=$ws;nvidiaDriverVersions=$nv;nvidiaStatus=$nr.Status;drives=$drives;pendingReboot=$reboot;taskStates=$tasks;scheduledTaskReviewStatus=$taskReview.Status;scheduledProjectTasks=@($taskReview.Tasks);ownerOnlyMaintenanceTasks=$tasks;distros=$distros.ToArray()}
}
function Write-AtomicInventoryJson {
 param([string]$Path,[string]$Json)
 $tmp=Join-Path (Split-Path -Parent $Path) ('.inventory-'+[guid]::NewGuid().ToString('N')+'.tmp')
 try{[IO.File]::WriteAllText($tmp,$Json,[text.UTF8Encoding]::new($false));if([IO.File]::Exists($Path)){[IO.File]::Replace($tmp,$Path,[System.Management.Automation.Language.NullString]::Value)}else{[IO.File]::Move($tmp,$Path)}}finally{if([IO.File]::Exists($tmp)){[IO.File]::Delete($tmp)}}
}
$script:InventoryMirrorPython=@'
import os,pwd,re,stat,sys

BASE="/home/agent/project-data"

def main(mode,name):
    if mode not in ("prepare","publish","cleanup") or not re.fullmatch(r"[.]inventory-[a-f0-9]{32}[.]tmp",name):
        raise ValueError("invalid mirror operation")
    uid=pwd.getpwnam("agent").pw_uid
    if os.getuid()!=uid:
        raise PermissionError("agent required")
    base=os.open(BASE,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    directory=None
    try:
        if mode=="prepare":
            try:
                os.mkdir("inventory",0o700,dir_fd=base)
            except FileExistsError:
                pass
        directory=os.open("inventory",os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=base)
        if os.fstat(directory).st_uid!=uid:
            raise PermissionError("inventory directory owner")
        os.fchmod(directory,0o700)
        if mode=="prepare":
            fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
            try:
                os.fchmod(fd,0o600)
            finally:
                os.close(fd)
        else:
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            try:
                info=os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid!=uid or info.st_nlink!=1:
                    raise PermissionError("inventory staging file")
                os.fchmod(fd,0o600)
            finally:
                os.close(fd)
            if mode=="publish":
                os.replace(name,"latest.json",src_dir_fd=directory,dst_dir_fd=directory)
            else:
                os.unlink(name,dir_fd=directory)
    finally:
        if directory is not None:
            os.close(directory)
        os.close(base)

if __name__=="__main__":
    main(sys.argv[1],sys.argv[2])
'@

function Test-AgentDevInventoryRunning {
 $wsl=Join-Path $env:SystemRoot 'System32\wsl.exe'
 $r=Invoke-BoundedNative $wsl @('--list','--verbose') 10000
 if($r.Status -ne 'Success'){return $false}
 try{
  $current=@(ConvertFrom-WslList $r.Output|Where-Object{[string]::Equals($_.Name,'AgentDev','OrdinalIgnoreCase')})
  return $current.Count -eq 1 -and $current[0].State -eq 'Running' -and $current[0].WslVersion -eq 2
 }catch{return $false}
}
function Write-AgentDevInventoryStagingJson {
 param([string]$Path,[string]$Json)
 # prepare created this private agent-owned inode; writing it preserves mode.
 [IO.File]::WriteAllText($Path,$Json,[text.UTF8Encoding]::new($false))
}
function Copy-AgentDevInventory {
 param([string]$Json)
 # Founder requires a strict no-start guarantee, including check/use races.
 # The prototype below uses launch-capable interfaces; keep it unreachable.
 return 'SKIP-NONSTARTING-TRANSPORT-REQUIRED'
}
function Copy-AgentDevInventoryPrototype {
 param([string]$Json)
 $name='.inventory-'+[guid]::NewGuid().ToString('N')+'.tmp'
 $wsl=Join-Path $env:SystemRoot 'System32\wsl.exe'
 $prepared=$false
 try{
  # UNC access and distro commands must never be attempted for a stopped,
  # absent, unsupported or unobservable AgentDev, including state changes.
  if(-not(Test-AgentDevInventoryRunning)){return 'SKIP'}
  $r=Invoke-BoundedNative $wsl @('-d','AgentDev','-u','agent','--','python3','-c',$script:InventoryMirrorPython,'prepare',$name) 10000 4096
  if($r.Status -ne 'Success'){return 'UNAVAILABLE'}
  $prepared=$true
  if(-not(Test-AgentDevInventoryRunning)){return 'SKIP'}
  $path='\\wsl.localhost\AgentDev\home\agent\project-data\inventory\'+$name
  Write-AgentDevInventoryStagingJson -Path $path -Json $Json
  if(-not(Test-AgentDevInventoryRunning)){return 'SKIP'}
  $r=Invoke-BoundedNative $wsl @('-d','AgentDev','-u','agent','--','python3','-c',$script:InventoryMirrorPython,'publish',$name) 10000 4096
  if($r.Status -ne 'Success'){return 'UNAVAILABLE'}
  $prepared=$false
  return 'SUCCESS'
 }catch{return 'UNAVAILABLE'}
 finally{
  # Best-effort removal also has a fresh running gate; never wake a distro
  # just to clean up its private, randomly named staging file.
  try{if($prepared -and (Test-AgentDevInventoryRunning)){
   $null=Invoke-BoundedNative $wsl @('-d','AgentDev','-u','agent','--','python3','-c',$script:InventoryMirrorPython,'cleanup',$name) 10000 4096
  }}catch{}
 }
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
 Write-Output ('AgentDev inventory copy: '+(Copy-AgentDevInventory -Json $json))
 foreach($d in @($v.distros|Where-Object{$_.GhTokenExpiryWarning -eq 'expires-within-14-days'})){
  Write-Output ('WARNING: AgentDev gh token expires within 14 days: '+$d.GhTokenExpiresAtISO)
 }
}
if($MyInvocation.InvocationName -ne '.') {Invoke-MachineInventory -OutputDirectory $OutputDirectory -At $Now}
