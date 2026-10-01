
[CmdletBinding()]
param([scriptblock]$KeygenRunner = $null)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-AgentDevSshKeygen {
    param([string[]]$ArgumentList, [scriptblock]$Runner)
    if ($null -ne $Runner) { return & $Runner $ArgumentList }
    $quoted = foreach ($argument in $ArgumentList) {
        if ($argument.Length -eq 0) { '""' }
        elseif ($argument -match '[\s"]') { '"' + $argument.Replace('"', '\"') + '"' }
        else { $argument }
    }
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = 'ssh-keygen.exe'
    $start.Arguments = $quoted -join ' '
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::Start($start)
    $process.StandardInput.Close()
    $output = $process.StandardOutput.ReadToEnd()
    $null = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    $exitCode = $process.ExitCode
    $process.Dispose()
    [pscustomobject]@{ ExitCode = $exitCode; Output = $output }
}

$sshDirectory = Join-Path $env:USERPROFILE '.ssh'
$keyPath = Join-Path $sshDirectory 'agentdev_ed25519'
$publicKeyPath = "$keyPath.pub"
$configPath = Join-Path $sshDirectory 'config'
$linuxKeyHelper = '/opt/machine-bootstrap/current/system/authorize-agent-key.py'

[IO.Directory]::CreateDirectory($sshDirectory) | Out-Null
$privateExists = [IO.File]::Exists($keyPath)
$publicExists = [IO.File]::Exists($publicKeyPath)
if ($privateExists -xor $publicExists) {
    throw 'The AgentDev key pair is incomplete. Preserve it and repair the pair before retrying.'
}
if (-not $privateExists) {
    $generated = Invoke-AgentDevSshKeygen @('-q', '-t', 'ed25519', '-N', '', '-f', $keyPath, '-C', 'agentdev') $KeygenRunner
    if ($generated.ExitCode -ne 0 -or -not [IO.File]::Exists($keyPath) -or -not [IO.File]::Exists($publicKeyPath)) {
        throw 'ssh-keygen could not create the AgentDev Ed25519 key pair.'
    }
}

# Read and validate public data only. ssh-keygen receives the private key path
# and returns its public half for pair comparison; PowerShell never reads the
# private-key contents.
$publicText = [IO.File]::ReadAllText($publicKeyPath)
if ($publicText -notmatch '^(ssh-ed25519 [A-Za-z0-9+/]+={0,3}(?: [^\r\n]*)?)\r?\n?$') {
    throw 'The AgentDev public key is not one valid Ed25519 line.'
}
$publicKey = $Matches[1]
$publicCheck = Invoke-AgentDevSshKeygen @('-lf', $publicKeyPath) $KeygenRunner
if ($publicCheck.ExitCode -ne 0) { throw 'The AgentDev public key failed OpenSSH validation.' }
$derived = Invoke-AgentDevSshKeygen @('-y', '-P', '', '-f', $keyPath) $KeygenRunner
if ($derived.ExitCode -ne 0) { throw 'The existing AgentDev private key is unavailable or protected by a passphrase.' }
$derivedFields = @($derived.Output.Trim() -split '\s+')
$publicFields = @($publicKey -split '\s+')
if ($derivedFields.Count -lt 2 -or $publicFields.Count -lt 2 -or
    $derivedFields[0] -cne $publicFields[0] -or $derivedFields[1] -cne $publicFields[1]) {
    throw 'The AgentDev public and private keys do not match. Preserve both and repair them before retrying.'
}

# Transfer only the single validated public key over the owner-launched WSL
# process stdin. This does not depend on Windows interop inside the agent.
$publicKey | & wsl.exe -d AgentDev -u root -- python3 $linuxKeyHelper
if ($LASTEXITCODE -ne 0) { throw 'The public key could not be installed in AgentDev.' }

if ([IO.File]::Exists($configPath)) {
    if ([IO.Directory]::Exists($configPath)) { throw 'The SSH config path is a directory.' }
    $original = [IO.File]::ReadAllText($configPath)
} else {
    $original = ''
}
$lineEnding = [Environment]::NewLine
$hadFinalNewline = $original.EndsWith([char]10) -or $original.EndsWith([char]13)
$lines = @(if ($original.Length) { [regex]::Split($original, '\r\n|\n|\r') })
$kept = New-Object 'System.Collections.Generic.List[string]'
$index = 0
while ($index -lt $lines.Length) {
    if ($lines[$index] -match '^\s*Host\s+agentdev\s*(?:#.*)?$') {
        $index++
        while ($index -lt $lines.Length -and $lines[$index] -notmatch '^\s*(?:Host|Match)\s+') { $index++ }
        continue
    }
    $kept.Add($lines[$index])
    $index++
}
while ($kept.Count -gt 0 -and $kept[$kept.Count - 1] -eq '') { $kept.RemoveAt($kept.Count - 1) }

# Keep global defaults ahead of host stanzas and put the specific block before
# wildcard and Match entries so first-value-wins settings remain effective.
$insertAt = 0
while ($insertAt -lt $kept.Count -and $kept[$insertAt] -notmatch '^\s*(?:Host|Match)\s+') { $insertAt++ }
$globalConflicts = @('HostName', 'Port', 'User', 'IdentityFile', 'IdentitiesOnly', 'ProxyCommand')
for ($i = 0; $i -lt $insertAt; $i++) {
    foreach ($directive in $globalConflicts) {
        if ($kept[$i] -match ('^\s*' + $directive + '\s+')) {
            throw "The global SSH config sets $directive; move that setting into host stanzas before running this script."
        }
    }
}
$block = @(
    'Host agentdev',
    '    HostName 127.0.0.1',
    '    Port 2222',
    '    User agent',
    '    IdentityFile ~/.ssh/agentdev_ed25519',
    '    IdentitiesOnly yes',
    '    ProxyCommand C:\Windows\System32\wsl.exe -d AgentDev -u agent -- nc 127.0.0.1 2222'
)
for ($i = $block.Length - 1; $i -ge 0; $i--) { $kept.Insert($insertAt, $block[$i]) }
$result = $kept -join $lineEnding
if ($hadFinalNewline -or $result.Length -eq 0) { $result += $lineEnding }
if ($result -cne $original) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($configPath, $result, $encoding)
}
Write-Output 'AgentDev Remote-SSH is configured for this Windows user.'
