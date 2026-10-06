[CmdletBinding()]
param([ValidateRange(1, 10000000)][int]$IncludedMinutes = 3000,
      [string]$StateDirectory = (Join-Path $env:LOCALAPPDATA 'machine-bootstrap'))
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Daily task (founder login): read this month's GitHub Actions minutes, compare them with the
# plan's included minutes (-IncludedMinutes, default 3000) and show a Windows notification once
# at 50% and once at 80% per month. Every check is logged. Built-in PowerShell only.

$script:Thresholds = @(80, 50)  # highest first

# Healthchecks.io ping (check windows-minutes-watchdog) when the owner-provisioned pings file has
# its URL; see job-ping.ps1. Skipped silently otherwise.
$jobPing = Join-Path $PSScriptRoot 'job-ping.ps1'
if (Test-Path -LiteralPath $jobPing) { . $jobPing } else { function Send-JobPing { param($Check, $Success) } }

function Get-ActionsMinutesUsed {
    param([Parameter(Mandatory)]$Usage)
    $property = $Usage.PSObject.Properties['usageItems']
    if ($null -eq $property) { throw 'Billing usage has no usageItems.' }
    $total = 0.0
    foreach ($item in @($property.Value)) {
        if ([string]$item.product -eq 'actions' -and [string]$item.unitType -eq 'Minutes') { $total += [double]$item.quantity }
    }
    return $total
}

function Get-MinutesLevel {
    param([double]$Used, [int]$Included)
    $percent = $Used / $Included * 100
    foreach ($threshold in $script:Thresholds) { if ($percent -ge $threshold) { return $threshold } }
    return 0
}

# The level to announce now: only a threshold not yet announced this month.
function Get-MinutesAlert {
    param([int]$Level, [string]$Month, $State)
    $notified = 0
    if ($null -ne $State -and $State.PSObject.Properties['month'] -and $State.month -eq $Month) { $notified = [int]$State.notified }
    if ($Level -gt $notified) { return $Level }
    return 0
}

function Get-BillingUsage {
    $gh = Get-Command gh -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $gh) { throw 'gh not found on PATH.' }
    $ErrorActionPreference = 'Continue'  # Windows PowerShell 5.1 turns native stderr into terminating errors under 'Stop'
    $text = (& $gh.Source api /users/Jacopo1991/settings/billing/usage 2>&1 | ForEach-Object { [string]$_ }) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "gh api failed ($LASTEXITCODE)." }
    return $text | ConvertFrom-Json
}

function Show-WatchdogNotification {
    param([string]$Title, [string]$Message)
    try {
        [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
        [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
        $xml = [Windows.Data.Xml.Dom.XmlDocument]::new()
        $xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>' + [Security.SecurityElement]::Escape($Title) + '</text><text>' + [Security.SecurityElement]::Escape($Message) + '</text></binding></visual></toast>')
        $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
    } catch {
        Add-Type -AssemblyName System.Windows.Forms, System.Drawing
        $icon = [Windows.Forms.NotifyIcon]::new()
        $icon.Icon = [Drawing.SystemIcons]::Warning
        $icon.Visible = $true
        $icon.ShowBalloonTip(15000, $Title, $Message, [Windows.Forms.ToolTipIcon]::Warning)
        Start-Sleep -Seconds 16
        $icon.Dispose()
    }
}

function Write-WatchdogLog {
    param([string]$Message, [string]$Directory = $StateDirectory)
    New-Item -ItemType Directory -Force -Path $Directory | Out-Null
    Add-Content -LiteralPath (Join-Path $Directory 'minutes-watchdog.log') -Value ((Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK') + ' ' + $Message)
}

function Invoke-MinutesWatchdog {
    param([int]$Included = $IncludedMinutes, [string]$Directory = $StateDirectory, [datetime]$Now = (Get-Date))
    $month = $Now.ToString('yyyy-MM')
    $statePath = Join-Path $Directory 'minutes-watchdog.json'
    try {
        $used = Get-ActionsMinutesUsed -Usage (Get-BillingUsage)
        $level = Get-MinutesLevel -Used $used -Included $Included
        $state = $null
        if (Test-Path -LiteralPath $statePath) { $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json }
        $alert = Get-MinutesAlert -Level $level -Month $month -State $state
        $percent = [math]::Round($used / $Included * 100, 1)
        Write-WatchdogLog -Directory $Directory ("OK month=$month used=$used included=$Included percent=$percent level=$level alert=$alert")
        if ($alert -gt 0) {
            Show-WatchdogNotification -Title "GitHub Actions minutes: $alert% of the month used" `
                -Message "$([math]::Round($used)) of $Included included minutes used in $month ($percent%). Stop adding workflow runs and tell the PM."
            New-Item -ItemType Directory -Force -Path $Directory | Out-Null
            [pscustomobject]@{ month = $month; notified = $alert } | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
        }
        return 0
    } catch {
        Write-WatchdogLog -Directory $Directory ("ERROR " + $_.Exception.Message)
        return 1
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $exitCode = Invoke-MinutesWatchdog
    Send-JobPing -Check 'windows-minutes-watchdog' -Success ($exitCode -eq 0)
    exit $exitCode
}
