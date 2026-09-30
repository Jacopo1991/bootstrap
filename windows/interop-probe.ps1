param([string]$Name)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-InteropProbe {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')]
        [string]$Name
    )

    $probeName = "interop-probe-$([Guid]::NewGuid().ToString('N')).exe"
    $linuxPath = "/var/tmp/$probeName"
    $uncPath = "\\wsl.localhost\$Name\var\tmp\$probeName"
    $tempDirectory = [IO.Path]::GetTempPath()
    $stdoutPath = Join-Path $tempDirectory "$probeName.stdout"
    $stderrPath = Join-Path $tempDirectory "$probeName.stderr"
    $process = $null
    $probeError = $null
    $cleanupErrors = @()

    try {
        if (-not (Test-Path -LiteralPath "\\wsl.localhost\$Name\var\tmp")) {
            throw "Cannot access distro '$Name' through \\wsl.localhost."
        }
        Copy-Item -LiteralPath (Join-Path $env:SystemRoot 'System32\whoami.exe') -Destination $uncPath
        & wsl.exe -d $Name -u root -- chmod 755 -- $linuxPath
        if ($LASTEXITCODE -ne 0) { throw 'Could not set executable mode on the temporary probe.' }
        & wsl.exe -d $Name -u agent -- test -f $linuxPath -a -x $linuxPath
        if ($LASTEXITCODE -ne 0) { throw 'The temporary probe is not present and executable for agent.' }

        $arguments = "-d $Name -u agent -- bash -lc `"exec '$linuxPath'`""
        $process = Start-Process -FilePath 'wsl.exe' -ArgumentList $arguments `
            -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath
        if (-not $process.WaitForExit(120000)) {
            try { $process.Kill() } catch { }
            throw 'Timed out waiting for the Windows interop probe (120 seconds).'
        }
        $process.WaitForExit()
        $stdout = [IO.File]::ReadAllText($stdoutPath)
        if ($process.ExitCode -eq 0) {
            throw 'Windows whoami.exe ran successfully inside the distro; expected interop to be blocked.'
        }
        if ($stdout.Length -ne 0) {
            throw 'Windows whoami.exe produced stdout, so Windows process interop is working.'
        }
    }
    catch {
        $probeError = $_
    }
    finally {
        if ($null -ne $process) { try { $process.Dispose() } catch { $cleanupErrors += $_.Exception.Message } }
        foreach ($path in @($uncPath, $stdoutPath, $stderrPath)) {
            try { Remove-Item -LiteralPath $path -Force -ErrorAction Stop }
            catch {
                if (Test-Path -LiteralPath $path) { $cleanupErrors += "Could not remove '$path': $($_.Exception.Message)" }
            }
        }
    }
    if ($null -ne $probeError) {
        if ($cleanupErrors.Count -gt 0) { throw ($probeError.Exception.Message + [Environment]::NewLine + ($cleanupErrors -join [Environment]::NewLine)) }
        throw $probeError
    }
    if ($cleanupErrors.Count -gt 0) { throw ($cleanupErrors -join [Environment]::NewLine) }
    Write-Output "PASS: Windows PE launch failed with no stdout for agent in '$Name'."
}

# Dot-sourcing exposes Invoke-InteropProbe for mocked Windows-side tests.
if ($MyInvocation.InvocationName -ne '.') {
    if ([string]::IsNullOrWhiteSpace($Name)) { throw 'Usage: interop-probe.ps1 -Name <distro>' }
    Invoke-InteropProbe -Name $Name
}
