<#
.SYNOPSIS
    Removes Dictate for the current user.

.DESCRIPTION
    Removes the shortcuts, the uninstall entry, and the install folder.
    Your settings (~\.config\dictate) and the downloaded speech model
    (~\.local\share\dictate) stay unless you pass -RemoveUserData.
#>
[CmdletBinding()]
param(
    [switch]$RemoveUserData,
    [switch]$Quiet
)

$ErrorActionPreference = 'Continue'
$InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "Removing Dictate from $InstallDir"

Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$InstallDir*gui.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

foreach ($lnk in @(
    (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\Dictate.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Startup')) 'Dictate.lnk'))) {
    if (Test-Path $lnk) {
        $shell = New-Object -ComObject WScript.Shell
        $target = $shell.CreateShortcut($lnk).TargetPath
        if ($target -like "$InstallDir*") {
            Remove-Item $lnk -Force
            Write-Host "Removed $lnk"
        } else {
            Write-Host "Kept $lnk (it points to another install: $target)"
        }
    }
}

Remove-Item 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Dictate' -Recurse -Force -ErrorAction SilentlyContinue

if ($RemoveUserData) {
    foreach ($d in @((Join-Path $HOME '.config\dictate'), (Join-Path $HOME '.local\share\dictate'))) {
        if (Test-Path $d) { Remove-Item $d -Recurse -Force; Write-Host "Removed $d" }
    }
}

# This script lives inside the install folder. Delete the folder after it exits.
$cmd = "ping 127.0.0.1 -n 3 > nul & rmdir /s /q `"$InstallDir`""
Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', $cmd -WindowStyle Hidden
Write-Host 'Dictate is removed.'
if (-not $Quiet) { Start-Sleep -Seconds 2 }
