# Join this Windows PC to your Dictate network with the code that
# `dictate-server add <name>` printed on the VPS.
#
#   powershell -ExecutionPolicy Bypass -File server\join.ps1 'dictate://join?control=...&key=...&server=...&token=...'
#   powershell -ExecutionPolicy Bypass -File server\join.ps1          (asks for the code)
#
# Installs the Tailscale client (winget, or the MSI from tailscale.com), logs
# it in with the key from the code, checks that the server answers and sets
# the desktop app to the "Dictate server" provider. No admin rights needed
# for the login; the Tailscale installer asks for them once.
param(
    [string]$Code = ""
)
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Say($m)  { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    $m" -ForegroundColor Green }
function Warn2($m){ Write-Host "    $m" -ForegroundColor Yellow }

if (-not $Code) {
    Write-Host "Paste the join code that 'dictate-server add <name>' printed on the server"
    $Code = Read-Host "(starts with dictate://join?)"
}
if ($Code -notlike "dictate://join?*") { throw "that is not a join code (it starts with dictate://join?)" }
$q = [System.Web.HttpUtility]::ParseQueryString(($Code -split '\?', 2)[1]) 2>$null
if (-not $q) { Add-Type -AssemblyName System.Web; $q = [System.Web.HttpUtility]::ParseQueryString(($Code -split '\?', 2)[1]) }
$Control = $q["control"]; $Key = $q["key"]; $Server = $q["server"]; $Token = $q["token"]
if (-not $Control -or -not $Key) { throw "the code has no control server or key" }
Write-Host "    network: $Control"
if ($Server) { Write-Host "    server:  $Server" }

# --- 1 Tailscale -------------------------------------------------------------
Say "[1/2] Tailscale client"
$Ts = "$env:ProgramFiles\Tailscale\tailscale.exe"
if (-not (Test-Path $Ts)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "    Installing Tailscale with winget ..."
        & winget install --id tailscale.tailscale -e --accept-package-agreements --accept-source-agreements | Out-Null
    } else {
        Write-Host "    Downloading the Tailscale installer ..."
        $msi = Join-Path $env:TEMP "tailscale-setup.msi"
        Invoke-WebRequest "https://pkgs.tailscale.com/stable/tailscale-setup-latest-amd64.msi" -OutFile $msi -UseBasicParsing
        Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /quiet /norestart" -Wait
    }
    for ($i = 0; $i -lt 30 -and -not (Test-Path $Ts); $i++) { Start-Sleep 1 }
    if (-not (Test-Path $Ts)) { throw "Tailscale did not install; get it from https://tailscale.com/download/windows and run this script again" }
}
# `tailscale login` with a custom control server and an auth key: no browser, no account.
& $Ts login --login-server $Control --auth-key $Key --accept-dns=true 2>&1 | Out-Null
Write-Host -NoNewline "    waiting for the connection "
$ip = ""
for ($i = 0; $i -lt 30 -and -not $ip; $i++) {
    Start-Sleep 1; Write-Host -NoNewline "."
    try { $ip = (& $Ts ip -4 2>$null | Select-Object -First 1) } catch {}
}
Write-Host ""
if (-not $ip) { throw "Tailscale did not come up; open the Tailscale tray icon and check its state" }
Ok "this PC in the network: $env:COMPUTERNAME ($ip)"

# --- 2 Dictate settings ------------------------------------------------------
Say "[2/2] Dictate settings"
if ($Server -and $Token) {
    $okServer = $false
    Write-Host -NoNewline "    checking $Server "
    for ($i = 0; $i -lt 15 -and -not $okServer; $i++) {
        try {
            $r = Invoke-WebRequest "$Server/api/tokens" -Headers @{ Authorization = "Bearer $Token" } -TimeoutSec 3 -UseBasicParsing
            if ($r.StatusCode -eq 200) { $okServer = $true }
        } catch { Start-Sleep 2; Write-Host -NoNewline "." }
    }
    Write-Host ""
    if ($okServer) { Ok "server answers, token accepted" } else { Warn2 "the server does not answer yet (on the VPS: dictate-server status). Settings are written anyway." }
    $cfgDir = Join-Path $env:APPDATA "Dictate"
    $cfgPath = Join-Path $cfgDir "config.json"
    New-Item -ItemType Directory -Force $cfgDir | Out-Null
    $cfg = @{}
    if (Test-Path $cfgPath) {
        $json = Get-Content $cfgPath -Raw | ConvertFrom-Json
        foreach ($p in $json.PSObject.Properties) { $cfg[$p.Name] = $p.Value }
    }
    $cfg["provider"] = "dictate_server"; $cfg["model"] = "parakeet"
    $cfg["server_url"] = $Server; $cfg["server_token"] = $Token
    $cfg | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $cfgPath
    Ok "provider: Dictate server, $Server -> $cfgPath"
} else {
    Warn2 "the code carries no dictation server (the VPS does not transcribe itself)."
}
Write-Host ""
Ok "Done. Start Dictate (restart it if it was running); the provider is 'Dictate server'."
