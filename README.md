# Dictate

Voice dictation to keyboard on Linux, powered by the Groq or OpenAI Whisper API.

Hold a hotkey, speak, release — the transcribed text is typed wherever your cursor is. Works in any application (X11 and native Wayland alike), including editors, terminals, browsers, chat windows.

## Features

- Push-to-talk or tap-to-toggle modes
- System tray icon with status (ready / recording / transcribing)
- Hotkey bound to any function key, modifier (Ctrl / Alt / Shift), or Logitech-style mouse side button
- Groq (`whisper-large-v3-turbo`, `whisper-large-v3`) or OpenAI (`gpt-4o-mini-transcribe`, `gpt-4o-transcribe`, `whisper-1`)
- Optional minimum-hold threshold to ignore accidental modifier presses
- API keys stored locally in `~/.config/dictate/config.json` (mode 600)
- **Wayland-compatible**: uses `evdev` for key capture and `ydotool` for typing, falls back to `pynput`/`xdotool` on X11

## Requirements

- Linux (X11 or Wayland — see the Wayland section below)
- Python 3.10+
- A microphone that PipeWire/PulseAudio can see
- A Groq API key (free tier at [console.groq.com](https://console.groq.com)) or OpenAI API key

## Install

```bash
git clone https://github.com/winidi/dictate.git
cd dictate
./install.sh
```

The installer adds:

- system packages: `xdotool`, `ydotool`, `python3-evdev`, `libnotify-bin`, `python3-pip`, `libxcb-cursor0`
- Python packages: `PyQt6`, `sounddevice`, `pynput`, `requests`, `numpy`
- membership in group `input` (needed for evdev on Wayland)
- a desktop entry so **Dictate** shows up in your app menu

If the installer added you to the `input` group you must log out and back in for it to take effect (or reboot — see WAYLAND_NOTES.md for the `systemd-linger` gotcha that can defeat a plain logout).

## First run

Launch **Dictate** from the app menu (or `python3 gui.py`). On first launch you will be prompted to pick a provider and paste an API key.

## Usage

Default hotkey is **F9**. Hold it, speak, release. The transcription is typed at the cursor.

In **Settings** you can change:

- **Provider** — Groq or OpenAI
- **Mode** — push-to-talk (hold) or toggle (tap to start / tap to stop)
- **Hotkey** — F1–F12, modifier keys (Ctrl / Alt / Shift / Super), or one of the Logitech-style mouse side buttons (`MOUSE BACK`, `MOUSE FORWARD`, `MOUSE SIDE`, `MOUSE EXTRA`, `MOUSE TASK`). Mouse buttons only work with the evdev backend (i.e. `input`-group setup complete).
- **Model** — provider-specific list
- **Min hold to send** — recordings shorter than this duration are discarded. Recommended ~2.0s when bound to Ctrl / Alt / Shift so regular keyboard shortcuts do not trigger recording.

## Wayland notes

On Wayland (GNOME Mutter in particular) the compositor does not forward global key events to XWayland, so `pynput`'s X-based listener sees nothing. Dictate works around this by reading `/dev/input/event*` directly via `evdev`. The `install.sh` script sets this up. Typing likewise uses `ydotool` (via `/dev/uinput`) because `xdotool` cannot type into native Wayland windows.

If a hotkey silently does nothing after install, the two things to check are:

1. `groups | grep input` — you must be in the `input` group in the current session. A fresh login is required; on Ubuntu the "Log out" menu item is often hidden when only one account exists, use `gnome-session-quit --logout --no-prompt` or reboot.
2. The Dictate GUI window has a status label. If it never turns "Recording" while you hold the hotkey, evdev is not receiving events. If it turns "Recording" but not "Transcribing" when you release, your microphone is not producing audio — see the troubleshooting section.

## Troubleshooting

**Hotkey does nothing / status never changes to "Recording..."**

- Verify `groups | grep input` in the same terminal you launched Dictate from
- Check the launcher output for `keyboard listener: evdev`. If it says `evdev unavailable (...) falling back to pynput`, evdev could not open `/dev/input/event*` and pynput will be silently blind on Wayland

**Status turns to "Transcribing..." but never returns to "Ready" (or comes back with empty text)**

- Your PipeWire default source may point at a device that is not currently plugged in. `wpctl status` shows both the runtime default (`*` next to a source) and the *configured* default at the bottom (`Default Configured Node Names`). If the configured default is a mic you have unplugged, `sd.rec()` will hang instead of falling back
- Fix with `wpctl set-default <ID>` for a live source, or pick an input in GNOME Sound Settings

**Text is transcribed (visible in the "Last transcription" pane) but nothing appears in your target window**

- `ydotool` is missing or `/dev/uinput` is not writable. Reinstall with `sudo apt install ydotool` and confirm `python3 -c 'import os; os.open("/dev/uinput", os.O_WRONLY)'` succeeds. On systemd systems membership in `input` should be enough

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
  "threshold": 0.0
}
```

## License

MIT. See [LICENSE](LICENSE).
