
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) {
    throw 'These regression tests must run in Windows PowerShell 5.1.'
}

function Assert-True {
    param([bool]$Condition, [string]$Label)
    if (-not $Condition) { throw ('FAIL: ' + $Label) }
    Write-Output "PASS: $Label"
}

function Assert-Throws {
    param([scriptblock]$Action, [string]$Pattern, [string]$Label)
    $message = $null
    try { & $Action | Out-Null } catch { $message = $_.Exception.Message }
    if ($null -eq $message -or $message -notlike $Pattern) { throw ($Label + ': expected ' + $Pattern + ', got ' + $message) }
    Write-Output "PASS: $Label"
}

$testRoot = Join-Path $env:TEMP ('bootstrap-vscode-ssh-' + [Guid]::NewGuid().ToString('N'))
$profile = Join-Path $testRoot 'profile'
[IO.Directory]::CreateDirectory($profile) | Out-Null
$oldProfile = $env:USERPROFILE
$script:publicFixture = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEpGVDQ0N3pYdVFlc3RGaXh0dXJlS2V5QUJDREVGR0hJSg== agentdev'
$script:keygenCalls = 0
$script:wslCalls = 0
$script:receivedKey = $null
$env:USERPROFILE = $profile
$runner = {
    param([string[]]$Arguments)
    $script:keygenCalls++
    if ($Arguments -contains '-t') {
        $privatePath = $Arguments[$Arguments.IndexOf('-f') + 1]
        [IO.File]::WriteAllText($privatePath, 'synthetic private fixture placeholder')
        [IO.File]::WriteAllText("$privatePath.pub", $script:publicFixture + [Environment]::NewLine)
        return [pscustomobject]@{ ExitCode = 0; Output = '' }
    }
    if ($Arguments -contains '-lf') { return [pscustomobject]@{ ExitCode = 0; Output = '' } }
    if ($Arguments -contains '-y') { return [pscustomobject]@{ ExitCode = 0; Output = $script:publicFixture } }
    throw 'Unexpected ssh-keygen invocation.'
}
function wsl.exe {
    process {
        $script:wslCalls++
        $script:receivedKey = [string]$_
        if (($args -join ' ') -ne '-d AgentDev -u root -- python3 /opt/machine-bootstrap/current/system/authorize-agent-key.py') {
            throw 'Unexpected WSL invocation.'
        }
        $global:LASTEXITCODE = 0
    }
}
try {
    $sshDirectory = Join-Path $profile '.ssh'
    [IO.Directory]::CreateDirectory($sshDirectory) | Out-Null
    $configPath = Join-Path $sshDirectory 'config'
    $initialConfig = @(
        '# retained comment',
        'Host github.com',
        '    User git',
        'Host *',
        '    ServerAliveInterval 30',
        'Host agentdev',
        '    HostName old.example',
        'Host archive other',
        '    User archived',
        ''
    ) -join [Environment]::NewLine
    [IO.File]::WriteAllText($configPath, $initialConfig)
    & "$PSScriptRoot/../windows/setup-vscode-ssh.ps1" -KeygenRunner $runner
    $merged = [IO.File]::ReadAllText($configPath)
    Assert-True ($script:keygenCalls -eq 3) 'new Ed25519 key generated and public half checked'
    Assert-True ($script:wslCalls -eq 1 -and $script:receivedKey -eq $script:publicFixture) 'only public key streamed to AgentDev'
    Assert-True (([regex]::Matches($merged, '(?m)^Host agentdev$')).Count -eq 1) 'managed Host stanza is unique'
    Assert-True ($merged.Contains('HostName 127.0.0.1') -and $merged.Contains('Port 2222') -and $merged.Contains('User agent')) 'connection target is fixed to AgentDev'
    Assert-True ($merged.Contains('IdentityFile %USERPROFILE%/.ssh/agentdev_ed25519')) 'Windows owner key path is configured'
    Assert-True ($merged.Contains('Host github.com') -and $merged.Contains('Host archive other') -and $merged.Contains('ServerAliveInterval 30')) 'unrelated SSH entries are preserved'
    $firstConfig = $merged
    & "$PSScriptRoot/../windows/setup-vscode-ssh.ps1" -KeygenRunner $runner
    Assert-True ([IO.File]::ReadAllText($configPath) -ceq $firstConfig) 'SSH config merge is idempotent'
    Assert-True ($script:keygenCalls -eq 5 -and $script:wslCalls -eq 2) 'existing matching key pair is reused'

    $brokenProfile = Join-Path $testRoot 'broken'
    $brokenSsh = Join-Path $brokenProfile '.ssh'
    [IO.Directory]::CreateDirectory($brokenSsh) | Out-Null
    $env:USERPROFILE = $brokenProfile
    $brokenPrivate = Join-Path $brokenSsh 'agentdev_ed25519'
    [IO.File]::WriteAllText($brokenPrivate, 'synthetic placeholder')
    Assert-Throws { & "$PSScriptRoot/../windows/setup-vscode-ssh.ps1" -KeygenRunner $runner } '*key pair is incomplete*' 'partial key pair fails closed'
    Assert-True ([IO.File]::Exists($brokenPrivate)) 'partial key file is preserved'
} finally {
    $env:USERPROFILE = $oldProfile
    Remove-Item -LiteralPath $testRoot -Recurse -Force
}
