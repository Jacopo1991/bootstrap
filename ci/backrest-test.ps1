Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 (and pwsh): generates the Backrest config from a fixture workspace and
# compares it with ci/fixtures/backrest-config.expected.json. Nothing is installed or downloaded.
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
    $names = @(Get-LocalOnlyRepoNames -WorkspaceRoot $workspace)
    Assert-Equal ($names -join ',') 'consultancy-website,customer-harness,scratch-local,typo3-dkm-plugin' 'local-only discovery'

    $generated = New-BackrestConfig -Sources (Get-BackrestSources -RepoNames $names)
    $actual = (ConvertTo-BackrestJson -Config $generated) | ConvertFrom-Json | ConvertTo-Json -Depth 12 -Compress
    $expectedFile = Join-Path $PSScriptRoot 'fixtures/backrest-config.expected.json'
    $expected = Get-Content -LiteralPath $expectedFile -Raw | ConvertFrom-Json | ConvertTo-Json -Depth 12 -Compress
    Assert-Equal $actual $expected 'generated config matches fixture'
    if ($actual -match '(?i)password') { throw 'Generated config must not contain a password field.' }
    Write-Output 'PASS: no password in generated config'

    # Merge keeps the founder's repo (with a password) and auth, replaces only the generated plan.
    $existing = '{"modno":7,"auth":{"users":[{"name":"f"}]},"repos":[{"id":"restic","uri":"C:\\backups\\restic","password":"P"}],"plans":[{"id":"agentdev-daily","paths":["old"]},{"id":"mine"}]}'
    $merged = Merge-BackrestConfig -ExistingJson $existing -Generated $generated
    Assert-Equal $merged.repos[0].password 'P' 'merge keeps repo password'
    Assert-Equal $merged.auth.users[0].name 'f' 'merge keeps auth'
    Assert-Equal (@($merged.plans | ForEach-Object id) -join ',') 'mine,agentdev-daily' 'merge replaces only generated plan'
    Assert-Equal @($merged.plans | Where-Object id -eq 'agentdev-daily')[0].schedule.cron '30 2 * * *' 'merged plan is fresh'
} finally { Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue }
Write-Output 'backrest-test.ps1 OK'
