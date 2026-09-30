# Minimum supported WSL 2.5.0; run from PowerShell on Windows, amd64.
[CmdletBinding()]
param([Parameter(Mandatory)][ValidatePattern('^[A-Za-z][A-Za-z0-9_-]{0,47}$')][string]$Name,
      [ValidateRange(1, 65536)][int]$MaxSizeGB=800)
. "$PSScriptRoot/common.ps1"
Assert-WslVersion
if ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne 'X64') {
    throw 'The pinned Ubuntu image supports x64 Windows only.'
}
$location = [IO.Path]::GetFullPath("D:\wsl\$Name")
$expected = 'bb415d824822c4b878125729af451a5d18fb13d1cf5cbed9a7393ad64ac6039e'
$marker = Join-Path $location '.bootstrap-image.sha256'
$existing = @(Get-WslDisks | Where-Object Name -eq $Name)
if ($existing.Count) {
    $existingPath = $existing[0].BasePath
    if ([IO.Path]::GetFullPath($existingPath).TrimEnd('\') -ne $location -or $existing[0].WslVersion -ne 2) {
        throw "Existing distro $Name is not WSL 2 at $location; refusing to change it."
    }
    if (-not (Test-Path -LiteralPath $marker) -or (Get-Content -LiteralPath $marker -Raw).Trim() -ne $expected) {
        throw "Existing distro $Name was not created from the pinned Ubuntu image by this script."
    }
} else {
    if (Test-Path -LiteralPath $location) { throw "Target already exists: $location" }
    if (-not (Test-Path -LiteralPath 'D:\')) { throw 'D: drive is required.' }
    $cache = Join-Path $env:LOCALAPPDATA 'machine-bootstrap\images'
    New-Item -ItemType Directory -Force -Path $cache | Out-Null
    $image = Join-Path $cache 'ubuntu-24.04.5-wsl-amd64.wsl'
    if (-not (Test-Path -LiteralPath $image)) {
        Invoke-WebRequest -Uri 'https://releases.ubuntu.com/24.04/ubuntu-24.04.5-wsl-amd64.wsl' -OutFile $image
    }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $image).Hash.ToLowerInvariant() -ne $expected) {
        throw "Ubuntu image checksum mismatch: $image. Remove that cached image and retry."
    }
    # Microsoft documents --from-file and --name for modern distros, and
    # --location / --no-launch in its Basic commands reference.
    Set-WslVhdCap -MaxSizeGB $MaxSizeGB
    Invoke-Wsl -WslArgs @('--install', '--from-file', $image, '--name', $Name, '--location', $location, '--no-launch')
    $installed = @(Get-WslDisks | Where-Object Name -eq $Name)
    if ($installed.Count -ne 1) { throw "Installed distro $Name could not be read back." }
    if ($installed[0].WslVersion -ne 2) {
        Invoke-Wsl -WslArgs @('--set-version', $Name, '2')
    }
    Set-Content -LiteralPath $marker -Value $expected -Encoding ascii -NoNewline
}
Get-CompactableWslDisk -Name $Name | Out-Null
$usage = Assert-WslCap -Name $Name -MaxSizeGB $MaxSizeGB
Invoke-Wsl -WslArgs @('--terminate', $Name)
Write-Output "Installed $Name at $location; filesystem cap read-back $($usage.CapBytes) bytes (limit ${MaxSizeGB}GB). Sparse mode is off."
Write-Output "Next: wsl -d $Name (create the non-root admin user; do not name it agent)."
