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
$global:bootstrapVscodeSshTestPublicFixture = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEpGVDQ0N3pYdVFlc3RGaXh0dXJlS2V5QUJDREVGR0hJSg== agentdev'
$global:bootstrapVscodeSshTestKeygenCalls = 0
$global:bootstrapVscodeSshTestWslCalls = 0
$global:bootstrapVscodeSshTestReceivedKey = $null
$global:bootstrapVscodeSshTestWslInvocation = $null
$env:USERPROFILE = $profile
$runner = {
    param([string[]]$Arguments)
    $global:bootstrapVscodeSshTestKeygenCalls++
    if ($Arguments -contains '-t') {
        $privatePath = $Arguments[$Arguments.IndexOf('-f') + 1]
        [IO.File]::WriteAllText($privatePath, 'synthetic private fixture placeholder')
        [IO.File]::WriteAllText("$privatePath.pub", $global:bootstrapVscodeSshTestPublicFixture + [Environment]::NewLine)
        return [pscustomobject]@{ ExitCode = 0; Output = '' }
    }
    if ($Arguments -contains '-lf') { return [pscustomobject]@{ ExitCode = 0; Output = '' } }
    if ($Arguments -contains '-y') { return [pscustomobject]@{ ExitCode = 0; Output = $global:bootstrapVscodeSshTestPublicFixture } }
    throw 'Unexpected ssh-keygen invocation.'
}
function wsl.exe {
    process {
        $global:bootstrapVscodeSshTestWslCalls++
        $global:bootstrapVscodeSshTestReceivedKey = [string]$_
        $global:bootstrapVscodeSshTestWslInvocation = $MyInvocation.Line
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
        '    ProxyCommand old-proxy.exe',
        'Host archive other',
        '    User archived',
        ''
    ) -join [Environment]::NewLine
    [IO.File]::WriteAllText($configPath, $initialConfig)
    & "$PSScriptRoot/../windows/setup-vscode-ssh.ps1" -KeygenRunner $runner
    $merged = [IO.File]::ReadAllText($configPath)
    Assert-True ($global:bootstrapVscodeSshTestKeygenCalls -eq 3) 'new Ed25519 key generated and public half checked'
    Assert-True ($global:bootstrapVscodeSshTestWslCalls -eq 1 -and $global:bootstrapVscodeSshTestReceivedKey -eq $global:bootstrapVscodeSshTestPublicFixture -and $global:bootstrapVscodeSshTestWslInvocation -match 'wsl\.exe -d AgentDev -u root -- python3 \$linuxKeyHelper') 'only public key streamed to the root key helper in AgentDev'
    Assert-True (([regex]::Matches($merged, '(?m)^Host agentdev\r?$')).Count -eq 1) 'managed Host stanza is unique'
    Assert-True ($merged.Contains('HostName 127.0.0.1') -and $merged.Contains('Port 2222') -and $merged.Contains('User agent')) 'connection target is fixed to AgentDev'
    Assert-True ($merged.Contains('IdentityFile ~/.ssh/agentdev_ed25519')) 'OpenSSH home-relative owner key path is configured'
    $priorErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $effectiveSsh = @(& ssh.exe -T -G -F $configPath agentdev 2>$null)
        $sshExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $priorErrorActionPreference
    }
    $effectiveText = $effectiveSsh -join [Environment]::NewLine
    Assert-True ($sshExitCode -eq 0 -and $effectiveText -match '(?m)^hostname 127\.0\.0\.1\r?$' -and $effectiveText -match '(?m)^port 2222\r?$' -and $effectiveText -match '(?m)^user agent\r?$' -and $effectiveText -match '(?m)^identityfile .*[\\/]agentdev_ed25519\r?$') 'Windows OpenSSH parses the managed host without connecting'
    $expectedProxy = 'C:\Windows\System32\wsl.exe -d AgentDev -u agent -- nc 127.0.0.1 2222'
    Assert-True ($effectiveText -match ('(?im)^proxycommand ' + [regex]::Escape($expectedProxy) + '\r?$')) 'Windows OpenSSH selects the AgentDev wake-and-relay command without executing it'
    Assert-True (-not $merged.Contains('old-proxy.exe') -and ([regex]::Matches($merged, '(?im)^\s*ProxyCommand\s+')).Count -eq 1) 'existing managed proxy is replaced once'
    Assert-True ($merged.Contains('Host github.com') -and $merged.Contains('Host archive other') -and $merged.Contains('ServerAliveInterval 30')) 'unrelated SSH entries are preserved'
    $firstConfig = $merged
    & "$PSScriptRoot/../windows/setup-vscode-ssh.ps1" -KeygenRunner $runner
    Assert-True ([IO.File]::ReadAllText($configPath) -ceq $firstConfig) 'SSH config merge is idempotent'
    Assert-True ($global:bootstrapVscodeSshTestKeygenCalls -eq 5 -and $global:bootstrapVscodeSshTestWslCalls -eq 2) 'existing matching key pair is reused'

    # Exercise real script/StrictMode behavior for the zero- and one-line cases.
    # All key generation and WSL calls remain inert fixtures on the CI runner.
    foreach ($configCase in 'missing', 'empty', 'one-line') {
        if ([IO.File]::Exists($configPath)) { [IO.File]::Delete($configPath) }
        if ($configCase -eq 'empty') { [IO.File]::WriteAllText($configPath, '') }
        if ($configCase -eq 'one-line') { [IO.File]::WriteAllText($configPath, '# one-line config without final newline') }
        & "$PSScriptRoot/../windows/setup-vscode-ssh.ps1" -KeygenRunner $runner
        $firstResult = [IO.File]::ReadAllText($configPath)
        Assert-True (([regex]::Matches($firstResult, '(?m)^Host agentdev\r?$')).Count -eq 1) "$configCase config has one managed Host block under StrictMode"
        $priorErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $parsed = @(& ssh.exe -T -G -F $configPath agentdev 2>$null)
            $parseExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $priorErrorActionPreference
        }
        $parsedText = $parsed -join [Environment]::NewLine
        Assert-True ($parseExitCode -eq 0 -and $parsedText -match '(?m)^hostname 127\.0\.0\.1\r?$' -and $parsedText -match '(?m)^port 2222\r?$' -and $parsedText -match '(?m)^user agent\r?$' -and $parsedText -match '(?m)^identitiesonly yes\r?$' -and $parsedText -match '(?m)^identityfile .*[\\/]agentdev_ed25519\r?$') "$configCase config retains all existing connection directives"
        Assert-True ($parsedText -match ('(?im)^proxycommand ' + [regex]::Escape($expectedProxy) + '\r?$')) "$configCase config selects the wake-and-relay proxy"
        if ($configCase -eq 'one-line') {
            Assert-True ($firstResult.StartsWith('# one-line config without final newline' + [Environment]::NewLine)) 'one-line input is retained as a complete line'
        }
        $firstBytes = [Convert]::ToBase64String([IO.File]::ReadAllBytes($configPath))
        & "$PSScriptRoot/../windows/setup-vscode-ssh.ps1" -KeygenRunner $runner
        Assert-True ([Convert]::ToBase64String([IO.File]::ReadAllBytes($configPath)) -ceq $firstBytes) "$configCase config is byte-identical on repeat setup"
        Assert-True (([regex]::Matches([IO.File]::ReadAllText($configPath), '(?im)^\s*ProxyCommand\s+')).Count -eq 1) "$configCase repeat has one proxy directive"
    }

    foreach ($globalSetting in 'ProxyCommand inherited-proxy.exe', 'ProxyCommand=inherited-proxy.exe', 'ProxyJump bastion', 'ProxyJump=bastion') {
        [IO.File]::WriteAllText($configPath, $globalSetting + [Environment]::NewLine + 'Host *' + [Environment]::NewLine + '    ServerAliveInterval 30' + [Environment]::NewLine)
        $beforeConflict = [Convert]::ToBase64String([IO.File]::ReadAllBytes($configPath))
        $priorErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $conflicting = @(& ssh.exe -T -G -F $configPath agentdev 2>$null)
            $conflictingExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $priorErrorActionPreference
        }
        $conflictingText = $conflicting -join [Environment]::NewLine
        Assert-True ($conflictingExitCode -eq 0 -and ($conflictingText -match '(?m)^proxycommand inherited-proxy\.exe\r?$' -or $conflictingText -match '(?m)^proxyjump bastion\r?$')) "$globalSetting is a real OpenSSH global conflict"
        Assert-Throws { & "$PSScriptRoot/../windows/setup-vscode-ssh.ps1" -KeygenRunner $runner } '*global SSH config sets Proxy*' "$globalSetting cannot silently override AgentDev relay"
        Assert-True ([Convert]::ToBase64String([IO.File]::ReadAllBytes($configPath)) -ceq $beforeConflict) "$globalSetting conflict preserves config bytes"
    }

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

