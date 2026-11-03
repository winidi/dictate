#!/bin/bash
# Dictate server + Headscale on a fresh VPS, from A to Z.
#
#   curl -fsSL https://raw.githubusercontent.com/winidi/dictate/main/server/install-server.sh -o install-server.sh
#   sudo bash install-server.sh
#
# What you need before you start (five minutes, see server/README.md):
#   - a server with Ubuntu 24.04 / 22.04 or Debian 12, 2 GB RAM or more, root access,
#     reachable from the internet on ports 80 and 443 (a router at home must forward them)
#   - a DNS name that points at the VPS (any domain, or a free one from
#     duckdns.org). The Tailscale apps insist on HTTPS for the control server,
#     and the certificate needs a name.
#
# What it does:
#   1. system packages, firewall (22, 80, 443, STUN 3478/udp, WireGuard 41641/udp), automatic updates
#   2. Headscale (pinned version): the private network that joins phone, PC and
#      this server, with Let's Encrypt and a built-in relay for phones behind
#      strict mobile NAT
#   3. the Tailscale client on this VPS, so the server itself is a member of
#      that network (name: dictate-server)
#   4. optional: the Dictate server with the Parakeet model on this VPS, as a
#      system service listening on the private network address only
#   5. a `dictate-server` command: add people, print their QR codes, status, logs
#
# On a server that already runs other things: existing firewall rules stay,
# the firewall is only switched on when nothing else listens, and when a web
# server holds port 80 or 443, headscale runs behind it (nginx: set up by the
# script, others: it prints what to add).
# Re-running the script is safe; it only changes what differs. Nothing here
# ever stores audio.
set -euo pipefail

HEADSCALE_VERSION="${HEADSCALE_VERSION:-0.29.7}"
DICTATE_REPO="${DICTATE_REPO:-https://github.com/winidi/dictate}"
DICTATE_REF="${DICTATE_REF:-main}"
PYTHON_SERIES=3.12
SERVICE_USER=dictate
APP_DIR=/opt/dictate
STATE_DIR=/var/lib/dictate-server          # HOME of the service user: ~/.config/dictate, ~/.local/share/dictate
MAGIC_DOMAIN=dictate.internal              # MagicDNS suffix inside the private network
SERVER_PORT=8000
export DEBIAN_FRONTEND=noninteractive

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[1;32m%s\033[0m\n' "$*"; }
warn() { printf '    \033[1;33m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
ask()  { local var="$1" prompt="$2" default="${3:-}" answer
         if [ -n "$default" ]; then read -r -p "$prompt [$default]: " answer; answer="${answer:-$default}"
         else read -r -p "$prompt: " answer; fi
         printf -v "$var" '%s' "$answer"; }

[ "$(id -u)" -eq 0 ] || die "run as root: sudo bash install-server.sh"
[ -t 0 ] || die "run this from an interactive terminal, it asks a few questions"
. /etc/os-release
case "${ID}-${VERSION_ID}" in
    ubuntu-22.04|ubuntu-24.04|ubuntu-26.04|debian-12|debian-13) ok "OS: $PRETTY_NAME" ;;
    *) warn "Untested OS: $PRETTY_NAME (headscale's packages support Ubuntu 22.04+ and Debian 12+). Continuing." ;;
esac
ARCH="$(dpkg --print-architecture)"
case "$ARCH" in amd64|arm64) ;; *) die "unsupported architecture $ARCH (amd64 or arm64)";; esac

# ------------------------------------------------------------------ questions
say "A few questions"
PUBLIC_IP="$(curl -4fsS https://ifconfig.me 2>/dev/null || curl -4fsS https://icanhazip.com 2>/dev/null || true)"
PUBLIC_IP="${PUBLIC_IP//[[:space:]]/}"
[ -n "$PUBLIC_IP" ] || die "could not find this server's public IPv4 address (no internet?)"
echo "    This server's public address: $PUBLIC_IP"
echo
echo "    Headscale needs a DNS name that points at $PUBLIC_IP. Examples:"
echo "      dictate.example.com        (your own domain, an A record)"
echo "      myname.duckdns.org         (free: duckdns.org -> add a domain -> enter $PUBLIC_IP)"
SAVED_HOST=""; [ -f /etc/headscale/config.yaml ] && SAVED_HOST="$(sed -n 's#^server_url: https://##p' /etc/headscale/config.yaml | head -1)"
while :; do
    ask HOST "DNS name of this server" "$SAVED_HOST"
    HOST="${HOST#https://}"; HOST="${HOST%/}"
    [ -n "$HOST" ] || continue
    RESOLVED="$(getent ahostsv4 "$HOST" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')"
    if [[ " $RESOLVED" == *" $PUBLIC_IP "* ]]; then ok "$HOST -> $PUBLIC_IP"; break; fi
    warn "$HOST resolves to '${RESOLVED:-nothing}', not to $PUBLIC_IP. Fix the DNS record (it can take a few minutes) and try again."
    read -r -p "    Try again (t) or continue anyway (c)? [t]: " a; [ "${a:-t}" = "c" ] && break
done
# Headscale wants ports 80 and 443 (certificate check and control server).
# When a web server already holds them, headscale runs behind it instead:
# on 127.0.0.1:8080 without TLS, the proxy terminates HTTPS.
command -v ss >/dev/null 2>&1 || apt-get install -y -qq iproute2 >/dev/null 2>&1 || true
MODE=direct; PROXY=""
for port in 80 443; do
    OWNER="$(ss -Hltnp "sport = :$port" 2>/dev/null | sed -n 's/.*users:(("\([^"]*\)".*/\1/p' | sort -u | tr '\n' ' ')"
    if [ -n "$OWNER" ] && [[ "$OWNER" != *headscale* ]]; then MODE=proxy; PROXY="${PROXY}${OWNER}"; fi
done
PROXY="$(echo "$PROXY" | tr ' ' '\n' | sort -u | tr '\n' ' ')"
if [ "$MODE" = proxy ]; then
    echo
    warn "Port 80/443 is already used by: $PROXY"
    echo "    Headscale will run behind that web server: it listens on 127.0.0.1:8080 only, and the"
    echo "    web server forwards https://$HOST to it. For nginx the script sets that up itself"
    echo "    (a new site file, certificate via certbot); for Caddy, Apache or a proxy in Docker it"
    echo "    prints what to add and waits. The existing sites are not touched."
    read -r -p "    Continue (y) or stop here without changes (n)? [y]: " a; [ "${a:-y}" = y ] || exit 0
fi
ask ADMIN "Your name (the first user, letters and digits only)" "$(logname 2>/dev/null || echo me)"
ADMIN="$(echo "$ADMIN" | tr -cd 'a-zA-Z0-9-' | tr 'A-Z' 'a-z')"; [ -n "$ADMIN" ] || die "the name must have letters or digits"
ask EMAIL "Email for Let's Encrypt expiry warnings (optional)" ""
RAM_MB=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 ))
CORES="$(nproc)"
DEFAULT_LOCAL=y; [ "$RAM_MB" -lt 1800 ] && DEFAULT_LOCAL=n
echo
echo "    This VPS has $CORES core(s) and $RAM_MB MB RAM."
echo "    Transcription can run here (Parakeet, ~1 GB RAM, about 1-2 s per sentence on a small VPS)"
echo "    or only on your PC at home, with this VPS as the bridge between phone and PC."
ask RUN_LOCAL "Run the transcription on this VPS too? (y/n)" "$DEFAULT_LOCAL"
[ "$RUN_LOCAL" = y ] && [ "$RAM_MB" -lt 1800 ] && warn "Under 2 GB RAM the model may not fit; continuing because you asked."

# ------------------------------------------------------------------ 1 system
say "[1/5] System packages, firewall, automatic updates"
apt-get update -qq
apt-get install -y -qq curl ca-certificates ufw qrencode jq unattended-upgrades apt-listchanges >/dev/null
# Existing rules stay; only ours are added. The firewall is switched on only
# when it was off and nothing else listens for the internet, otherwise other
# services on this server would be cut off.
for rule in "22/tcp ssh" "80/tcp headscale-letsencrypt" "443/tcp headscale-control-and-relay" \
            "3478/udp headscale-stun" "41641/udp tailscale-wireguard"; do
    ufw allow "${rule%% *}" comment "${rule#* }" >/dev/null
done
ufw allow in on tailscale0 comment 'private network' >/dev/null
if ufw status | grep -q "^Status: active"; then
    ok "firewall was already on; rules for 22, 80, 443, 3478/udp, 41641/udp added"
else
    OTHERS="$(ss -Hltn 2>/dev/null | awk '{print $4}' | sed 's/.*://' | sort -un | grep -Evx '22|80|443|53|25|5355|9090' | tr '\n' ' ')"
    if [ -z "$OTHERS" ]; then
        ufw default deny incoming >/dev/null; ufw default allow outgoing >/dev/null
        ufw --force enable >/dev/null
        ok "firewall on: 22, 80, 443, 3478/udp, 41641/udp open, everything else closed"
    else
        warn "the firewall stays OFF: other services listen on port(s) $OTHERS and would be cut off."
        warn "(the dictate server listens on the private network address only, so it is not exposed)"
    fi
fi

# ------------------------------------------------------------------ 2 headscale
say "[2/5] Headscale $HEADSCALE_VERSION"
if ! headscale version 2>/dev/null | grep -q "$HEADSCALE_VERSION"; then
    DEB="/tmp/headscale_${HEADSCALE_VERSION}_linux_${ARCH}.deb"
    curl -fsSL -o "$DEB" "https://github.com/juanfont/headscale/releases/download/v${HEADSCALE_VERSION}/headscale_${HEADSCALE_VERSION}_linux_${ARCH}.deb"
    apt-get install -y -qq "$DEB" >/dev/null
    rm -f "$DEB"
fi
ok "headscale $(headscale version 2>/dev/null | head -1)"
HS_CONFIG="${HEADSCALE_CONFIG:-/etc/headscale/config.yaml}"
mkdir -p "$(dirname "$HS_CONFIG")" /var/lib/headscale
if [ "$MODE" = direct ]; then
    LISTEN="0.0.0.0:443"; TRUSTED="[]"
    TLS_BLOCK="tls_letsencrypt_hostname: ${HOST}
tls_letsencrypt_listen: \":http\"
tls_letsencrypt_cache_dir: /var/lib/headscale/cache
tls_letsencrypt_challenge_type: HTTP-01
acme_url: https://acme-v02.api.letsencrypt.org/directory
acme_email: \"${EMAIL}\"
tls_cert_path: \"\"
tls_key_path: \"\""
else
    LISTEN="127.0.0.1:8080"
    TRUSTED="
  - 127.0.0.1/32
  - ::1/128"
    TLS_BLOCK="# TLS ends at the reverse proxy; headscale logs 'listening without TLS', which is expected.
tls_cert_path: \"\"
tls_key_path: \"\""
fi
cat > "$HS_CONFIG" <<EOF
# Written by dictate's install-server.sh (mode: ${MODE}). Keys as in headscale ${HEADSCALE_VERSION}'s config-example.yaml.
server_url: https://${HOST}
listen_addr: ${LISTEN}
metrics_listen_addr: 127.0.0.1:9090
grpc_listen_addr: 127.0.0.1:50443
grpc_allow_insecure: false
trusted_proxies: ${TRUSTED}
noise:
  private_key_path: /var/lib/headscale/noise_private.key
prefixes:
  v4: 100.64.0.0/10
  v6: fd7a:115c:a1e0::/48
  allocation: sequential
derp:
  # The built-in relay carries traffic when phone and PC cannot reach each
  # other directly (mobile carriers' NAT). Still end-to-end encrypted: the
  # relay sees only WireGuard ciphertext. It rides on the control port.
  server:
    enabled: true
    region_id: 999
    region_code: "dictate"
    region_name: "Dictate relay (${HOST})"
    verify_clients: true
    stun_listen_addr: "0.0.0.0:3478"
    private_key_path: /var/lib/headscale/derp_server_private.key
    automatically_add_embedded_derp_region: true
    ipv4: ${PUBLIC_IP}
  urls:
    - https://controlplane.tailscale.com/derpmap/default
  paths: []
  auto_update_enabled: true
  update_frequency: 3h
disable_check_updates: true
node:
  expiry: 0
  ephemeral:
    inactivity_timeout: 30m
database:
  type: sqlite
  sqlite:
    path: /var/lib/headscale/db.sqlite
    write_ahead_log: true
${TLS_BLOCK}
log:
  level: info
  format: text
policy:
  mode: file
  path: ""
dns:
  magic_dns: true
  base_domain: ${MAGIC_DOMAIN}
  override_local_dns: false
  nameservers:
    global:
      - 1.1.1.1
      - 9.9.9.9
    split: {}
  search_domains: []
  extra_records: []
unix_socket: /var/run/headscale/headscale.sock
unix_socket_permission: "0770"
logtail:
  enabled: false
EOF
chown -R headscale:headscale /etc/headscale /var/lib/headscale 2>/dev/null || true
systemctl enable headscale >/dev/null 2>&1
systemctl restart headscale
sleep 2
if ! systemctl is-active --quiet headscale; then
    journalctl -u headscale -n 30 --no-pager
    die "headscale did not start; the lines above say why"
fi
ok "headscale runs ($MODE)"

if [ "$MODE" = proxy ]; then
    if [[ "$PROXY" == *nginx* ]] && [ -d /etc/nginx/sites-enabled ]; then
        say "nginx: a site for https://${HOST} -> headscale"
        SITE=/etc/nginx/sites-available/dictate-headscale.conf
        if [ ! -f "$SITE" ]; then
            cat > "$SITE" <<EOF
# Headscale (the Dictate private network), written by install-server.sh.
# certbot adds the HTTPS part. Needs the WebSocket upgrade exactly like this.
map \$http_upgrade \$connection_upgrade_headscale {
  default keep-alive;
  ''      close;
}
server {
  listen 80;
  listen [::]:80;
  server_name ${HOST};
  location = /generate_204 { return 204; }
  location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection \$connection_upgrade_headscale;
    proxy_set_header Host \$host;
    proxy_set_header True-Client-IP \$remote_addr;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_redirect http://127.0.0.1:8080 https://${HOST};
    proxy_buffering off;
    proxy_read_timeout 1d;
    proxy_send_timeout 1d;
  }
}
EOF
            ln -sf "$SITE" /etc/nginx/sites-enabled/dictate-headscale.conf
            nginx -t >/dev/null 2>&1 || { rm -f /etc/nginx/sites-enabled/dictate-headscale.conf; die "nginx rejects the new site (nginx -t); the file is $SITE, nothing was enabled"; }
            systemctl reload nginx
            ok "$SITE enabled (HTTP for now)"
        fi
        if ! command -v certbot >/dev/null 2>&1; then
            apt-get install -y -qq certbot python3-certbot-nginx >/dev/null 2>&1 || warn "could not install certbot"
        fi
        if command -v certbot >/dev/null 2>&1; then
            if [ -n "$EMAIL" ]; then MAIL_ARGS=(-m "$EMAIL"); else MAIL_ARGS=(--register-unsafely-without-email); fi
            certbot --nginx -d "$HOST" --non-interactive --agree-tos --redirect "${MAIL_ARGS[@]}" \
                && ok "certificate for $HOST installed, renews itself" \
                || warn "certbot failed (DNS must point here and port 80 must be reachable from the internet). Run later: certbot --nginx -d $HOST"
        fi
    else
        say "Your reverse proxy ($PROXY) needs a site for https://${HOST}"
        echo "    Forward https://${HOST} to http://127.0.0.1:8080 with WebSocket upgrade allowed."
        echo "    The Tailscale protocol upgrades with POST and the header 'Upgrade: tailscale-control-protocol',"
        echo "    so a proxy must pass any Upgrade value, not only 'websocket'. Examples:"
        echo
        echo "    Caddy (Caddyfile):"
        echo "      ${HOST} {"
        echo "        reverse_proxy 127.0.0.1:8080"
        echo "      }"
        echo
        echo "    Apache: ProxyPreserveHost On / ProxyPass / http://127.0.0.1:8080/ upgrade=any  (+ SSL as usual)"
        echo "    Nginx Proxy Manager / Traefik in Docker: proxy host ${HOST} -> http://<this host>:8080, WebSockets on;"
        echo "      headscale must then listen where the container reaches it: set listen_addr in"
        echo "      /etc/headscale/config.yaml to 0.0.0.0:8080, restart headscale, keep 8080 closed in the firewall."
        echo "    Cloudflare proxy/tunnel does not work (no WebSocket POST)."
        echo
        read -r -p "    Press Enter once the proxy forwards https://${HOST} (Ctrl-C to stop) ..." _
    fi
fi

printf '    waiting for https://%s/health ' "$HOST"
for i in $(seq 1 60); do
    if curl -fsS "https://${HOST}/health" >/dev/null 2>&1; then echo; break; fi
    printf '.'; sleep 3
    if [ "$i" -eq 60 ]; then
        echo; journalctl -u headscale -n 20 --no-pager
        echo
        echo "    headscale itself: $(curl -fsS "http://127.0.0.1:${LISTEN##*:}/health" 2>/dev/null || echo 'does not answer on localhost either')"
        die "https://${HOST}/health does not answer. Checks: does ${HOST} resolve to ${PUBLIC_IP} (dig +short ${HOST})? Are ports 80 and 443 open from the internet (a router at home must forward them)? In proxy mode: does the proxy forward to 127.0.0.1:8080?"
    fi
done
ok "https://${HOST} is up"
headscale users list -o json 2>/dev/null | jq -e --arg u "$ADMIN" 'map(select(.name==$u)) | length > 0' >/dev/null 2>&1 \
    || headscale users create "$ADMIN" >/dev/null
ok "headscale user: $ADMIN"

# ------------------------------------------------------------------ 3 tailscale on the VPS
say "[3/5] Tailscale client on this VPS"
if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh >/dev/null 2>&1
fi
systemctl enable --now tailscaled >/dev/null 2>&1
if ! tailscale status --json 2>/dev/null | jq -e '.BackendState == "Running"' >/dev/null; then
    KEY="$(headscale preauthkeys create --user "$ADMIN" --expiration 10m -o json | jq -r '.key // .')"
    tailscale up --login-server "https://${HOST}" --authkey "$KEY" --hostname dictate-server --accept-dns=false
fi
TS_IP="$(tailscale ip -4)"
ok "this VPS in the private network: dictate-server ($TS_IP)"

# ------------------------------------------------------------------ 4 dictate server
if [ "$RUN_LOCAL" = y ]; then
    say "[4/5] Dictate server (Parakeet) on this VPS"
    apt-get install -y -qq build-essential git libportaudio2 >/dev/null
    id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    mkdir -p "$STATE_DIR"; chown "$SERVICE_USER:$SERVICE_USER" "$STATE_DIR"
    if [ -d "$APP_DIR/.git" ]; then
        git -C "$APP_DIR" fetch -q origin "$DICTATE_REF" && git -C "$APP_DIR" checkout -q FETCH_HEAD
    else
        if [ -e "$APP_DIR" ]; then die "$APP_DIR exists but is not a Dictate checkout; move it away first. Nothing was changed."; fi
        git clone -q --depth 1 --branch "$DICTATE_REF" "$DICTATE_REPO" "$APP_DIR"
    fi
    chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
    if ! command -v uv >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null
    fi
    sudo -u "$SERVICE_USER" -H bash -c "
        set -e; cd '$APP_DIR'
        export UV_PYTHON_PREFERENCE=managed UV_CACHE_DIR='$STATE_DIR/.cache/uv'
        [ -x .venv/bin/python ] || uv venv -q --python $PYTHON_SERIES .venv
        uv pip install -q --python .venv/bin/python -r server/requirements-server.txt
        HF_HUB_DISABLE_PROGRESS_BARS=1 .venv/bin/python -m server download-model
    "
    cat > /etc/systemd/system/dictate-server.service <<EOF
[Unit]
Description=Dictate server (speech to text for the private network)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
Environment=HOME=${STATE_DIR}
Environment=PYNPUT_BACKEND=dummy
Environment=HF_HUB_DISABLE_PROGRESS_BARS=1
# Listens on this machine's Tailscale address only: unreachable from the internet,
# whatever the firewall does. Waits for Tailscale at boot, systemd retries otherwise.
ExecStart=${APP_DIR}/.venv/bin/python -m server serve --host tailscale --port ${SERVER_PORT}
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${STATE_DIR}
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable dictate-server >/dev/null 2>&1
    systemctl restart dictate-server
    ok "dictate-server service: http://dictate-server:${SERVER_PORT} (http://${TS_IP}:${SERVER_PORT}) inside the private network"
else
    say "[4/5] Dictate server on this VPS: skipped (your PC does the transcription)"
fi

# ------------------------------------------------------------------ 5 management command
say "[5/5] The dictate-server command"
cat > /usr/local/bin/dictate-server <<'EOF'
#!/bin/bash
# Manage the Dictate server and its private network.
#   dictate-server add <name>      a person or device: network key + dictation token + QR codes
#   dictate-server list            people, devices, tokens
#   dictate-server remove <name>   their tokens and headscale user
#   dictate-server status | logs | update
set -euo pipefail
APP_DIR=/opt/dictate; STATE_DIR=/var/lib/dictate-server; SERVICE_USER=dictate; PORT=8000
HOST="$(sed -n 's#^server_url: https://##p' /etc/headscale/config.yaml | head -1)"
have_local() { [ -f /etc/systemd/system/dictate-server.service ]; }
token_cmd() { sudo -u "$SERVICE_USER" -H env PYNPUT_BACKEND=dummy "$APP_DIR/.venv/bin/python" -m server token "$@"; }
case "${1:-}" in
  add)
    NAME="$(echo "${2:-}" | tr -cd 'a-zA-Z0-9-' | tr 'A-Z' 'a-z')"; [ -n "$NAME" ] || { echo "usage: dictate-server add <name>"; exit 1; }
    headscale users list -o json | jq -e --arg u "$NAME" 'map(select(.name==$u)) | length > 0' >/dev/null || headscale users create "$NAME" >/dev/null
    KEY="$(headscale preauthkeys create --user "$NAME" --reusable --expiration 168h -o json | jq -r '.key // .')"
    TOKEN=""; have_local && TOKEN="$(token_cmd add "$NAME")"
    TS_IP="$(tailscale ip -4 2>/dev/null || true)"
    CODE="dictate://join?control=https://${HOST}&key=${KEY}"
    [ -n "$TOKEN" ] && CODE="${CODE}&server=http://${TS_IP}:${PORT}&token=${TOKEN}"
    echo
    echo "=================== $NAME: one join code for everything ==================="
    echo "$CODE"
    echo
    echo "PC with Dictate (Linux):   ./server/join.sh '$CODE'"
    echo "PC with Dictate (Windows): powershell -ExecutionPolicy Bypass -File server\\join.ps1 '$CODE'"
    echo "  installs Tailscale, joins the network, points Dictate at the server. Add --serve on"
    echo "  Linux to make that PC a server too (its own Parakeet and Gemma for the phone)."
    echo
    echo "Phone: install 'Tailscale' (Play Store / F-Droid):"
    echo "  Settings (gear) -> Accounts -> three dots -> 'Use an alternate server' -> https://$HOST"
    echo "  close the login page -> Accounts -> three dots -> 'Use an auth key' ->"
    echo "     $KEY"
    echo "  then the Dictate app: scan the QR code below (or paste the join code)."
    echo "The key works for 7 days and for several devices."
    if [ -n "$TOKEN" ]; then
      echo
      echo "  server URL:  http://${TS_IP}:${PORT}   (also http://dictate-server:${PORT})"
      echo "  token:       $TOKEN"
      qrencode -t ANSIUTF8 -m 1 "$CODE"
    else
      echo
      echo "This VPS does not transcribe itself: on your PC run ./server/join.sh --serve '$CODE'"
      echo "and give the phone the server URL and token that prints."
    fi
    ;;
  list)
    echo "--- network users ---"; headscale users list
    echo "--- devices ---"; headscale nodes list
    have_local && { echo "--- dictation tokens ---"; token_cmd list; }
    ;;
  remove)
    [ -n "${2:-}" ] || { echo "usage: dictate-server remove <name>"; exit 1; }
    have_local && token_cmd remove "$2" || true
    for id in $(headscale nodes list -o json | jq -r --arg u "$2" '.[] | select(.user.name==$u) | .id'); do headscale nodes delete -i "$id" --force; done
    headscale users destroy --name "$2" --force 2>/dev/null || headscale users destroy "$2" --force 2>/dev/null || true
    echo "removed $2"
    ;;
  status)
    systemctl --no-pager status headscale tailscaled $(have_local && echo dictate-server) | grep -E "●|Active:" || true
    echo; tailscale status || true
    have_local && { echo; curl -fsS "http://$(tailscale ip -4):$PORT/health" | jq . || echo "dictate-server does not answer"; }
    ;;
  logs)  journalctl -u dictate-server -u headscale -n "${2:-50}" --no-pager ;;
  update)
    have_local || { echo "no dictate server on this VPS"; exit 0; }
    git -C "$APP_DIR" fetch -q origin main && git -C "$APP_DIR" checkout -q FETCH_HEAD && chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
    sudo -u "$SERVICE_USER" -H bash -c "cd $APP_DIR && UV_CACHE_DIR=$STATE_DIR/.cache/uv uv pip install -q --python .venv/bin/python -r server/requirements-server.txt"
    systemctl restart dictate-server && echo "updated and restarted"
    ;;
  *) sed -n '2,7p' "$0" ;;
esac
EOF
chmod +x /usr/local/bin/dictate-server
ok "/usr/local/bin/dictate-server"

# ------------------------------------------------------------------ done
say "Done. Your first key and token:"
dictate-server add "$ADMIN"
echo
echo "Next: on your PC with Dictate, pick the provider 'Dictate server', enter the URL and token"
echo "above (after joining the network with tailscale up ... as printed). The phone app follows."
echo "More people: dictate-server add <name>     Status: dictate-server status     Logs: dictate-server logs"
