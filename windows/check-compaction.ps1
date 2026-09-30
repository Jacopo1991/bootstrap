[CmdletBinding()]
param([Parameter(Mandatory)][ValidatePattern('^[A-Za-z][A-Za-z0-9_-]{0,47}$')][string]$Name,
      [string]$LogPath='C:\ProgramData\machine-bootstrap\compact.log',
      [string]$InventoryDirectory=(Join-Path $PSScriptRoot 'inventory'))
. "$PSScriptRoot/common.ps1"
. "$PSScriptRoot/inventory.ps1"

function Get-WslDistroState {
 param([Parameter(Mandatory)][string]$Name)
 $result=Invoke-BoundedNative -FilePath (Join-Path $env:SystemRoot 'System32\wsl.exe') -Arguments @('--list','--verbose') -TimeoutMilliseconds 10000
 if($result.Status -ne 'Success'){throw 'Could not read WSL distro states.'}
 try{$items=@(ConvertFrom-WslList -Text $result.Output)}catch{throw 'Could not parse WSL distro states.'}
 $found=@($items|Where-Object{[string]::Equals($_.Name,$Name,[StringComparison]::OrdinalIgnoreCase)})
 if($found.Count -gt 1){throw "Multiple WSL list entries matched $Name."}
 if($found.Count -eq 0){return}
 [pscustomobject]@{State=$found[0].State;Version=$found[0].WslVersion}
}
function Invoke-WslCompactionCheck {
 [CmdletBinding()]
 param([Parameter(Mandatory)][string]$Name,[string]$LogPath='C:\ProgramData\machine-bootstrap\compact.log',[string]$InventoryDirectory=(Join-Path $PSScriptRoot 'inventory'))
 $disks=@(Get-WslDisks|Where-Object{[string]::Equals($_.Name,$Name,[StringComparison]::OrdinalIgnoreCase)})
 if($disks.Count -ne 1){throw "Expected one VHDX record for $Name; found $($disks.Count)."}
 $disk=$disks[0];$stateRecord=Get-WslDistroState -Name $Name
 $logDirectory=Split-Path -Parent $LogPath;if($logDirectory){New-Item -ItemType Directory -Force -Path $logDirectory|Out-Null}
 $running=$null -ne $stateRecord -and $stateRecord.State -eq 'Running' -and $stateRecord.Version -eq 2 -and $disk.WslVersion -eq 2
 $state=if($null -eq $stateRecord){'Unknown'}else{$stateRecord.State};$fileBytes=if($null -eq $disk.FileBytes){'unknown'}else{[string]$disk.FileBytes}
 $baseLine="CHECK: $($disk.Name) State=$state FileBytes=$fileBytes";Add-Content -LiteralPath $LogPath -Value $baseLine -Encoding utf8;Write-Output $baseLine
 $usage=$null;$source=$null;$recordedAt=$null
 if($running){
  $native=Invoke-BoundedNative -FilePath (Join-Path $env:SystemRoot 'System32\wsl.exe') -Arguments @('-d',$disk.Name,'-u','root','--','df','-B1','--output=size,used','/') -TimeoutMilliseconds 10000
  if($native.Status -ne 'Success'){throw 'Could not read filesystem usage.'}
  $text=$native.Output
  if($text -notmatch '(?m)^\s*(\d+)\s+(\d+)\s*$'){throw 'Cannot read filesystem usage.'}
  $usage=[pscustomobject]@{CapBytes=[long]$Matches[1];UsedBytes=[long]$Matches[2]};$source='Live';$recordedAt=(Get-Date).ToString('o')
 }elseif($stateRecord -and $stateRecord.State -eq 'Stopped' -and $stateRecord.Version -eq 2 -and $disk.WslVersion -eq 2){
  $cached=Get-LatestCachedDistro -Directory $InventoryDirectory -Name $disk.Name -Vhdx $disk.Vhdx -CurrentDate (Get-Date)
  if($cached){$usage=[pscustomobject]@{CapBytes=$cached.Distro.capBytes;UsedBytes=$cached.Distro.usedBytes};$source='Cache';$recordedAt=[string]$cached.Distro.usageRecordedAtISO}
 }
 if($null -eq $usage){return}
 $used=[long]$usage.UsedBytes;$cap=if($null -ne $usage.CapBytes){[long]$usage.CapBytes}else{$null};$capText=if($null -eq $cap){'unknown'}else{[string]$cap}
 $metrics="USAGE: $($disk.Name) FileBytes=$fileBytes UsedBytes=$used CapBytes=$capText Source=$source RecordedAt=$recordedAt";Add-Content -LiteralPath $LogPath -Value $metrics -Encoding utf8;Write-Output $metrics
 if($null -eq $disk.FileBytes){return}
 $gap=[long]$disk.FileBytes-$used
 if($gap -gt 50GB -or ($null -ne $cap -and [double]$disk.FileBytes -gt ([double]$cap*0.9))){
  $recommendation="COMPACTION RECOMMENDED: run windows\compact-distro.ps1 -Name $($disk.Name)";Add-Content -LiteralPath $LogPath -Value $recommendation -Encoding utf8;Write-Output $recommendation
 }
}
if($MyInvocation.InvocationName -ne '.') {Invoke-WslCompactionCheck -Name $Name -LogPath $LogPath -InventoryDirectory $InventoryDirectory}
