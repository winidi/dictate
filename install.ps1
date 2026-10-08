<#
.SYNOPSIS
    Installs Dictate on Windows 10/11 for the current user. No admin rights needed.

.DESCRIPTION
    1. Finds Python 3.10 or newer (installs Python 3.12 with winget if missing).
    2. Copies the app to the install folder.
    3. Creates a virtual environment and installs all packages.
    4. Creates a Start Menu shortcut (and optional autostart entry).
    5. Registers an uninstall entry in "Installed apps".

    6. Asks whether to download the offline speech model now (about 640 MB, one
       time) and shows a progress bar. Pass -DownloadModel to say yes without the
       question, or -NoModel to say no. If you skip it, the app downloads the
       model from Settings on first use.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Autostart -DownloadModel
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Proxy http://proxy.example:8080
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\Dictate'),
    [switch]$Autostart,
    [switch]$DownloadModel,
    [switch]$NoModel,
    [switch]$NoShortcuts,
    [switch]$NoRegister,
    [switch]$NoPythonInstall,
    [string]$Proxy,
    [string]$Version = '1.0.0',
    [switch]$FromSetup
)

$ErrorActionPreference = 'Stop'
$AppName = 'Dictate'
$SourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ExitCode = 0

function Write-Step([string]$msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2([string]$msg){ Write-Host "    $msg" -ForegroundColor Yellow }

function Test-PythonCandidate {
    param([string]$Exe, [string[]]$PrefixArgs = @())
    try {
        $out = & $Exe @PrefixArgs -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
        $ver = [version](($out | Select-Object -First 1).ToString().Trim())
        if ($ver -lt [version]'3.10') { return $null }
        return [pscustomobject]@{ Exe = $Exe; Args = $PrefixArgs; Version = $ver.ToString() }
    } catch {
        return $null
    }
}

function Find-Python {
    $cands = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $cands += ,@('py', @('-3'))
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $cands += ,@('python', @())
    }
    foreach ($dir in (Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)) {
        $exe = Join-Path $dir.FullName 'python.exe'
        if (Test-Path $exe) { $cands += ,@($exe, @()) }
    }
    foreach ($c in $cands) {
        $r = Test-PythonCandidate -Exe $c[0] -PrefixArgs $c[1]
        if ($r) { return $r }
    }
    return $null
}

function New-Shortcut([string]$Path, [string]$Target, [string]$Arguments, [string]$WorkDir) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($Path)
    $lnk.TargetPath = $Target
    $lnk.Arguments = $Arguments
    $lnk.WorkingDirectory = $WorkDir
    $lnk.Description = 'Dictate - voice dictation to keyboard'
    $lnk.Save()
}

try {
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $logPath = Join-Path $InstallDir 'install.log'
    Start-Transcript -Path $logPath -Force | Out-Null

    Write-Host "Dictate $Version - installation" -ForegroundColor White
    Write-Host "Install folder: $InstallDir"

    # --- 1. Python -----------------------------------------------------
    Write-Step 'Looking for Python 3.10 or newer'
    $py = Find-Python
    if (-not $py -and -not $NoPythonInstall) {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Warn2 'Python not found. Installing Python 3.12 for the current user with winget ...'
            & winget install --id Python.Python.3.12 -e --scope user --silent --accept-package-agreements --accept-source-agreements
            $py = Find-Python
        }
    }
    if (-not $py) {
        throw ("Python 3.10 or newer was not found and could not be installed.`n" +
               "Install Python from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'),`n" +
               "then run this installer again.")
    }
    Write-Ok "Using Python $($py.Version): $($py.Exe) $($py.Args -join ' ')"

    # --- 2. Stop a running instance, copy files -------------------------
    Write-Step 'Copying application files'
    $running = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$InstallDir*gui.py*" }
    foreach ($p in $running) {
        Write-Warn2 "Stopping running Dictate (PID $($p.ProcessId))"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ((Resolve-Path $SourceDir).Path -ne (Resolve-Path $InstallDir).Path) {
        $files = @('*.py', 'README.md', 'LICENSE', 'requirements-windows.txt', 'install.ps1', 'uninstall.ps1')
        foreach ($pattern in $files) {
            Get-ChildItem -Path $SourceDir -Filter $pattern -File -ErrorAction SilentlyContinue |
                Copy-Item -Destination $InstallDir -Force
        }
    }
    foreach ($need in 'gui.py', 'core.py', 'local_stt.py', 'requirements-windows.txt') {
        if (-not (Test-Path (Join-Path $InstallDir $need))) { throw "Missing file after copy: $need" }
    }
    Write-Ok 'Files copied.'

    # --- 3. Virtual environment and packages -------------------------
    Write-Step 'Creating virtual environment'
    $venv = Join-Path $InstallDir 'venv'
    $venvPy = Join-Path $venv 'Scripts\python.exe'
    $venvPyw = Join-Path $venv 'Scripts\pythonw.exe'
    if (-not (Test-Path $venvPy)) {
        & $py.Exe @($py.Args) -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
    }
    Write-Ok "venv: $venv"

    Write-Step 'Installing packages (this can take a few minutes)'
    $pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check')
    if ($Proxy) { $pipArgs += @('--proxy', $Proxy) }
    & $venvPy @pipArgs --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed. Check the network or proxy.' }
    & $venvPy @pipArgs -r (Join-Path $InstallDir 'requirements-windows.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Package installation failed. Check the network or proxy (-Proxy).' }

    Write-Step 'Checking the installation'
    $env:QT_QPA_PLATFORM = 'offscreen'
    & $venvPy -c "import PyQt6.QtWidgets, sounddevice, pynput, requests, numpy, sherpa_onnx, soundfile, huggingface_hub; print('all modules import OK')"
    if ($LASTEXITCODE -ne 0) { throw 'A package does not import. See install.log in the install folder.' }
    Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue

    # --- 4. Shortcuts -----------------------------------------------------
    if (-not $NoShortcuts) {
        Write-Step 'Creating shortcuts'
        $guiPath = Join-Path $InstallDir 'gui.py'
        $startMenu = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\Dictate.lnk'
        New-Shortcut -Path $startMenu -Target $venvPyw -Arguments ('"' + $guiPath + '"') -WorkDir $InstallDir
        Write-Ok "Start Menu: $startMenu"
        $startupLnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Dictate.lnk'
        if ($Autostart) {
            New-Shortcut -Path $startupLnk -Target $venvPyw -Arguments ('"' + $guiPath + '"') -WorkDir $InstallDir
            Write-Ok "Autostart: $startupLnk"
        } elseif (Test-Path $startupLnk) {
            Write-Warn2 "An autostart entry already exists and stays unchanged: $startupLnk"
        }
    }

    # --- 5. Uninstall entry ------------------------------------------------
    if (-not $NoRegister) {
        Write-Step 'Registering uninstall entry'
        $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Dictate'
        New-Item -Path $key -Force | Out-Null
        $uninst = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $InstallDir 'uninstall.ps1') + '"'
        Set-ItemProperty -Path $key -Name DisplayName -Value $AppName
        Set-ItemProperty -Path $key -Name DisplayVersion -Value $Version
        Set-ItemProperty -Path $key -Name InstallLocation -Value $InstallDir
        Set-ItemProperty -Path $key -Name UninstallString -Value $uninst
        Set-ItemProperty -Path $key -Name NoModify -Value 1 -Type DWord
        Set-ItemProperty -Path $key -Name NoRepair -Value 1 -Type DWord
        Write-Ok 'Dictate now appears in Settings > Apps > Installed apps.'
    }

    # --- 6. Offline speech model -------------------------------------------
    Write-Step 'Offline speech model (Parakeet, German, runs on the CPU)'
    $modelDir = Join-Path $HOME '.local\share\dictate\models\parakeet-primeline-onnx'
    $modelFiles = @('encoder.int8.onnx', 'encoder.int8.onnx.data', 'decoder.int8.onnx', 'joiner.int8.onnx', 'tokens.txt')
    $missing = @($modelFiles | Where-Object { -not (Test-Path (Join-Path $modelDir $_)) })
    if ($missing.Count -eq 0) {
        Write-Ok "Model already installed: $modelDir"
    } else {
        $doModel = $false
        if ($DownloadModel) {
            $doModel = $true
        } elseif ($NoModel) {
            Write-Warn2 'Skipped (-NoModel). Download it later from Settings in the app.'
        } else {
            $answer = $null
            try {
                $answer = Read-Host 'Download the offline model now? About 640 MB, one time. [Y/n]'
            } catch {
                Write-Warn2 'No console input. Skipping the model. Download it later from Settings in the app.'
            }
            if ($null -ne $answer) {
                $doModel = ($answer.Trim() -eq '' -or $answer.Trim() -match '^(y|yes|j|ja)$')
                if (-not $doModel) { Write-Warn2 'Skipped. Download it later from Settings in the app.' }
            }
        }
        if ($doModel) {
            Write-Host '    Downloading from huggingface.co. The bars below show the progress.'
            Remove-Item Env:HF_HUB_DISABLE_PROGRESS_BARS -ErrorAction SilentlyContinue
            $env:PYTHONUNBUFFERED = '1'
            Push-Location $InstallDir
            try {
                & $venvPy -c "import local_stt; local_stt.download_model(lambda m: print('   ', m))"
                if ($LASTEXITCODE -ne 0) {
                    Write-Warn2 'Model download failed. Retry it from Settings in the app.'
                } else {
                    Write-Ok 'Model downloaded.'
                }
            } finally {
                Pop-Location
                Remove-Item Env:PYTHONUNBUFFERED -ErrorAction SilentlyContinue
            }
        }
    }

    Write-Host ""
    Write-Host "Dictate is installed." -ForegroundColor Green
    Write-Host "Start it from the Start Menu: type 'Dictate'. Default hotkey: F9."
    Write-Host "Log file: $logPath"
} catch {
    $ExitCode = 1
    Write-Host ""
    Write-Host "Installation failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Details are in the log file in the install folder."
} finally {
    try { Stop-Transcript | Out-Null } catch { }
}

if ($FromSetup) {
    Write-Host ""
    Read-Host 'Press Enter to close this window'
}
exit $ExitCode
