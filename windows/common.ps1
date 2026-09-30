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
    foreach ($flag in '--from-file', '--location', '--name', '--set-sparse') {
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
        }
    }
}
