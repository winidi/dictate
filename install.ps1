# Dictate installer for Windows.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Autostart]
#
# Creates a virtual environment next to this script, installs the Python
# packages into it, draws the microphone icon, and puts a "Dictate" shortcut
# in the Start Menu (and, with -Autostart, in the Startup folder). The
# shortcut runs gui.py through pythonw.exe, so no console window appears.
param(
    [switch]$Autostart
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $ScriptDir ".venv"
$PythonW = Join-Path $Venv "Scripts\pythonw.exe"
$Python = Join-Path $Venv "Scripts\python.exe"
$Icon = Join-Path $ScriptDir "dictate.ico"

Write-Host "Dictate installer (Windows)"
Write-Host "==========================="
Write-Host ""

# --- [1/4] find a Python 3.10+ ------------------------------------------------
Write-Host "[1/4] Looking for Python 3.10+ ..."
$SystemPython = $null
foreach ($candidate in @(@("py", "-3"), @("python"), @("python3"))) {
    $exe = $candidate[0]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        $v = & $exe @($candidate[1..($candidate.Length - 1)]) -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    } catch { continue }
    if ($v -match '^(\d+)\.(\d+)$' -and [int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 10) {
        $SystemPython = $candidate
        Write-Host "  Using $($candidate -join ' ') (Python $v)"
        break
    }
}
if (-not $SystemPython) {
    Write-Host "  No Python 3.10+ found. Install it from https://www.python.org/downloads/windows/"
    Write-Host "  (tick 'Add python.exe to PATH') and run this script again."
    exit 1
}

# --- [2/4] virtual environment + packages ------------------------------------
Write-Host ""
Write-Host "[2/4] Installing Python packages into $Venv ..."
if (-not (Test-Path $PythonW)) {
    & $SystemPython[0] @($SystemPython[1..($SystemPython.Length - 1)]) -m venv $Venv
    if (-not $?) { throw "creating the virtual environment failed" }
}
& $Python -m pip install --quiet --upgrade pip
# Core deps: GUI, audio capture, hotkey listener + typing, HTTP client, arrays.
# Local-STT deps: sherpa-onnx runs Parakeet on the CPU offline;
# huggingface_hub downloads the model on demand from Settings.
& $Python -m pip install --quiet PyQt6 sounddevice pynput requests numpy sherpa-onnx huggingface_hub
if (-not $?) { throw "pip install failed" }

# --- [3/4] icon --------------------------------------------------------------
Write-Host ""
Write-Host "[3/4] Drawing the icon ..."
$iconScript = @'
import sys
from PyQt6.QtGui import QGuiApplication
app = QGuiApplication(sys.argv)
import gui
gui.write_app_icon(sys.argv[1])
'@
Push-Location $ScriptDir
try {
    $iconScript | & $Python - $Icon
    if (-not $?) { Write-Host "  Could not write $Icon, the shortcut will use the Python icon." }
} finally {
    Pop-Location
}

# --- [4/4] shortcuts -----------------------------------------------------------
Write-Host ""
Write-Host "[4/4] Creating the Start Menu shortcut ..."
function New-DictateShortcut([string]$Path) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($Path)
    $lnk.TargetPath = $PythonW
    $lnk.Arguments = "`"$(Join-Path $ScriptDir 'gui.py')`""
    $lnk.WorkingDirectory = $ScriptDir
    $lnk.Description = "Voice dictation to keyboard"
    if (Test-Path $Icon) { $lnk.IconLocation = $Icon }
    $lnk.Save()
}
$programs = [Environment]::GetFolderPath("Programs")
New-DictateShortcut (Join-Path $programs "Dictate.lnk")
Write-Host "  $programs\Dictate.lnk"

if ($Autostart) {
    $startup = [Environment]::GetFolderPath("Startup")
    New-DictateShortcut (Join-Path $startup "Dictate.lnk")
    Write-Host "  $startup\Dictate.lnk  (starts with Windows)"
}

Write-Host ""
Write-Host "Installed."
Write-Host ""
Write-Host "Launch:"
Write-Host "  - Start Menu: 'Dictate'"
Write-Host "  - Shell:      `"$PythonW`" `"$(Join-Path $ScriptDir 'gui.py')`""
if (-not $Autostart) {
    Write-Host "  - Run this script again with -Autostart to start Dictate with Windows."
}
Write-Host ""
Write-Host "On first launch: open Settings and pick a provider."
Write-Host "  - Groq (cloud, free tier):  https://console.groq.com"
Write-Host "  - OpenAI (cloud):           https://platform.openai.com/api-keys"
Write-Host "  - Local (offline German):   click 'Download model' in Settings (~640 MB, one-time)"
Write-Host ""
Write-Host "Windows Settings > Privacy & security > Microphone must allow desktop apps."
