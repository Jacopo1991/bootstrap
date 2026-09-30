Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-Wsl {
    param([Parameter(Mandatory)][string[]]$WslArgs)
    & wsl.exe @WslArgs
    if ($LASTEXITCODE -ne 0) { throw "wsl.exe failed ($LASTEXITCODE): $($WslArgs -join ' ')" }
}

function Assert-WslVersion {
    $text = ((& wsl.exe --version) -join "`n").Replace([string][char]0, '')
    if ($LASTEXITCODE -ne 0 -or $text -notmatch '(\d+\.\d+\.\d+(?:\.\d+)?)') {
        throw 'A separately serviced WSL installation with wsl --version is required.'
    }
    if ([version]$Matches[1] -lt [version]'2.5.0') {
        throw 'Minimum supported WSL version: 2.5.0. Update WSL before continuing.'
    }
    $help = ((& wsl.exe --help) -join "`n").Replace([string][char]0, '')
    foreach ($flag in '--from-file', '--location', '--name') {
        if (-not $help.Contains($flag)) { throw "Installed WSL lacks required flag $flag." }
    }
}

if (-not ('BootstrapDiskSize' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class BootstrapDiskSize {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern uint GetCompressedFileSizeW(string file, out uint high);
    public static ulong Allocated(string file) {
        uint high;
        uint low = GetCompressedFileSizeW(file, out high);
        int error = Marshal.GetLastWin32Error();
        if (low == UInt32.MaxValue && error != 0)
            throw new System.ComponentModel.Win32Exception(error);
        return ((ulong)high << 32) | low;
    }
}
'@
}

function ConvertTo-WslBasePath {
    param([AllowNull()][AllowEmptyString()][string]$BasePath)
    if ([string]::IsNullOrWhiteSpace($BasePath)) { return }
    $path = [Environment]::ExpandEnvironmentVariables($BasePath)
    if ($path.StartsWith('\\?\UNC\', [StringComparison]::OrdinalIgnoreCase)) {
        $path = '\\' + $path.Substring(8)
    } elseif ($path.StartsWith('\\?\', [StringComparison]::OrdinalIgnoreCase)) {
        $path = $path.Substring(4)
    }
    return [IO.Path]::GetFullPath($path)
}

function Get-WslDisks {
    $registry = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss'
    if (-not (Test-Path -LiteralPath $registry)) { return }
    foreach ($key in Get-ChildItem -LiteralPath $registry) {
        $distro = Get-ItemProperty -LiteralPath $key.PSPath
        $baseProperty = $distro.PSObject.Properties['BasePath']
        $basePath = ConvertTo-WslBasePath -BasePath $(if ($null -ne $baseProperty) { $baseProperty.Value })
        if ($null -eq $basePath) {
            Write-Warning "Skipping WSL registry entry $($key.PSPath): missing BasePath."
            continue
        }
        $diskName = if ($distro.PSObject.Properties.Name -contains 'VhdFileName') {
            $distro.VhdFileName
        } else { 'ext4.vhdx' }
        $vhd = Join-Path $basePath $diskName
        $exists = Test-Path -LiteralPath $vhd
        [pscustomobject]@{
            Name = $distro.DistributionName
            WslVersion = $distro.Version
            BasePath = $basePath
            Vhdx = $vhd
            FileBytes = if ($exists) { (Get-Item -LiteralPath $vhd).Length } else { $null }
            AllocatedBytes = if ($exists) { [BootstrapDiskSize]::Allocated($vhd) } else { $null }
            IsSparse = if ($exists) { [bool]((Get-Item -LiteralPath $vhd).Attributes -band [IO.FileAttributes]::SparseFile) } else { $false }
        }
    }
}

function Merge-WslVhdCap {
    param([AllowEmptyString()][string]$Text, [ValidateRange(1, 65536)][int]$MaxSizeGB)
    # Do not inherit an existing opt-in to unsafe sparse creation.
    if ($Text -match '(?im)^\s*sparseVhd\s*=\s*true\s*(?:[#;].*)?$') {
        throw 'Existing .wslconfig enables sparseVhd. Disable it before creating a distro.'
    }
    $lines = [System.Collections.Generic.List[string]]::new()
    $inWsl2 = $false
    $foundSection = $false
    $written = $false
    foreach ($line in ($Text -split '\r?\n')) {
        if ($line -match '^\s*\[([^]]+)\]\s*(?:[#;].*)?$') {
            if ($inWsl2 -and -not $written) { $lines.Add("defaultVhdSize=${MaxSizeGB}GB"); $written=$true }
            $inWsl2 = $Matches[1] -ieq 'wsl2'
            if ($inWsl2) { $foundSection=$true }
        }
        if ($inWsl2 -and $line -match '^\s*defaultVhdSize\s*=') {
            if (-not $written) { $lines.Add("defaultVhdSize=${MaxSizeGB}GB"); $written=$true }
        } else { $lines.Add($line) }
    }
    if (-not $foundSection) { $lines.Add('[wsl2]') }
    if (-not $written) { $lines.Add("defaultVhdSize=${MaxSizeGB}GB") }
    return ($lines -join "`r`n").TrimEnd("`r", "`n") + "`r`n"
}

function Set-WslVhdCap {
    param([ValidateRange(1, 65536)][int]$MaxSizeGB,
          [string]$Path=(Join-Path $env:USERPROFILE '.wslconfig'))
    $exists = Test-Path -LiteralPath $path
    $text = if ($exists) { Get-Content -LiteralPath $path -Raw } else { '' }
    $merged = Merge-WslVhdCap -Text $text -MaxSizeGB $MaxSizeGB
    if ($merged -ceq $text) { return }
    if ($exists) {
        $backup = $path + '.bootstrap-' + [Guid]::NewGuid().ToString('N') + '.bak'
        Copy-Item -LiteralPath $path -Destination $backup
        Write-Output "Backed up .wslconfig: $backup"
    }
    [IO.File]::WriteAllText($path, $merged, [Text.UTF8Encoding]::new($false))
    Write-Output "Configured defaultVhdSize=${MaxSizeGB}GB in $path"
}

function Get-WslFilesystemUsage {
    param([string]$Name)
    $text = ((Invoke-Wsl -WslArgs @('-d', $Name, '-u', 'root', '--', 'df', '-B1', '--output=size,used', '/')) -join "`n").Replace([string][char]0, '')
    if ($text -notmatch '(?m)^\s*(\d+)\s+(\d+)\s*$') { throw "Cannot read filesystem size for $Name." }
    [pscustomobject]@{ CapBytes=[long]$Matches[1]; UsedBytes=[long]$Matches[2] }
}

function Assert-WslCap {
    param([string]$Name, [ValidateRange(1, 65536)][int]$MaxSizeGB)
    $usage = Get-WslFilesystemUsage -Name $Name
    if ($usage.CapBytes -gt ([long]$MaxSizeGB * 1GB)) { throw "Filesystem size $($usage.CapBytes) exceeds ${MaxSizeGB}GB cap for $Name." }
    return $usage
}

function Assert-BootstrapAdministrator {
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run in elevated PowerShell as the Windows account that owns this distro.' }
}

function Get-CompactableWslDisk {
    param([string]$Name)
    $disks = @(Get-WslDisks | Where-Object Name -eq $Name)
    if ($disks.Count -ne 1 -or $disks[0].WslVersion -ne 2) { throw 'Select an existing WSL 2 distro.' }
    if ($disks[0].IsSparse) { throw 'Refusing to compact a sparse VHDX.' }
    if (-not (Test-Path -LiteralPath $disks[0].Vhdx -PathType Leaf)) { throw 'Distro VHDX is missing.' }
    return $disks[0]
}

function Get-OtherRunningWslDistros {
    param([Parameter(Mandatory)][string]$Name)
    $lines = @(& wsl.exe --list --verbose 2>&1)
    $code = $LASTEXITCODE
    if ($code -ne 0) { throw "wsl.exe failed ($code): --list --verbose" }
    $text = (($lines -join "`n").Replace([string][char]0, ''))
    $hasHeader = $false
    $running = [System.Collections.Generic.List[string]]::new()
    foreach ($line in ($text -split "`r?`n")) {
        if ($line -match '^\s*NAME\s+STATE\s+VERSION\s*$') { $hasHeader = $true; continue }
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if (-not $hasHeader) { throw 'Could not parse output from wsl --list --verbose.' }
        if ($line -notmatch '^\s*(?:\*\s*)?(.+?)\s+(Running|Stopped)\s+\d+\s*$') {
            throw 'Could not parse a distro row from wsl --list --verbose.'
        }
        $distroName = $Matches[1].Trim()
        $state = $Matches[2]
        if ($state -eq 'Running' -and -not [string]::Equals($distroName, $Name, [StringComparison]::OrdinalIgnoreCase)) {
            $running.Add($distroName)
        }
    }
    if (-not $hasHeader) { throw 'Could not parse output from wsl --list --verbose.' }
    return $running.ToArray()
}

function Test-WslBusy {
    param([string]$Name)
    # Unknown processes are busy. Only root/system-account OS daemons, WSL init,
    # kernel threads and this observer are allowed; user processes always block.
    $probe = @'
import os, pathlib
allowed = {'systemd', 'systemd-journald', 'systemd-udevd', 'systemd-networkd', 'systemd-resolved', 'systemd-logind', 'systemd-timesyncd', 'dbus-daemon', 'dbus-broker', 'dbus-broker-launch', 'cron', 'rsyslogd', 'agetty', 'sshd', 'polkitd', 'snapd', 'snapfuse', 'accounts-daemon', 'upowerd', 'ModemManager', 'NetworkManager', 'wsl-pro-service'}
scripts = {'/usr/bin/networkd-dispatcher', '/usr/share/unattended-upgrades/unattended-upgrade-shutdown'}
busy = False
for p in pathlib.Path('/proc').iterdir():
    if not p.name.isdigit() or int(p.name) == os.getpid(): continue
    try:
        uid = p.stat().st_uid
        cmd = (p / 'cmdline').read_bytes().split(b'\0')
        exe = os.readlink(p / 'exe')
    except FileNotFoundError:
        continue
    except PermissionError:
        busy = True; continue
    if uid < 1000 and (exe == '/init' or (exe.startswith('/usr/') and (pathlib.Path(exe).name in allowed or any(a.decode(errors='replace') in scripts for a in cmd[1:2])))): continue
    busy = True
print('BUSY' if busy else 'IDLE')
'@
    $answer = ((Invoke-Wsl -WslArgs @('-d', $Name, '-u', 'root', '--', 'python3', '-c', $probe)) -join '').Trim()
    if ($answer -notin 'BUSY', 'IDLE') { throw 'Idle process probe returned an unexpected result.' }
    return $answer -eq 'BUSY'
}

function Wait-WslVhdDetached {
    param([string]$Vhdx, [int]$TimeoutSeconds=300)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $stream = $null
        try {
            $stream = [IO.File]::Open($Vhdx, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
            return
        } catch [IO.IOException] {
            # Only sharing/lock violations mean another opener still owns the VHDX.
            if (($_.Exception.HResult -band 0xFFFF) -notin 32, 33) { throw }
        } finally {
            if ($null -ne $stream) { $stream.Dispose() }
        }
        if ((Get-Date) -ge $deadline) { break }
        Start-Sleep -Seconds 2
    } while ($true)
    throw "Timed out waiting for exclusive read access to $Vhdx. No compaction attempted."
}

function Invoke-BootstrapDiskPart {
    param([string[]]$Commands)
    if (($Commands -join "`n") -match '[^\x00-\x7F]') { throw 'DiskPart commands must contain only ASCII characters.' }
    $scriptFile = Join-Path $env:TEMP ('bootstrap-diskpart-' + [Guid]::NewGuid().ToString('N') + '.txt')
    try {
        Set-Content -LiteralPath $scriptFile -Value ($Commands -join "`r`n") -Encoding ascii
        $output = & diskpart.exe /s $scriptFile 2>&1
        $code = $LASTEXITCODE
        Write-Output $output
        if ($code -ne 0) { throw "diskpart failed ($code)." }
    } finally { Remove-Item -LiteralPath $scriptFile -Force }
}

function Invoke-WslCompaction {
    param([string]$Name, [switch]$IfIdle)
    Assert-BootstrapAdministrator
    $otherRunning = @(Get-OtherRunningWslDistros -Name $Name)
    if ($otherRunning.Count -gt 0) {
        $names = $otherRunning -join ', '
        if ($IfIdle) { Write-Output "SKIP: WSL VM held by $names"; return }
        throw "WSL VM held by $names; cannot compact $Name."
    }
    $disk = Get-CompactableWslDisk -Name $Name
    if ($disk.Vhdx -match '[^\x00-\x7F]') { throw 'VHDX path contains non-ASCII characters; diskpart script requires an ASCII path.' }
    if ($IfIdle -and (Test-WslBusy -Name $Name)) { Write-Output "SKIP: $Name has non-system processes."; return }
    $before = (Get-Item -LiteralPath $disk.Vhdx).Length
    Write-Output "BEFORE: $Name FileBytes=$before"
    Invoke-Wsl -WslArgs @('-d', $Name, '-u', 'root', '--', 'fstrim', '/')
    if ($IfIdle -and (Test-WslBusy -Name $Name)) { Write-Output "SKIP: $Name became busy before termination."; return }
    Invoke-Wsl -WslArgs @('--terminate', $Name)
    Wait-WslVhdDetached -Vhdx $disk.Vhdx
    if ($disk.Vhdx.Contains('"') -or $disk.Vhdx -match '[\r\n]') { throw 'Invalid VHDX path for diskpart.' }
    $select = 'select vdisk file="' + $disk.Vhdx + '"'
    try {
        Invoke-BootstrapDiskPart -Commands @($select, 'attach vdisk readonly', 'compact vdisk', 'detach vdisk')
    } finally {
        if ((Get-DiskImage -ImagePath $disk.Vhdx).Attached) {
            # Microsoft requires 15 seconds between successive diskpart scripts.
            Start-Sleep -Seconds 15
            Invoke-BootstrapDiskPart -Commands @($select, 'detach vdisk')
        }
    }
    Wait-WslVhdDetached -Vhdx $disk.Vhdx
    $after = (Get-Item -LiteralPath $disk.Vhdx).Length
    Write-Output "COMPACT: $Name FileBytes=$before -> $after ReclaimedBytes=$($before-$after)"
}
