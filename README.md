# Dictate

Voice dictation to keyboard on Linux — cloud (Groq / OpenAI) or fully offline (Parakeet, German, CPU).

Hold a hotkey, speak, release — the transcribed text is typed wherever your cursor is. Works in any application (X11 and native Wayland alike), including editors, terminals, browsers, chat windows.

## Features

- Push-to-talk or tap-to-toggle modes
- System tray icon with status (ready / recording / transcribing)
- Hotkey bound to any function key, modifier (Ctrl / Alt / Shift), or Logitech-style mouse side button
- Four transcription backends:
  - **Yorik** (home server) — sends the clip to a Yorik install on the LAN (`/v1/audio/transcriptions`, personal API token from Yorik's Settings → API tokens). One Parakeet for every device in the house; this machine carries no model.
  - **Groq** cloud — `whisper-large-v3-turbo`, `whisper-large-v3`
  - **OpenAI** cloud — `gpt-4o-mini-transcribe`, `gpt-4o-transcribe`, `whisper-1`
  - **Local** offline — `parakeet-primeline` (German fine-tune of `nvidia/parakeet-tdt-0.6b-v3`, int8 ONNX via [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)). CPU-only, ~50 ms decode for 2 s audio at 4 threads. No network, no API key.
- Optional minimum-hold threshold to ignore accidental modifier presses
- API keys stored locally in `~/.config/dictate/config.json` (mode 600)
- **Wayland-compatible**: uses `evdev` for key capture and layout-aware `ydotool` key events for character-by-character typing; clipboard paste is optional. Falls back to `pynput`/`xdotool` on X11
- **Windows / macOS**: `pip install PyQt6 sounddevice pynput requests numpy` and run `python gui.py`; hotkeys and typing go through `pynput`. Pair it with the Yorik provider and the machine needs no model.

## Requirements

- Linux (X11 or Wayland — see the Wayland section below)
- Python 3.10+
- A microphone that PipeWire/PulseAudio can see
- Either a Groq API key (free tier at [console.groq.com](https://console.groq.com)), an OpenAI API key — or nothing at all: the Local provider needs no key and no network

## Install

The installer supports **Debian/Ubuntu** and **Arch Linux**, including derivatives
such as **CachyOS** (detected through `/etc/os-release`). On Arch, update your
system with `sudo pacman -Syu` before installing; the installer uses the existing
package databases without refreshing them separately.

```bash
git clone https://github.com/winidi/dictate.git
cd dictate
./install.sh
```

The installer adds:

- Debian/Ubuntu system packages: `xdotool`, `ydotool`, `wl-clipboard`, `python3-evdev`, `libnotify-bin`, `python3-pip`, `libxcb-cursor0`, `libxkbcommon-x11-0`, `xkb-data`, `libportaudio2`
- Arch/CachyOS system packages: `python`, `python-pip`, `python-evdev`, `xdotool`, `ydotool`, `wl-clipboard`, `libnotify`, `xcb-util-cursor`, `libxkbcommon-x11`, `xkeyboard-config`, `portaudio`, `libsndfile`, `desktop-file-utils`
- Python packages: `PyQt6`, `sounddevice`, `pynput`, `requests`, `numpy`, `sherpa-onnx`, `soundfile`, `huggingface_hub`
- membership in group `input` (needed for evdev on Wayland)
- a desktop entry so **Dictate** shows up in your app menu

On Arch/CachyOS, Python packages are installed in `.venv/` inside the checkout,
with access to system packages for pacman's `python-evdev`. The desktop entry
uses that environment automatically; to launch from a terminal, use
`.venv/bin/python gui.py` (or `.venv/bin/python dictate.py` for the X11 CLI).
The installer also enables the packaged `ydotool.service` user service for
Wayland typing. If it cannot connect to your user systemd session, run
`systemctl --user enable --now ydotool.service` after logging in.
After an Arch upgrade changes Python's minor version, recreate `.venv/` and
rerun `./install.sh`.

The Parakeet ONNX model itself (~640 MB) is *not* downloaded by `install.sh` — the download is triggered on demand from Settings when you first pick the local backend, so users who only want cloud transcription don't pay the disk cost.

If the installer added you to the `input` group you must log out and back in for it to take effect (or reboot — see WAYLAND_NOTES.md for the `systemd-linger` gotcha that can defeat a plain logout).

## First run

Launch **Dictate** from the app menu (or `python3 gui.py` on Debian/Ubuntu,
`.venv/bin/python gui.py` on Arch/CachyOS). On first launch you will be prompted
to pick a provider and paste an API key.

## Usage

Default hotkey is **F9**. Hold it, speak, release. The transcription is typed at the cursor.

In **Settings** you can change:

- **Provider** — Groq, OpenAI, or Local (Parakeet DE)
- **Mode** — push-to-talk (hold) or toggle (tap to start / tap to stop)
- **Hotkey** — F1–F12, modifier keys (Ctrl / Alt / Shift / Super), or one of the Logitech-style mouse side buttons (`MOUSE BACK`, `MOUSE FORWARD`, `MOUSE SIDE`, `MOUSE EXTRA`, `MOUSE TASK`). Mouse buttons only work with the evdev backend (i.e. `input`-group setup complete).
- **Wayland insertion** — **Type character by character** (default) sends keyboard events without changing the clipboard. **Clipboard paste** is optional and inserts the whole transcription at once.
- **Typing layout** — `auto` reads the current XWayland keymap, falling back to the system's XKB configuration. If your desktop or the ydotool virtual keyboard uses a different layout, enter its XKB name, such as `de`, `at`, `de(nodeadkeys)`, or `us`. It must match the virtual keyboard's layout. Automatic detection is refreshed for every dictation.
- **Wayland paste** — only used in Clipboard paste mode: `Ctrl+V` for most apps or `Ctrl+Shift+V` for terminals that use that shortcut. Clipboard mode replaces the clipboard contents and leaves the transcription there. Some terminal apps treat Ctrl+V as an image paste; use character typing in those apps. X11 continues to type directly.
- **Model** — provider-specific list (cloud only)
- **CPU threads** (Local only) — sherpa-onnx worker threads. 4 is the sweet spot on typical CPUs; more brings little benefit and starves the rest of the system.
- **Min hold to send** — recordings shorter than this duration are discarded. Recommended ~2.0s when bound to Ctrl / Alt / Shift so regular keyboard shortcuts do not trigger recording.

## Local (offline) transcription

Selecting the **Local (Parakeet DE, CPU offline)** provider gives you German dictation with no network at all. On first selection Settings shows a "Download model (~640 MB)" button — the ONNX weights come from the pinned HuggingFace revision of [`flozen1981/parakeet-primeline-onnx`](https://huggingface.co/flozen1981/parakeet-primeline-onnx) and land in `~/.local/share/dictate/models/parakeet-primeline-onnx/`.

Latency on a modest desktop CPU (measured on a Ryzen 9 workstation, 4 sherpa-onnx threads): about 50 ms decode for a 2 s clip, 100 ms for a 5 s clip. Model load + warmup happens once in the background at Dictate startup (~0.8 s).

Attribution (CC-BY-4.0): [primeline](https://huggingface.co/flozen1981/parakeet-primeline-onnx) (fine-tune), [NVIDIA](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) (base model), [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) (runtime).

## Wayland notes

On Wayland (GNOME Mutter in particular) the compositor does not forward global key events to XWayland, so `pynput`'s X-based listener sees nothing. Dictate works around this by reading `/dev/input/event*` directly via `evdev`. The `install.sh` script sets this up.

By default, text insertion uses libxkbcommon to convert each character into
key presses for the current XKB layout, then sends those events through
`ydotool` (via `/dev/uinput`). German umlauts, `ß`, y/z, punctuation, Shift,
and AltGr combinations are mapped to the matching keys. Text arrives character
by character, without a paste shortcut or clipboard change. This works with
native Wayland windows, including KDE Plasma. Set **Typing layout** to `de` if
automatic detection does not match your German keyboard configuration.

Typing can only produce characters available through the selected layout and
its supported dead-key combinations. If a character cannot be typed (for example,
an emoji), Dictate reports that before inserting any text; it never silently
drops characters or switches to pasting. Older ydotool 0.x also cannot name some
physical keys, such as the ISO key used for `<` on German keyboards.

For those cases, **Clipboard paste** is an explicit alternative: `wl-copy`
copies the exact UTF-8 transcription and `ydotool` sends the selected paste
shortcut. The latest transcription stays on the clipboard, and clipboard
managers may retain it in their history. The target application must accept
the selected shortcut. Choose Ctrl+Shift+V for terminals that require it;
if a terminal handles paste specially, use character typing instead.

If a hotkey silently does nothing after install, the two things to check are:

1. `groups | grep input` — you must be in the `input` group in the current session. A fresh login is required; on Ubuntu the "Log out" menu item is often hidden when only one account exists, use `gnome-session-quit --logout --no-prompt` or reboot.
2. The Dictate GUI window has a status label. If it never turns "Recording" while you hold the hotkey, evdev is not receiving events. If it turns "Recording" but not "Transcribing" when you release, your microphone is not producing audio — see the troubleshooting section.

## Troubleshooting

**A dictation went nowhere**

- Every recording leaves one line in `~/.local/state/dictate/dictate.log` (length, peak level, characters, decode time — never the text). A failed one also raises a desktop notification: no audio frames, silent microphone (`peak` under 300 of 32768), nothing recognised, recording cancelled by another key or mouse button while the hotkey was held, or typing failed
- The local Parakeet model cannot take more than 400 s in one pass; recordings over 90 s are split at a pause and decoded in pieces. Quiet audio is raised in level before decoding, and a piece that still comes back empty is retried in 20 s parts
- A recording that had sound but produced no text is saved as a WAV in `~/.local/state/dictate/failed/`, so the failure can be replayed. This is your own voice on disk: only the newest 3 are kept, nothing is uploaded, and you can delete the folder at any time

**Hotkey does nothing / status never changes to "Recording..."**

- Verify `groups | grep input` in the same terminal you launched Dictate from
- Check the launcher output for `keyboard listener: evdev`. If it says `evdev unavailable (...) falling back to pynput`, evdev could not open `/dev/input/event*` and pynput will be silently blind on Wayland

**Status turns to "Transcribing..." but never returns to "Ready" (or comes back with empty text)**

- Your PipeWire default source may point at a device that is not currently plugged in. `wpctl status` shows both the runtime default (`*` next to a source) and the *configured* default at the bottom (`Default Configured Node Names`). If the configured default is a mic you have unplugged, `sd.rec()` will hang instead of falling back
- Fix with `wpctl set-default <ID>` for a live source, or pick an input in GNOME Sound Settings

**Text is transcribed (visible in the "Last transcription" pane) but nothing appears in your target window**

- On Wayland, rerun `./install.sh` for the XKB dependencies and check **Settings → Typing layout** matches your keyboard (for example, `de`). If using optional Clipboard paste, check its paste shortcut matches the target app. If your terminal reports "failed to paste image", select **Type character by character**.
- `ydotool` is missing or `/dev/uinput` is not writable. Reinstall with `sudo apt install ydotool` on Debian/Ubuntu or `sudo pacman -S ydotool` on Arch/CachyOS and confirm `python3 -c 'import os; os.open("/dev/uinput", os.O_WRONLY)'` succeeds. Log out and back in if you were just added to `input`.
- On Arch/CachyOS, `ydotool` also needs its daemon. Check `systemctl --user status ydotool.service` and start it with `systemctl --user enable --now ydotool.service`. If it fails, inspect `journalctl --user -u ydotool.service -b`; `/dev/uinput` must be writable by your current session. See the [packaged service and udev rule](https://archlinux.org/packages/extra/x86_64/ydotool/files/) and [ydotool's daemon requirement](https://github.com/ReimuNotMoe/ydotool#why-a-background-service-is-needed).

**GUI freezes for a few seconds when releasing the hotkey**

- Should not happen since the audio-stop + transcribe pipeline runs in a worker thread. If it does, check the launcher stderr for exceptions

See `WAYLAND_NOTES.md` for the longer story of the four combined bugs that had to be untangled to make this work on GNOME Wayland with NVIDIA.

## CLI variant

There is also a headless `dictate.py` (unchanged from initial release, uses `pynput` + `xdotool` directly) that reads the API key from the `GROQ_API_KEY` environment variable:

```bash
export GROQ_API_KEY=gsk_...
python3 dictate.py --mode ptt --key f9
```

The CLI variant only works on X11 for the reasons described above. Use `gui.py` on Wayland.

## Configuration file

`~/.config/dictate/config.json`:

```json
{
  "provider": "groq",
  "api_key": "gsk_...",
  "openai_api_key": "sk-...",
  "mode": "ptt",
  "key": "f9",
  "model": "whisper-large-v3-turbo",
  "threshold": 0.0,
  "paste_shortcut": "ctrl+v",
  "insertion_mode": "type",
  "typing_layout": "auto",
  "local_stt_num_threads": 4
}
```

For `provider`, valid values are `"groq"`, `"openai"`, or `"parakeet_local"`.

## License

MIT. See [LICENSE](LICENSE).
