<#
.SYNOPSIS
    Builds Dictate-Setup-<version>.zip with a single setup.exe inside.

.DESCRIPTION
    Uses IExpress, which ships with Windows (no extra tools, no download).
    setup.exe unpacks the app files into a temp folder and runs install.ps1.
    The result is written to .\dist\.

    The exe is not code-signed. Windows SmartScreen can warn on first start.
    Sign setup.exe with your company certificate if your IT department requires it.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\build-setup.ps1 -Version 1.0.0
#>
[CmdletBinding()]
param(
    [string]$Version = '1.0.0'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Dist = Join-Path $Root 'dist'
$Stage = Join-Path $Dist 'payload'
$ExePath = Join-Path $Dist 'setup.exe'
$ZipPath = Join-Path $Dist "Dictate-Setup-$Version.zip"
$SedPath = Join-Path $Dist 'dictate.sed'

if (-not (Get-Command iexpress.exe -ErrorAction SilentlyContinue)) {
    throw 'iexpress.exe not found. It ships with Windows in C:\Windows\System32.'
}

Write-Host "Staging files ..."
if (Test-Path $Dist) { Remove-Item $Dist -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Stage | Out-Null
$payload = @('gui.py', 'core.py', 'dictate.py', 'local_stt.py', 'evdev_listener.py',
             'README.md', 'LICENSE', 'requirements-windows.txt', 'install.ps1', 'uninstall.ps1')
foreach ($f in $payload) {
    $src = Join-Path $Root $f
    if (-not (Test-Path $src)) { throw "Missing file: $f" }
    Copy-Item $src $Stage
}
# Bake the version into the installer call.
$launcher = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File install.ps1 -FromSetup -Version $Version"

$fileLines = @(); $srcLines = @(); $i = 0
foreach ($f in $payload) {
    $fileLines += "FILE$i=""$f"""
    $srcLines  += "%FILE$i%="
    $i++
}

$sed = @"
[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=1
HideExtractAnimation=1
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=%InstallPrompt%
DisplayLicense=%DisplayLicense%
FinishMessage=%FinishMessage%
TargetName=%TargetName%
FriendlyName=%FriendlyName%
AppLaunched=%AppLaunched%
PostInstallCmd=%PostInstallCmd%
AdminQuietInstCmd=%AdminQuietInstCmd%
UserQuietInstCmd=%UserQuietInstCmd%
SourceFiles=SourceFiles
[Strings]
InstallPrompt=Install Dictate $Version for the current user?
DisplayLicense=
FinishMessage=
TargetName=$ExePath
FriendlyName=Dictate Setup $Version
AppLaunched=$launcher
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=
$($fileLines -join "`r`n")
[SourceFiles]
SourceFiles0=$Stage\
[SourceFiles0]
$($srcLines -join "`r`n")
"@
Set-Content -Path $SedPath -Value $sed -Encoding ASCII

Write-Host "Running IExpress ..."
# IExpress only accepts the SED file by a relative name here, so run from the dist folder.
Push-Location $Dist
try { cmd.exe /c "iexpress.exe /N /Q dictate.sed" } finally { Pop-Location }
# IExpress is a GUI program and can return before it finishes. Wait for a stable file size.
$deadline = (Get-Date).AddSeconds(90)
$last = -1
while ((Get-Date) -lt $deadline) {
    if (Test-Path $ExePath) {
        $size = (Get-Item $ExePath).Length
        if ($size -gt 0 -and $size -eq $last) { break }
        $last = $size
    }
    Start-Sleep -Milliseconds 700
}
if (-not (Test-Path $ExePath)) { throw 'IExpress did not create setup.exe.' }

$readme = @"
Dictate $Version - Windows setup

1. Unzip this archive.
2. Double-click setup.exe and confirm the prompt.
3. Wait until the window says "Dictate is installed". This takes a few minutes.
4. Open the Start Menu, type "Dictate", and start it.

The installer needs Internet access to python.org / PyPI and installs for the current user only.
It installs Python 3.12 with winget if Python 3.10+ is missing.
Settings > Apps > Installed apps removes Dictate again.
Log file: %LOCALAPPDATA%\Programs\Dictate\install.log
"@
Set-Content -Path (Join-Path $Dist 'README-setup.txt') -Value $readme -Encoding ASCII

Compress-Archive -Path $ExePath, (Join-Path $Dist 'README-setup.txt') -DestinationPath $ZipPath -Force
$mb = [math]::Round((Get-Item $ZipPath).Length / 1MB, 2)
Write-Host "Built $ZipPath ($mb MB)" -ForegroundColor Green
