# Dictate

Voice dictation to keyboard on Linux, powered by the Groq Whisper API.

Hold a hotkey, speak, release — the transcribed text is typed wherever your cursor is. Works in any X11 application: editors, terminals, browsers, chat windows.

## Features

- Push-to-talk or tap-to-toggle modes
- System tray icon with status (ready / recording / transcribing)
- Hotkey bound to any function key or modifier (Ctrl, Alt, F9, etc.)
- Choice of Groq Whisper models (turbo / large-v3)
- Optional minimum-hold threshold to ignore accidental presses
- API key stored locally in `~/.config/dictate/config.json` (mode 600)

## Requirements

- Linux with X11 (Wayland is not supported — relies on `xdotool` for typing)
- Python 3.10+
- A microphone
- A Groq API key — free tier at [console.groq.com](https://console.groq.com)

## Install

```bash
git clone https://github.com/winidi/dictate.git
cd dictate
./install.sh
```

The installer adds:

- system packages: `xdotool`, `libnotify-bin`, `python3-pip`, `libxcb-cursor0`
- Python packages: `PyQt6`, `sounddevice`, `pynput`, `requests`, `numpy`
- a desktop entry so **Dictate** shows up in your app menu

## First run

Launch **Dictate** from the app menu (or `python3 gui.py`). On first launch you will be prompted to paste your Groq API key. Get one for free at [console.groq.com](https://console.groq.com).

## Usage

Default hotkey is **F9**. Hold it, speak, release. The transcription is typed at the cursor.

In **Settings** you can change:

- **Mode** — push-to-talk (hold) or toggle (tap to start / tap to stop)
- **Hotkey** — F1–F12 or modifier keys (Ctrl / Alt / Shift). When bound to a modifier, raise the min-hold threshold so regular shortcuts do not trigger recording.
- **Model** — `whisper-large-v3-turbo` (fastest, default) or `whisper-large-v3` (most accurate)
- **Min hold to send** — recordings shorter than this duration are discarded. Recommended ~2.0s when bound to Ctrl / Alt / Shift.

## CLI variant

There is also a headless `dictate.py` that reads the API key from the `GROQ_API_KEY` environment variable:

```bash
export GROQ_API_KEY=gsk_...
python3 dictate.py --mode ptt --key f9
```

## Configuration file

`~/.config/dictate/config.json`:

```json
{
  "api_key": "gsk_...",
  "mode": "ptt",
  "key": "f9",
  "model": "whisper-large-v3-turbo",
  "threshold": 0.0
}
```

## License

MIT. See [LICENSE](LICENSE).
