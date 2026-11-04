# Dictate server: dictate from your phone, through your own machines

The desktop app does everything on one PC. The server in this folder puts the
same recognizer (and, on a PC with a GPU, the same written-text model) behind
an HTTP API, so a phone or a second computer can dictate through it. The
connection runs over a private WireGuard network (Tailscale with a
self-hosted Headscale control server), so nothing is exposed to the internet
and nothing is decrypted in between.

Two places can do the work, and the phone app will try them in order:

| Where | Latency | Written text (Gemma) | Needs |
|---|---|---|---|
| **Your PC** at home, `python -m server serve` | about 0.1 s per sentence on a desktop CPU | yes, on the GPU | the PC on and in the private network |
| **The VPS** itself | about 1 to 2 s per sentence on 2 vCPUs | no (too slow without a GPU) | 2 GB RAM, 3 to 5 € a month |

The API is the OpenAI transcription API, so the desktop client (provider
"Dictate server"), the phone app and any OpenAI-compatible tool can use it.

## The VPS, from A to Z

You need: a VPS with Ubuntu 24.04/22.04 or Debian 12 (2 GB RAM or more),
root access, and a DNS name that points at it. The name is unavoidable: the
Tailscale apps accept only an HTTPS control server, and a certificate needs
a name. A free one takes a minute at [duckdns.org](https://www.duckdns.org)
(add a domain, enter the server's IP).

```bash
curl -fsSL https://raw.githubusercontent.com/winidi/dictate/main/server/install-server.sh -o install-server.sh
sudo bash install-server.sh
```

The script asks four things (DNS name, your name, an email for certificate
warnings, whether this VPS should transcribe itself) and then installs:

1. firewall (SSH, 80, 443, STUN, WireGuard) and automatic security updates
2. **Headscale** (pinned version) with a Let's Encrypt certificate and the
   built-in relay, which carries traffic when phone and PC cannot reach each
   other directly (mobile carriers' NAT). The relay sees only ciphertext.
3. the Tailscale client on the VPS, so it is a member of the network as `dictate-server`
4. optionally the Dictate server with the Parakeet model as a system service,
   reachable from the private network only
5. the `dictate-server` command

It ends by printing your own join code. Re-running it is safe.

**A server that already runs other things.** Nothing is deleted. Existing
firewall rules stay and only ours are added; the firewall is switched on
only when it was off and nothing else listens for the internet. The Dictate
server listens on the private network address only, so it is never exposed
whatever the firewall does. An existing `/opt/dictate` that is not a Dictate
checkout is left alone (the script stops).

**A web server already on ports 80 and 443.** Then headscale runs behind
it: on `127.0.0.1:8080` without TLS, the web server terminates HTTPS for
`https://<name>`. With nginx the script does that itself (a new site file
`dictate-headscale.conf`, nothing else touched, certificate through
certbot). With Caddy, Apache or a proxy in Docker it prints what to add and
waits. Headscale's log then says `listening without TLS`, which is expected
in that mode. Cloudflare's proxy does not work in front of headscale.

**A server at home** works too, as long as the router forwards ports 80 and
443 (and 3478/udp for the relay) to it; the DNS check only compares the
name with the public address.

### Adding a person or device: one join code

```bash
dictate-server add anna
```

prints a single join code, `dictate://join?control=...&key=...&server=...&token=...`,
and a QR code of it. It carries everything a device needs: the network, a
key to join it (7 days, several devices), the server address and Anna's
token.

- **PC with Dictate, Linux**: `./server/join.sh '<code>'` installs the
  Tailscale client, joins the network, checks that the server accepts the
  token and switches Dictate to the "Dictate server" provider. Add `--serve`
  and the PC becomes a server as well, with its own Parakeet and Gemma for
  the phone (a user systemd service; it prints a second URL and token for
  the phone).
- **PC with Dictate, Windows**: `powershell -ExecutionPolicy Bypass -File server\join.ps1 '<code>'`
  does the same (Tailscale through winget or its MSI).
- **Phone**: install *Tailscale* (Play Store or F-Droid). Settings → Accounts →
  three dots → *Use an alternate server* → `https://<your name>`. Close the
  login page, then Accounts → three dots → *Use an auth key* → the key from
  the code. Then the Dictate app scans the QR code. (Tailscale's app cannot
  read our QR code itself, so these two taps stay manual.)

`dictate-server list`, `remove <name>`, `status`, `logs` and `update` do what
they say.

### Why Headscale and not plain Tailscale?

Plain Tailscale (the company's free plan, up to 3 users) does the same job
with no VPS, no DNS name and no certificate: install the app, log in with a
Google, Microsoft or GitHub account on phone and PC, done. The encryption is
the same; only the list of your devices and their keys lives at Tailscale.
Headscale keeps that on your own server. The Dictate pieces do not care
which one you use: the control server URL is the only difference, and
`python -m server serve` on your PC works with both.

## The server on your PC

```bash
.venv/bin/python -m server token add anna     # a token for Anna's phone
.venv/bin/python -m server serve              # listens on the PC's Tailscale address
```

It reads your own `config.json`: your vocabulary, your written-text settings
and your Gemma model serve the phone too. `--host 0.0.0.0` serves the LAN
without any VPN; `--threads 4,2,2` overrides the recognizer plan.
`python -m server bench` measures decode times on the machine and checks that
two decodes really run in parallel.

### Several people at once

sherpa-onnx fixes the thread count when a recognizer is created, so the
server keeps several instances of different sizes: the first request gets
the big one (up to four threads), a request that arrives while it is busy
gets a smaller one, and when all are busy the request waits a moment. Each
instance holds its own copy of the model, about a gigabyte, so the RAM
decides the count: a 2-core / 4 GB VPS gets `[2, 1]`, a 4-core / 4 GB machine
`[4, 2]`, with 8 GB `[4, 2, 2]`. Instances load on first use. Nothing is written to disk; the
log holds durations and character counts.

## API

```
POST /v1/audio/transcriptions      Authorization: Bearer dk_...
  multipart: file (WAV any rate; FLAC/OGG/Opus with soundfile), model,
             response_format = text | json | verbose_json, prompt, language
  Dictate extras: polish = 1|0, mode = "Email" | free instructions,
                  app, title (the window the text goes into)
  -> text, or {"text", "raw", "polished", "duration", "decode_s"}
GET  /api/tokens                   200 {"tokens": [{"name": ...}]} for a valid token
GET  /api/modes                    {"polish": bool, "modes": [{"name", "instructions"}]}: the pie-menu
                                   modes of this server, for the phone's ring
GET  /health                       no auth: pool and model state
```

## The phone app (next step, own repository)

The Android app lives in its own repository. It will show a floating bubble
beside the keyboard as Wispr Flow does, insert text through an accessibility
service, and talk to this server with the join and setup codes above,
`/v1/audio/transcriptions`, `/api/tokens` and `/api/modes`.
