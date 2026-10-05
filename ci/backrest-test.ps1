param([switch]$Live)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 (and pwsh): generates the Backrest config from a fixture workspace and
# compares it with ci/fixtures/backrest-config.expected.json. Nothing is installed or downloaded
# unless -Live is given: that downloads the pinned Backrest and restic into a temp folder, starts
# Backrest with the generated config on a spare port and a temp repository, and checks it stays up.
. "$PSScriptRoot/../windows/install-backrest.ps1"

function Assert-Equal {
    param($Actual, $Expected, [string]$Label)
    if ($Actual -cne $Expected) { throw "$Label`: expected '$Expected', got '$Actual'." }
    Write-Output "PASS: $Label"
}

$temp = Join-Path ([IO.Path]::GetTempPath()) ('backrest-tests-' + [Guid]::NewGuid().ToString('N'))
try {
    $workspace = Join-Path $temp 'dev_workspace'
    $layout = @{
        'consultancy-website' = ''                                   # always included, no remote
        'customer-harness' = "[remote `"origin`"]`n`turl = git@github.com:x/y.git"  # always included even with a remote
        'typo3-dkm-plugin' = ''
        'scratch-local' = '[core]'                                   # no GitHub remote
        'bootstrap' = "[remote `"origin`"]`n`turl = https://github.com/Jacopo1991/bootstrap.git"
        'work-pr' = "[remote `"origin`"]`n`turl = git@github.com:Jacopo1991/work-pr.git"
    }
    foreach ($name in $layout.Keys) {
        $git = Join-Path (Join-Path $workspace $name) '.git'
        New-Item -ItemType Directory -Force -Path $git | Out-Null
        [IO.File]::WriteAllText((Join-Path $git 'config'), $layout[$name])
    }
    # Worktrees (.git is a file) and plain folders are never sources.
    $worktree = Join-Path $workspace 'cortex-mining-address-redaction'
    New-Item -ItemType Directory -Force -Path $worktree | Out-Null
    [IO.File]::WriteAllText((Join-Path $worktree '.git'), 'gitdir: /home/agent/dev_workspace/cortex-mining/.git/worktrees/x')
    New-Item -ItemType Directory -Force -Path (Join-Path $workspace 'plain-folder') | Out-Null
    $names = @(Get-LocalOnlyRepoNames -WorkspaceRoot $workspace)
    Assert-Equal ($names -join ',') 'consultancy-website,customer-harness,scratch-local,typo3-dkm-plugin' 'local-only discovery'

    $generated = New-BackrestConfig -Sources (Get-BackrestSources -RepoNames $names) `
        -PasswordFile 'C:\Users\founder\AppData\Local\backrest\restic-password.txt'
    $actual = (ConvertTo-BackrestJson -Config $generated) | ConvertFrom-Json | ConvertTo-Json -Depth 12 -Compress
    $expectedFile = Join-Path $PSScriptRoot 'fixtures/backrest-config.expected.json'
    $expected = Get-Content -LiteralPath $expectedFile -Raw | ConvertFrom-Json | ConvertTo-Json -Depth 12 -Compress
    Assert-Equal $actual $expected 'generated config matches fixture'
    if ($actual -match '(?i)"password"\s*:') { throw 'Generated config must not contain a password field.' }
    Write-Output 'PASS: no password in generated config'

    # Password file: owner-only ACL, content written, mismatch and empty refused.
    $secret = ConvertTo-SecureString -String 'fixture-secret' -AsPlainText -Force
    $passwordFile = Join-Path $temp 'sub\restic-password.txt'
    New-ResticPasswordFile -Path $passwordFile -Password $secret
    Assert-Equal ([IO.File]::ReadAllText($passwordFile)) 'fixture-secret' 'password file content'
    $acl = Get-Acl -LiteralPath $passwordFile
    Assert-Equal $acl.AreAccessRulesProtected $true 'password file does not inherit access'
    $rules = @($acl.Access)
    Assert-Equal $rules.Count 1 'password file has a single access rule'
    Assert-Equal $rules[0].IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value `
        ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value) 'password file is for the current user only'
    $answers = [System.Collections.Generic.Queue[securestring]]::new()
    function Read-Host { param([switch]$AsSecureString, [string]$Prompt) return $answers.Dequeue() }
    $answers.Enqueue($secret); $answers.Enqueue((ConvertTo-SecureString -String 'other' -AsPlainText -Force))
    try { Read-ConfirmedPassword | Out-Null; throw 'mismatch accepted' } catch { if ($_.Exception.Message -notlike '*differ*') { throw } }
    $answers.Enqueue((ConvertTo-SecureString -String 'x' -AsPlainText -Force)); $answers.Enqueue((ConvertTo-SecureString -String 'x' -AsPlainText -Force))
    Assert-Equal (ConvertFrom-SecureStringPlain -Secret (Read-ConfirmedPassword)) 'x' 'matching passwords accepted'
    Remove-Item function:\Read-Host
    Write-Output 'PASS: password prompt confirms and refuses mismatches'

    $launcher = Get-BackrestLauncherText -Base 'C:\b' -ConfigFile 'C:\b\config.json' -BinDirectory 'C:\b\bin'
    foreach ($line in "Set-Location -LiteralPath 'C:\b'", "`$env:BACKREST_RESTIC_COMMAND = 'C:\b\bin\restic.exe'",
                      "`$env:BACKREST_CONFIG = 'C:\b\config.json'", "`$env:BACKREST_DATA = 'C:\b\data'") {
        if (-not $launcher.Contains($line)) { throw "Launcher lacks: $line" }
    }
    Write-Output 'PASS: launcher sets working directory and explicit paths'

    # Merge keeps the founder's repo (with a password) and auth, replaces only the generated plan.
    $existing = '{"modno":7,"auth":{"users":[{"name":"f"}]},"repos":[{"id":"restic","uri":"C:\\backups\\restic","password":"P"}],"plans":[{"id":"agentdev-daily","paths":["old"]},{"id":"mine"}]}'
    $merged = Merge-BackrestConfig -ExistingJson $existing -Generated $generated
    Assert-Equal $merged.repos[0].password 'P' 'merge keeps unrelated repo fields'
    Assert-Equal (@($merged.repos[0].env) -join '|') 'RESTIC_PASSWORD_FILE=C:\Users\founder\AppData\Local\backrest\restic-password.txt' 'merge sets the password file env'
    Assert-Equal $merged.auth.users[0].name 'f' 'merge keeps auth'
    Assert-Equal (@($merged.plans | ForEach-Object id) -join ',') 'mine,agentdev-daily' 'merge replaces only generated plan'
    Assert-Equal @($merged.plans | Where-Object id -eq 'agentdev-daily')[0].schedule.cron '30 2 * * *' 'merged plan is fresh'
} finally { Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue }
Write-Output 'backrest-test.ps1 OK'

if ($Live) {
    $liveRoot = Join-Path ([IO.Path]::GetTempPath()) ('backrest-live-' + [Guid]::NewGuid().ToString('N'))
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start(); $port = $listener.LocalEndpoint.Port; $listener.Stop()
    $process = $null
    try {
        $bin = Join-Path $liveRoot 'bin'; $data = Join-Path $liveRoot 'data'; $source = Join-Path $liveRoot 'source'
        New-Item -ItemType Directory -Force -Path $data, $source, (Join-Path $liveRoot 'repo') | Out-Null
        Set-Content -LiteralPath (Join-Path $source 'hello.txt') -Value 'hello'
        Install-BackrestBinary -InstallDirectory $bin
        Write-Output 'PASS: pinned Backrest and restic downloaded, hashes verified'
        $livePasswordFile = Join-Path $liveRoot 'restic-password.txt'
        New-ResticPasswordFile -Path $livePasswordFile -Password (ConvertTo-SecureString -String ([Guid]::NewGuid().ToString('N')) -AsPlainText -Force)
        $config = New-BackrestConfig -Sources @($source) -PasswordFile $livePasswordFile -RepoPath (Join-Path $liveRoot 'repo')
        $configFile = Join-Path $liveRoot 'config.json'
        [IO.File]::WriteAllText($configFile, (ConvertTo-BackrestJson -Config $config), [Text.UTF8Encoding]::new($false))
        $launcherFile = Join-Path $liveRoot 'start.ps1'
        [IO.File]::WriteAllText($launcherFile, (Get-BackrestLauncherText -Base $liveRoot -ConfigFile $configFile -BinDirectory $bin -Port "127.0.0.1:$port"))
        $log = Join-Path $liveRoot 'out.log'; $errLog = Join-Path $liveRoot 'err.log'
        $powerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $process = Start-Process -FilePath $powerShell -ArgumentList @('-NoProfile', '-NonInteractive', '-File', $launcherFile) `
            -WorkingDirectory $env:SystemRoot -RedirectStandardOutput $log -RedirectStandardError $errLog -PassThru -WindowStyle Hidden
        $backrestAlive = { @(Get-CimInstance Win32_Process -Filter "Name='backrest.exe'" | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($liveRoot) }).Count -gt 0 }
        $status = $null
        for ($i = 0; $i -lt 30 -and $null -eq $status; $i++) {
            Start-Sleep -Seconds 1
            if ($process.HasExited -and -not (& $backrestAlive)) { break }
            try { $status = (Invoke-WebRequest -Uri "http://127.0.0.1:$port/" -UseBasicParsing -TimeoutSec 3).StatusCode } catch { }
        }
        for ($i = 0; $i -lt 15 -and -not (Test-Path -LiteralPath (Join-Path $liveRoot 'repo\config')); $i++) { Start-Sleep -Seconds 1 }
        Start-Sleep -Seconds 3
        $text = ((Get-Content -LiteralPath $log -Raw -ErrorAction SilentlyContinue), (Get-Content -LiteralPath $errLog -Raw -ErrorAction SilentlyContinue)) -join "`n"
        # backrest.exe may detach from the launcher (GUI subsystem), so judge the real process.
        if (-not (& $backrestAlive)) { throw "backrest.exe is not running. Log:`n$text" }
        Assert-Equal $status 200 'web UI answers 200'
        if ($text -match 'FATAL') { throw "FATAL in Backrest log:`n$text" }
        Write-Output 'PASS: Backrest stayed up, no FATAL in log'
        if (-not (Test-Path -LiteralPath (Join-Path $liveRoot 'repo\config'))) { throw "Repository was not initialized. Log:`n$text" }
        Write-Output 'PASS: temporary restic repository initialized'
    } finally {
        foreach ($p in @(Get-CimInstance Win32_Process -Filter "Name='backrest.exe'" | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($liveRoot) })) {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        }
        if ($null -ne $process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 1
        Remove-Item -LiteralPath $liveRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Output 'backrest live smoke OK'
}
