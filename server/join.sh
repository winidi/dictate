#!/bin/bash
# Join this Linux PC to your Dictate network with the code that
# `dictate-server add <name>` printed on the VPS.
#
#   ./server/join.sh 'dictate://join?control=https://...&key=...&server=http://...&token=dk_...'
#   ./server/join.sh                   (asks for the code)
#   ./server/join.sh --serve CODE      also run the Dictate server on this PC, so the
#                                      phone gets this machine's Parakeet and Gemma
#
# It installs the Tailscale client (official installer), logs it in with the
# key from the code, checks that the server answers, and sets the desktop
# app to the "Dictate server" provider. Re-running it is safe.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$SCRIPT_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[1;32m%s\033[0m\n' "$*"; }
warn() { printf '    \033[1;33m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

SERVE=n
CODE=""
for arg in "$@"; do
    case "$arg" in
        --serve) SERVE=y ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) CODE="$arg" ;;
    esac
done
if [ -z "$CODE" ]; then
    echo "Paste the join code that 'dictate-server add <name>' printed on the server"
    read -r -p "(starts with dictate://join?): " CODE
fi

# dictate://join?control=...&key=...&server=...&token=...  -> four variables
param() { printf '%s' "$CODE" | sed -n "s#.*[?&]$1=\([^&]*\).*#\1#p" | python3 -c 'import sys,urllib.parse; print(urllib.parse.unquote(sys.stdin.read().strip()))'; }
case "$CODE" in dictate://join\?*) ;; *) die "that is not a join code (it starts with dictate://join?)";; esac
CONTROL="$(param control)"; KEY="$(param key)"; SERVER="$(param server)"; TOKEN="$(param token)"
[ -n "$CONTROL" ] && [ -n "$KEY" ] || die "the code has no control server or key"
echo "    network: $CONTROL"
[ -n "$SERVER" ] && echo "    server:  $SERVER"

# ------------------------------------------------------------------ tailscale
say "[1/3] Tailscale client"
if ! command -v tailscale >/dev/null 2>&1; then
    echo "    Installing Tailscale (needs sudo) ..."
    curl -fsSL https://tailscale.com/install.sh | sh
fi
sudo systemctl enable --now tailscaled >/dev/null 2>&1 || true
STATE="$(tailscale status --json 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("BackendState",""), (d.get("CurrentTailnet") or {}).get("Name",""))' 2>/dev/null || true)"
if [[ "$STATE" == Running* ]] && tailscale status --json 2>/dev/null | grep -q "\"ControlURL\": *\"$CONTROL\"" 2>/dev/null; then
    ok "already logged in to $CONTROL"
else
    [[ "$STATE" == Running* ]] && warn "Tailscale is logged in to another network; switching to $CONTROL (tailscale logout first, so the old login stays clean)." && sudo tailscale logout || true
    sudo tailscale up --login-server "$CONTROL" --authkey "$KEY" --accept-dns=true --reset
fi
printf '    waiting for the connection '
for i in $(seq 1 30); do
    if tailscale status >/dev/null 2>&1 && [ -n "$(tailscale ip -4 2>/dev/null)" ]; then echo; break; fi
    printf '.'; sleep 1
    [ "$i" -eq 30 ] && { echo; die "Tailscale did not come up; see: tailscale status"; }
done
ok "this PC in the network: $(hostname) ($(tailscale ip -4))"

# ------------------------------------------------------------------ dictate config
say "[2/3] Dictate settings"
if [ -n "$SERVER" ] && [ -n "$TOKEN" ]; then
    printf '    checking %s ' "$SERVER"
    for i in $(seq 1 15); do
        if curl -fsS -m 3 -H "Authorization: Bearer $TOKEN" "$SERVER/api/tokens" >/dev/null 2>&1; then echo; ok "server answers, token accepted"; break; fi
        printf '.'; sleep 2
        [ "$i" -eq 15 ] && { echo; warn "the server does not answer yet (is the VPS service running? dictate-server status). Settings are written anyway."; }
    done
    # Plain JSON merge with the standard library: works before ./install.sh ran.
    python3 - "$SERVER" "$TOKEN" <<'EOF'
import json, os, sys
path = os.path.expanduser("~/.config/dictate/config.json")
cfg = {}
try:
    cfg = json.load(open(path))
except (OSError, ValueError):
    pass
cfg.update(provider="dictate_server", model="parakeet", server_url=sys.argv[1], server_token=sys.argv[2])
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
os.chmod(path, 0o600)
print("    provider: Dictate server,", sys.argv[1], "->", path)
EOF
else
    warn "the code carries no dictation server (the VPS does not transcribe itself); this PC can be the server, see below"
fi

# ------------------------------------------------------------------ serve
say "[3/3] Serve the phone from this PC"
if [ "$SERVE" = y ]; then
    [ -x "$SCRIPT_DIR/.venv/bin/python" ] || die "run ./install.sh first, the server uses the same .venv"
    cd "$SCRIPT_DIR"
    export PATH="$HOME/.local/bin:$PATH"
    uv pip install --quiet --python .venv/bin/python -r server/requirements-server.txt
    PYNPUT_BACKEND=dummy .venv/bin/python -m server download-model
    mkdir -p ~/.config/systemd/user
    cat > ~/.config/systemd/user/dictate-server.service <<EOF
[Unit]
Description=Dictate server on this PC (for the phone and other devices)
After=network-online.target

[Service]
WorkingDirectory=$SCRIPT_DIR
Environment=PYNPUT_BACKEND=dummy
ExecStart=$SCRIPT_DIR/.venv/bin/python -m server serve --host tailscale --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now dictate-server.service
    MYIP="$(tailscale ip -4)"
    PHONE_TOKEN="$(PYNPUT_BACKEND=dummy .venv/bin/python -m server token add phone)"
    ok "dictate-server runs on http://$MYIP:8000 (systemctl --user status dictate-server)"
    echo
    echo "    For the phone, as a second server (tried first, because it has your Gemma):"
    echo "      server URL:  http://$MYIP:8000"
    echo "      token:       $PHONE_TOKEN"
    command -v qrencode >/dev/null 2>&1 && qrencode -t ANSIUTF8 -m 1 "dictate://setup?server=http://${MYIP}:8000&token=${PHONE_TOKEN}&name=pc"
    echo "    The server listens on the Tailscale address only (not on the LAN or the internet)."
    echo "    Polishing follows your Dictate settings."
else
    echo "    Not now. Later: ./server/join.sh --serve '$CODE'"
fi

echo
ok "Done. Start Dictate; the provider is 'Dictate server'. Hold the hotkey, speak, release."
