# Dictate for Android: the build brief

A dictation app that behaves like Wispr Flow on Android and runs entirely
against your own machines: a floating bubble beside the keyboard, tap or
hold to speak, the text lands in whatever field has the cursor. Speech goes
to a Dictate server (`server/` in this repository) on your PC or VPS over a
private Tailscale/Headscale network; written-text polishing happens there
too. Several people share a server with their own tokens.

This document is the whole specification. It is written so that a developer
or a coding agent can build the app in its own repository (`dictate-android`)
without asking back. Where a decision was made, the reason is given.

## 1. What the app does, in one screen

1. Anna opens WhatsApp and taps into the message field. A small round
   bubble appears next to the keyboard.
2. She holds the bubble and talks, lets go, and half a second later the
   sentence stands in the field, as written text, with punctuation. Or she
   taps once, talks hands-free, taps again.
3. While holding she can swipe a little in a direction: a ring with the
   modes of her server appears (Email, Short message, Formatted, English).
   Release in a segment and the text comes back in that form.
4. Where a field cannot take text (some banking apps, a custom widget), the
   text goes to the clipboard and a notification says so. In password and
   PIN fields the bubble does not appear at all.
5. Nothing is stored. Audio lives in memory for one request; the server
   logs durations and character counts, never text or sound.

## 2. Architecture

```
┌──────────────────────────────── phone ────────────────────────────────┐
│ AccessibilityService  ─ which field has focus, is it editable, which   │
│   (FieldWatcher)        app and window title, insert text              │
│ BubbleService (FGS)   ─ overlay bubble, gestures, states, ring, audio   │
│ Recorder              ─ AudioRecord 16 kHz mono PCM16 → WAV in memory   │
│ ServerClient          ─ OkHttp multipart POST, token, server fallback   │
│ Settings (Compose)    ─ servers, join/setup codes, vocabulary, modes    │
└───────────────────────────────┬───────────────────────────────────────┘
                                │ WireGuard (Tailscale app, Headscale)
                ┌───────────────┴───────────────┐
      PC at home: python -m server serve   VPS: dictate-server (systemd)
      Parakeet + Gemma on the GPU          Parakeet only
```

The Tailscale Android app provides the network; we do not touch WireGuard
ourselves. The Dictate app only needs a server URL (a Tailscale address like
`http://100.64.0.3:8000`) and a token.

## 3. The server API the app talks to

Already implemented in `server/`, see `server/README.md`.

| Call | Purpose |
|---|---|
| `POST /v1/audio/transcriptions` (multipart, `Authorization: Bearer dk_…`) | `file` WAV; `response_format=json`; `polish=1|0`; `mode=<name>`; `app=<package>`; `title=<window title>` → `{"text","raw","polished","duration","decode_s"}` |
| `GET /api/tokens` | 200 when the token is valid; used by "Test connection" |
| `GET /api/modes` | `{"polish": bool, "modes": [{"name","instructions"}]}`; fills the ring |
| `GET /health` | no auth; reachability check |

Error handling the app must implement: 401 → "token rejected, rescan the
code"; 503 with `Retry-After` → wait and retry once, then next server; 413
→ recording too long; connect timeout (3 s) → next server in the list; read
timeout 60 s. The response's `polished=false` with `polish=1` requested
means the server fell back to the raw transcript; show a subtle hint, not an
error.

## 4. Onboarding: the codes

`dictate-server add anna` on the VPS prints one **join code**:

```
dictate://join?control=https://dictate.example.com&key=<preauth key>&server=http://100.64.0.1:8000&token=dk_…&name=anna
```

and `./server/join.sh --serve` on a PC prints a **setup code** for that
server only:

```
dictate://setup?server=http://100.64.0.3:8000&token=dk_…&name=pc
```

The app registers both as deep links (intent filter for scheme `dictate`)
and reads them from a QR scanner (CameraX + ML Kit barcode, or ZXing) or
from the clipboard.

A **join code** drives a three-step screen:

1. *Network.* "Install Tailscale" (opens the Play Store page of
   `com.tailscale.ipn` if missing, else the app). Then two rows with a copy
   button each: the control URL and the key, with the exact taps written out:
   *Settings → Accounts → ⋮ → Use an alternate server → paste URL → close the
   login page → Accounts → ⋮ → Use an auth key → paste key*. The screen
   polls `GET <server>/health` every 2 s and turns green when the network
   is up; no "Next" button needed.
2. *Server.* Added from the code: name, URL, token. "Test connection" calls
   `/api/tokens`.
3. *Permissions.* See section 7.

A **setup code** adds a server to the list (first position if it is a PC
with polish available, see `/api/modes`).

## 5. Behaviour in detail

### 5.1 When the bubble shows

The bubble is visible when **all** hold: an editable text field has focus
(AccessibilityService, `TYPE_VIEW_FOCUSED` / `TYPE_VIEW_TEXT_SELECTION_CHANGED`
on a node with `isEditable`), the soft keyboard is showing (`getWindows()`
contains a window of type `TYPE_INPUT_METHOD`), the field is not a
password/PIN field (`isPassword`, `inputType` numeric or
`TYPE_TEXT_VARIATION_PASSWORD` family), and the foreground package is not on
the user's "never here" list. It hides 300 ms after the keyboard closes and
immediately on a focus change to a non-editable node. A short debounce keeps
it from flickering while apps rebuild their views.

Hidden by default (user-editable list): well-known banking packages; the
user can add any app from a "recent apps" picker. This mirrors Wispr Flow
and is what Play reviewers expect from an accessibility-based app.

### 5.2 Gestures

- **Tap** (down/up under 250 ms, little movement): toggle. First tap starts
  recording, the bubble shows a level meter; second tap stops and sends.
- **Hold** (down longer than 250 ms): push-to-talk. Release stops and sends.
  A hold shorter than the server's "min hold" (default 0.5 s on the phone,
  configurable) is discarded silently.
- **Hold + move** more than 60 dp from the start: the **ring** appears
  around the bubble with the server's modes (first at 12 o'clock, clockwise,
  up to 8, centre = plain written text). The segment follows the finger's
  direction only, distance does not matter beyond the dead zone. Release
  sends with `mode=<segment name>`. The ring is drawn in the overlay, so no
  focus changes.
- **Drag** (move more than 60 dp *before* the hold threshold): reposition
  the bubble; it snaps to the nearest screen edge and remembers the side and
  height per orientation.
- **Long tap on an idle bubble without a field**: open Settings.

Haptics: a short tick on record start, a double tick on stop, a buzz on
error. Sounds optional (off by default; Wispr's chime annoys in meetings).

### 5.3 Recording

`AudioRecord`, source `VOICE_RECOGNITION` (no AGC surprises, mono),
16 kHz, 16-bit PCM, 20 ms frames. Keep the last 300 ms before "stop" as
well (people release early). Hard limit 5 minutes, then stop and send.
Level meter from RMS per frame. Pack to WAV in memory (44-byte header,
little-endian). Opus (`MediaCodec`, 24 kbit/s, in OGG) comes later as an
option "save mobile data"; the server already accepts it through soundfile.

Microphone access from an overlay: Android 14+ allows starting a
foreground service of type `microphone` from the background when the app
holds `SYSTEM_ALERT_WINDOW` **and** has a visible overlay window, which is
exactly our bubble. So: `BubbleService` runs as a foreground service of type
`specialUse` (the bubble) and switches to `microphone|specialUse` while
recording (`startForeground` again with the new type), then back. Declare
both types in the manifest with `FOREGROUND_SERVICE_MICROPHONE` and
`FOREGROUND_SERVICE_SPECIAL_USE` (with the `PROPERTY_SPECIAL_USE_FGS_SUBTYPE`
explanation "floating dictation button").

### 5.4 Sending

Servers are an ordered list. For a dictation: take the first server whose
last health check (every 30 s while the bubble is visible, cached) was good;
on connect failure or 503-after-retry fall through to the next; on 401 skip
the server and flag it in Settings. Send `app` = foreground package name and
`title` = the window title the accessibility service saw, so the server's
per-window notes apply (the desktop does the same). Send `polish=1` when the
user has "written text" on and the server reports `polish: true`.

Local vocabulary: the app keeps the user's own "names and terms" list
(same syntax as the desktop: one per line, `Wrong -> Right` lines are
replacements). Terms go to the server as `prompt` (ignored by Parakeet,
harmless), replacements are applied **on the phone** to the returned text,
whole words, case-insensitive, longest first. This gives every user their
own vocabulary without any per-user state on the server.

### 5.5 Inserting

Insertion goes through the accessibility node that had focus when
recording started (keep a reference and re-find it by `viewIdResourceName`
and window if it was recycled):

1. Read `text`, `textSelectionStart`, `textSelectionEnd`. Build the new
   text: existing text with the selection replaced by the dictation. Add a
   leading space if the character before the cursor is not whitespace or
   an opening bracket, a trailing space if the next character is a letter.
2. `ACTION_SET_TEXT` with the new text, then `ACTION_SET_SELECTION` to put
   the cursor after the inserted text. Verify by reading `text` back; if it
   differs, go to 3.
3. Fallback: put the text on the clipboard, `ACTION_PASTE`, wait 500 ms,
   restore the previous clipboard (text only; if it held an image, leave the
   dictation there). Mark the clip `EXTRA_IS_SENSITIVE` so it stays out of
   the clipboard overlay preview.
4. If the node is gone or neither worked: clipboard + a notification "Text
   copied, paste it with a long press". Never drop a dictation silently.

Multi-line text: `\n` stays `\n` in `ACTION_SET_TEXT`. In chat apps where
Enter sends, that is still correct (a paste does the same); the server's
Email mode produces multi-line text by design.

### 5.6 Feedback states of the bubble

idle (translucent), listening (solid, level ring), sending (spinner),
done (check for 600 ms), error (red for 1.5 s plus a notification with the
reason: no network, server down, token rejected, nothing recognised, too
short). After an error the bubble returns to idle; the recording is kept
in memory for one retry via the notification's "Retry" action, then
dropped.

## 6. Settings (Jetpack Compose)

- **Servers**: ordered list (drag to reorder), each with name, URL, token,
  "Test", status dot, "polish available" badge from `/api/modes`. Buttons:
  Scan code, Paste code, Add manually.
- **Dictation**: tap = toggle / hold = push-to-talk (both on by default),
  min hold, haptics, sounds, keep the bubble on the left/right.
- **Written text**: on/off; modes shown from the first server that has
  polish, read-only, with a note "edit them in Dictate on that PC".
- **Names and terms**: multiline editor, same syntax as the desktop.
- **Apps**: "never show the bubble in" list.
- **Language**: system / Deutsch / English.
- **About / privacy**: what leaves the phone (audio to your servers only),
  what is logged.

## 7. Permissions and onboarding order

1. **Display over other apps** (`SYSTEM_ALERT_WINDOW`): the bubble.
2. **Accessibility service**: the prominent-disclosure screen first (what
   it reads: the focused text field and the app name; what it does: insert
   your dictation; what it never does: read other content, store, send
   anywhere but your servers), then the system settings page. Required for
   Play review of an `AccessibilityService`; declare
   `isAccessibilityTool="true"` only if the app is positioned as an
   accessibility aid, otherwise keep the disclosure and the in-app
   explanation.
3. **Microphone**: requested on the first tap of the bubble, where it makes
   sense to the user.
4. **Notifications** (Android 13+): for errors and the clipboard fallback.
5. **Battery optimisation exemption**: optional, with the explanation that
   some manufacturers kill the bubble service otherwise (Samsung, Xiaomi,
   Huawei).

The app works with 1 and 2 only; 3 is asked when needed; 4 and 5 improve
reliability.

## 8. Multi-user

Nothing in the app is per-user beyond the server list and the vocabulary;
one phone = one person. Several people share servers:

- On the VPS, `dictate-server add <name>` per person: own network user, own
  key, own token. Removing a person removes their token and devices.
- On a PC, `python -m server token add <name>` per person; the PC owner's
  vocabulary and modes apply to everyone on that server (per-token
  settings on the server are a later step; the phone-side vocabulary
  covers the common case).
- The server's pool serves people concurrently (see `server/README.md`).
- A family on one VPS: each phone has the VPS and, if they have one, their
  own PC in the list.

## 9. Project setup

- Kotlin, Gradle (Kotlin DSL), minSdk 29 (Android 10; `getWindows()` and
  `ACTION_SET_TEXT` are solid from here), targetSdk 35, Compose for
  Settings and onboarding, plain `View`s for the overlay (cheaper, no
  Compose in a `WindowManager` window).
- Dependencies: OkHttp, kotlinx.serialization, CameraX + ML Kit barcode
  (or ZXing for F-Droid, no Google dependency), DataStore for settings,
  `EncryptedSharedPreferences` for tokens.
- Modules: `app` (UI, services), `core` (pure Kotlin: WAV writer, cursor
  insertion maths, vocabulary replacements, code parsing, server fallback
  logic) so it is unit-testable without Android.
- Package `de.winidi.dictate` (or your choice); application name "Dictate".
- CI: GitHub Actions builds a debug APK on every push and a signed release
  APK on tags; F-Droid metadata in `metadata/`; Play later.
- Licence MIT like the desktop.

## 10. Tests that must exist

Unit (`core`):
- insertion maths: cursor at start / middle / end, with selection,
  spacing rules, multi-line
- vocabulary: parse, replacements whole-word/case-insensitive/longest-first,
  same cases as `tests/test_core.py` in this repository
- code parsing: join and setup codes, URL-encoded tokens, missing fields
- server fallback: order, 401 skip, 503 retry once, connect failure
- WAV writer: header fields for 16 kHz mono 16-bit, length

Instrumented:
- an `EditText` test activity: `ACTION_SET_TEXT` path and paste fallback
  insert the expected text at the cursor
- bubble shows for an editable field with the keyboard open, hides for a
  password field

Manual acceptance (checklist in the repo): WhatsApp, Gmail, Chrome address
bar, Signal, Telegram, a note app, a terminal emulator (Termux), the
notification inline reply, a banking app (bubble hidden).

## 11. Milestones

1. **Insert and record** (week 1): accessibility service, bubble with
   tap/hold, WAV recording, one server, text inserted. Debug APK.
2. **Codes and servers** (week 2): QR/deep links, server list with
   fallback, onboarding screens, error states, notifications.
3. **Ring, polish, vocabulary** (week 3): `/api/modes`, the ring, `polish`,
   local replacements, settings complete, German and English.
4. **Release** (week 4): F-Droid metadata, signed APK, privacy policy,
   Play closed test; Opus upload as an option.

## 12. Later

- iOS: no microphone in keyboard extensions, so a main app with Live
  Activity plus paste; a different product, decide after Android ships.
- Per-token vocabulary and modes on the server.
- Streaming (send while speaking) once the server has a streaming model;
  Parakeet is a batch model, so today the latency is upload + decode.
- Wear OS / a Bluetooth button as the hotkey.
