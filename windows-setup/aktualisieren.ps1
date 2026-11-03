# Bringt eine mit einrichten.ps1 angelegte Dictate-Installation auf einen neuen Stand.
#
#   powershell -ExecutionPolicy Bypass -File aktualisieren.ps1 -Commit <sha>
#
# Einstellungen und Modelle liegen in %APPDATA%\Dictate und %LOCALAPPDATA%\Dictate
# und bleiben unberuehrt. Die Python-Umgebung bleibt, install.ps1 ergaenzt nur Fehlendes.
param(
    [Parameter(Mandatory = $true)][string]$Commit,
    [string]$Ziel = (Join-Path $env:LOCALAPPDATA "Dictate-App")
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$App = Join-Path $Ziel "dictate"
$PyExe = Join-Path $Ziel "python\tools\python.exe"
if (-not (Test-Path (Join-Path $App "gui.py")) -or -not (Test-Path $PyExe)) {
    throw "Keine Installation unter $Ziel gefunden. Zuerst einrichten.ps1 ausfuehren."
}

# Laufendes Dictate muss vorher beendet sein, sonst laeuft der alte Stand weiter.
$laeuft = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$App\gui.py*" }
if ($laeuft) {
    Write-Host "Dictate laeuft noch. Bitte unten rechts beim Mikrofon-Symbol 'Beenden' waehlen und dann neu starten."
    exit 1
}

$Alt = if (Test-Path (Join-Path $App "VERSION")) { (Get-Content (Join-Path $App "VERSION")).Trim() } else { "unbekannt" }
Write-Host "Aktualisiere Dictate von $Alt auf $Commit ..."
$Zip = Join-Path $Ziel "update.zip"
$Tmp = Join-Path $Ziel "update_tmp"
Invoke-WebRequest "https://github.com/winidi/dictate/archive/$Commit.zip" -OutFile $Zip -UseBasicParsing
if (Test-Path $Tmp) { Remove-Item $Tmp -Recurse -Force }
Expand-Archive $Zip $Tmp -Force
$Neu = (Get-ChildItem $Tmp -Directory | Select-Object -First 1).FullName
Copy-Item (Join-Path $Neu "*") $App -Recurse -Force
Remove-Item $Tmp -Recurse -Force
Remove-Item $Zip
Set-Content -Encoding Ascii (Join-Path $App "VERSION") $Commit

$Weiche = Join-Path $Ziel "weiche"
$env:PATH = "$Weiche;$env:PATH"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $App "install.ps1")
if ($LASTEXITCODE -ne 0) { throw "install.ps1 ist fehlgeschlagen (Code $LASTEXITCODE)" }

Write-Host ""
Write-Host "Fertig, Stand $Commit. Dictate ueber das Startmenue neu starten."
