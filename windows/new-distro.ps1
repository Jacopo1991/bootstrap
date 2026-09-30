# Minimum supported WSL 2.5.0; run from PowerShell on Windows, amd64.
[CmdletBinding()]
param([Parameter(Mandatory)][ValidatePattern('^[A-Za-z][A-Za-z0-9_-]{0,47}$')][string]$Name)
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
    if ($existingPath.StartsWith('\\?\')) { $existingPath = $existingPath.Substring(4) }
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
    Invoke-Wsl -WslArgs @('--install', '--from-file', $image, '--name', $Name, '--location', $location, '--no-launch')
    Invoke-Wsl -WslArgs @('--set-version', $Name, '2')
    Set-Content -LiteralPath $marker -Value $expected -Encoding ascii -NoNewline
}
Invoke-Wsl -WslArgs @('--terminate', $Name)
Invoke-Wsl -WslArgs @('--manage', $Name, '--set-sparse', 'true')
Write-Output "Installed $Name at $location; sparse mode enabled."
Write-Output "Next: wsl -d $Name (create the non-root admin user; do not name it agent)."
