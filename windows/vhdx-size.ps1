[CmdletBinding()]
param()
. "$PSScriptRoot/common.ps1"
Get-WslDisks | ForEach-Object {
    $usage = if ($_.WslVersion -eq 2) { Get-WslFilesystemUsage -Name $_.Name } else { $null }
    $_ | Add-Member -NotePropertyName UsedBytes -NotePropertyValue $(if ($usage) { $usage.UsedBytes })
    $_ | Add-Member -NotePropertyName CapBytes -NotePropertyValue $(if ($usage) { $usage.CapBytes })
    $_
} | Select-Object Name, WslVersion, Vhdx, FileBytes, AllocatedBytes, UsedBytes, CapBytes |
    Format-Table -AutoSize
