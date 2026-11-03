# Richtet Dictate auf einem fremden Windows-PC ein, ohne Administratorrechte.
#
#   powershell -ExecutionPolicy Bypass -File einrichten.ps1
#
# Bringt eine eigene Python-Kopie mit, damit nichts im System installiert wird,
# holt genau den festgelegten Dictate-Stand von GitHub und ruft dessen install.ps1 auf.
# Danach folgt selbsttest.py mit Modell-Download und Prüfung.
param(
    [string]$Ziel = (Join-Path $env:LOCALAPPDATA "Dictate-App"),
    [string]$Commit = "main",
    [string]$PythonVersion = "3.13.17"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Hier = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "Dictate einrichten nach $Ziel"
New-Item -ItemType Directory -Force $Ziel | Out-Null

# --- Python ohne Installation ---------------------------------------------------
$PyDir = Join-Path $Ziel "python"
$PyExe = Join-Path $PyDir "tools\python.exe"
if (-not (Test-Path $PyExe)) {
    Write-Host "[1/3] Hole Python $PythonVersion ..."
    $Zip = Join-Path $Ziel "python.zip"
    Invoke-WebRequest "https://www.nuget.org/api/v2/package/python/$PythonVersion" -OutFile $Zip -UseBasicParsing
    Expand-Archive $Zip $PyDir -Force
    Remove-Item $Zip
} else {
    Write-Host "[1/3] Python ist schon da."
}

# --- Dictate --------------------------------------------------------------------
$App = Join-Path $Ziel "dictate"
if (-not (Test-Path (Join-Path $App "gui.py"))) {
    Write-Host "[2/3] Hole Dictate, Stand $Commit ..."
    $Zip = Join-Path $Ziel "dictate.zip"
    $Tmp = Join-Path $Ziel "dictate_tmp"
    Invoke-WebRequest "https://github.com/winidi/dictate/archive/$Commit.zip" -OutFile $Zip -UseBasicParsing
    if (Test-Path $Tmp) { Remove-Item $Tmp -Recurse -Force }
    Expand-Archive $Zip $Tmp -Force
    Move-Item (Get-ChildItem $Tmp -Directory | Select-Object -First 1).FullName $App
    Remove-Item $Tmp -Recurse -Force
    Remove-Item $Zip
    Set-Content -Encoding Ascii (Join-Path $App "VERSION") $Commit
} else {
    Write-Host "[2/3] Dictate ist schon da."
}

# --- install.ps1 sucht zuerst "py -3". Diese kleine Weiche zeigt auf unsere Kopie. ---
Write-Host "[3/3] Installiere Pakete und Eintrag im Startmenue, das dauert einige Minuten ..."
$Weiche = Join-Path $Ziel "weiche"
New-Item -ItemType Directory -Force $Weiche | Out-Null
Set-Content -Encoding Ascii (Join-Path $Weiche "py.cmd") "@`"$PyExe`" %2 %3 %4 %5 %6 %7 %8 %9"
$env:PATH = "$Weiche;$env:PATH"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $App "install.ps1")
if ($LASTEXITCODE -ne 0) { throw "install.ps1 ist fehlgeschlagen (Code $LASTEXITCODE)" }

Copy-Item (Join-Path $Hier "selbsttest.py") $Ziel -Force
Copy-Item (Join-Path $Hier "testsatz.wav") $Ziel -Force

Write-Host ""
Write-Host "Fertig. Jetzt den Selbsttest starten:"
Write-Host "  & `"$App\.venv\Scripts\python.exe`" `"$Ziel\selbsttest.py`""
