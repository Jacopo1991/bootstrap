[CmdletBinding()]
param()
. "$PSScriptRoot/common.ps1"
$states = ((Invoke-Wsl -WslArgs @('--list', '--verbose')) -join "`n").Replace([string][char]0, '')
Get-WslDisks | ForEach-Object {
    $running = $states -match ('(?m)^\s*\*?\s*' + [regex]::Escape($_.Name) + '\s+Running\s+2\s*$')
    $usage = if ($_.WslVersion -eq 2 -and $running) { Get-WslFilesystemUsage -Name $_.Name } else { $null }
    $_ | Add-Member -NotePropertyName UsedBytes -NotePropertyValue $(if ($usage) { $usage.UsedBytes })
    $_ | Add-Member -NotePropertyName CapBytes -NotePropertyValue $(if ($usage) { $usage.CapBytes })
    $_
} | Select-Object Name, WslVersion, Vhdx, FileBytes, AllocatedBytes, UsedBytes, CapBytes |
    Format-Table -AutoSize
