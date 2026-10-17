# Dictate

Voice dictation to keyboard on Linux and Windows — cloud (Groq / OpenAI), a Yorik server on the LAN, or fully offline (Parakeet, German, CPU).

Hold a hotkey, speak, release — the transcribed text is typed wherever your cursor is. Works in any application (X11, native Wayland and Windows alike), including editors, terminals, browsers, chat windows.

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
- **Optional "written text" pass** — a local Gemma 4 model (via llama.cpp) turns the spoken transcript into written text: filler words and false starts go, punctuation and spelling get fixed, your wording and tone stay. It can match your own writing style and adapt to the window the text goes into. Off by default, runtime and model are downloaded on demand, nothing leaves the machine
- API keys stored locally in `~/.config/dictate/config.json` (mode 600) — `%APPDATA%\Dictate\config.json` on Windows
- **Wayland-compatible**: uses `evdev` for key capture and `ydotool` for typing, falls back to `pynput`/`xdotool` on X11
- **Windows**: same features, same four backends. Hotkeys come from a low-level keyboard hook plus a mouse hook (`pynput`), text goes out through `SendInput` as Unicode, so umlauts and ß arrive intact on any layout. Notifications come from the tray icon, `install.ps1` puts Dictate in the Start Menu. macOS runs on the same code path but is untested.

## Requirements

- Linux (X11 or Wayland — see the Wayland section below) or Windows 10/11
- Python 3.10+
- A microphone that PipeWire/PulseAudio (Linux) or Windows can see
- Either a Groq API key (free tier at [console.groq.com](https://console.groq.com)), an OpenAI API key — or nothing at all: the Local provider needs no key and no network

## Install

### Linux

```bash
git clone https://github.com/winidi/dictate.git
cd dictate
./run.sh
```

Run it from a terminal the first time (apt asks for your sudo password). `run.sh` sets up whatever is missing and then starts Dictate; later runs skip everything that is already in place. It adds:

- system packages (apt): `xdotool`, `ydotool`, `libnotify-bin`, `libxcb-cursor0`, `libportaudio2`, `build-essential`, `python3-dev`
- [uv](https://docs.astral.sh/uv/), and a virtualenv in `.venv` with the Python packages from `requirements.txt` (`evdev` is compiled there, hence `build-essential` and `python3-dev`)
- nvm and Node `lts/krypton`
- membership in group `input` (needed for evdev on Wayland)
- a desktop entry so **Dictate** shows up in your app menu

`./run.sh --setup` does the setup without starting the app.

The Parakeet ONNX model itself (~640 MB) is *not* downloaded by `run.sh` — the download is triggered on demand from Settings when you first pick the local backend, so users who only want cloud transcription don't pay the disk cost.

If `run.sh` added you to the `input` group you must log out and back in for it to take effect (or reboot — see WAYLAND_NOTES.md for the `systemd-linger` gotcha that can defeat a plain logout).

### Windows

Install Python 3.10+ from [python.org](https://www.python.org/downloads/windows/) (tick *Add python.exe to PATH*), then in PowerShell:

```powershell
git clone https://github.com/winidi/dictate.git
cd dictate
powershell -ExecutionPolicy Bypass -File install.ps1
```

The installer creates a virtual environment in `.venv` next to the scripts, installs the same Python packages as on Linux (`PyQt6`, `sounddevice`, `pynput`, `requests`, `numpy`, `sherpa-onnx`, `huggingface_hub`), and puts a **Dictate** shortcut in the Start Menu that runs `gui.py` through `pythonw.exe`, so there is no console window. Add `-Autostart` to also start Dictate with Windows. No system packages, no admin rights.

Windows must allow desktop apps to use the microphone: *Settings → Privacy & security → Microphone*.

## First run

Launch **Dictate** from the app menu (or `python3 gui.py`). On first launch you will be prompted to pick a provider and paste an API key.

## Usage

Default hotkey is **F9**. Hold it, speak, release. The transcription is typed at the cursor.

In **Settings** you can change:

- **Language** — the interface in English or German (*Deutsch*); the default follows the system language. Switching applies at once
- **Provider** — Groq, OpenAI, or Local (Parakeet DE)
- **Mode** — push-to-talk (hold) or toggle (tap to start / tap to stop)
- **Hotkey** — F1–F12, modifier keys (Ctrl / Alt / Shift / Super), or one of the Logitech-style mouse side buttons (`MOUSE BACK`, `MOUSE FORWARD`, `MOUSE SIDE`, `MOUSE EXTRA`, `MOUSE TASK`). On Linux the mouse buttons need the evdev backend (i.e. `input`-group setup complete). On Windows the two thumb buttons arrive as `MOUSE BACK` / `MOUSE FORWARD` (`MOUSE SIDE` / `MOUSE EXTRA` are the same two buttons under their Linux names); `MOUSE TASK` is Linux-only. Avoid the Super / Windows key on Windows: releasing it opens the Start menu.
- **Model** — provider-specific list (cloud only)
- **CPU threads** (Local only) — sherpa-onnx worker threads. 4 is the sweet spot on typical CPUs; more brings little benefit and starves the rest of the system.
- **Min hold to send** — recordings shorter than this duration are discarded. Recommended ~2.0s when bound to Ctrl / Alt / Shift so regular keyboard shortcuts do not trigger recording.

## Local (offline) transcription

Selecting the **Local (Parakeet DE, CPU offline)** provider gives you German dictation with no network at all. On first selection Settings shows a "Download model (~640 MB)" button — the ONNX weights come from the pinned HuggingFace revision of [`flozen1981/parakeet-primeline-onnx`](https://huggingface.co/flozen1981/parakeet-primeline-onnx) and land in `~/.local/share/dictate/models/parakeet-primeline-onnx/` (Windows: `%LOCALAPPDATA%\Dictate\models\parakeet-primeline-onnx\`).

Latency on a modest desktop CPU (measured on a Ryzen 9 workstation, 4 sherpa-onnx threads): about 50 ms decode for a 2 s clip, 100 ms for a 5 s clip. Model load + warmup happens once in the background at Dictate startup (~0.8 s). The same model runs unchanged on Windows through the `sherpa-onnx` wheel (measured: 0.6 s decode for a 9 s clip, 4.6 s load + warmup).

Attribution (CC-BY-4.0): [primeline](https://huggingface.co/flozen1981/parakeet-primeline-onnx) (fine-tune), [NVIDIA](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) (base model), [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) (runtime).

## Written text (optional polishing)

People speak differently from how they write. With **Settings → Written text → "Turn the dictation into written text"** the transcript goes through a small local language model before it is typed:

- Filler words (äh, also, halt, quasi, …), false starts and repetitions are removed, self-corrections keep only the corrected version
- Punctuation, capitalisation and spelling are fixed, "neuer Absatz" / "neue Zeile" become real breaks
- Meaning, wording, tone and the form of address stay. A question stays a question
- **Names and terms**: a list of words the recognizer gets wrong by sound (your name, product names, jargon). The model replaces sound-alikes with the listed spelling, e.g. "Parakit" → "Parakeet"
- **Your style**: paste a few of your own texts into the "How you write" box and the model matches that voice
- **Per window**: one line per destination, e.g. `linkedin = Beruflich, aber locker. Kurze Absätze.` The word on the left is looked for in the title and program name of the window that will receive the text. `* = …` is the default. (Window titles are not available on GNOME Wayland, the default applies there.)

**Pie menu — pick a mode while you speak.** With the hotkey held, move the mouse a little in a direction (or tap an arrow key): a translucent circle at the pointer shows the configured modes, e.g. *English* at the top and *Formatted* at the bottom, with *Standard* in the centre. Release, and the dictation is written that way — translated, structured, whatever the mode's instruction says. Only the direction counts, so the pointer may leave the circle; no movement means plain written text. Modes are one line each in Settings (`English = Translate the text into natural English.`), up to eight, the first at 12 o'clock. Push-to-talk only. On GNOME Wayland no window can be drawn at the pointer, so the current mode is shown in the shell's own on-screen display (the volume pill) instead; the gesture itself works the same. The choice never depends on the picture: the main window shows which mode was applied.

Nothing is downloaded on its own. When you switch the feature on and no model is installed, Dictate asks whether you already run one (then it only sends requests there) or whether it should download llama.cpp and Gemma 4; the download button asks once more with the exact size and the free space on the disk.

This is a helper, never a gate: if the model is still loading, too slow (default limit 8 s) or not installed, the raw transcript is typed and a notification says so. The tray menu has a quick on/off switch. The main window shows the written text and, below it, what was said.

**Already running a model?** Set "Model source" to *A server that is already running* and enter the base URL of any OpenAI-compatible server — Ollama (`http://127.0.0.1:11434/v1`), LM Studio (`http://127.0.0.1:1234/v1`), your own `llama-server`, vLLM — plus the model name it expects (e.g. `gemma4:e2b`) and an API key if it wants one. "Test connection" lists the models the server offers. Dictate then downloads nothing and starts nothing; it only sends the prompt. Text leaves your machine only if that URL points to another one.

**What runs underneath** (built-in source): [llama.cpp](https://github.com/ggml-org/llama.cpp)'s `llama-server` from a pinned release build, started by Dictate as a child process on `127.0.0.1` and stopped with it, plus a pinned GGUF of [Gemma 4](https://huggingface.co/collections/google/gemma-4) (Apache 2.0). Both are downloaded from Settings into `~/.local/share/dictate/` (Windows: `%LOCALAPPDATA%\Dictate\`) — nothing is installed system-wide, no Python packages are added. Choices:

| Model | Download | Notes |
|---|---|---|
| Gemma 4 E2B, QAT Q4_0 (default) | 3.4 GB | Google's own 4-bit quantisation. Fast, fits any GPU and runs on a CPU |
| Gemma 4 E2B, Q8_0 | 5 GB | Best E2B quality |
| Gemma 4 E4B, QAT Q4_0 | 5.2 GB | Smarter on convoluted sentences |
| Gemma 4 E4B, Q8_0 | 8 GB | Best quality |

"Run on" picks the llama.cpp build: **Auto** uses the NVIDIA CUDA build when an NVIDIA driver is present (CUDA 13.4 driver or newer, ~1.3 GB extra download for the CUDA runtime) and the CPU build otherwise; **Vulkan** covers AMD and Intel GPUs. Measured on an RTX 5090 with E2B Q4_0: model load 2 s at startup, 0.1–0.6 s per dictation. On a CPU expect a few seconds for a long dictation; set "Give up after" accordingly.

The polished text is logged only as a character count, like everything else.

## Wayland notes

On Wayland (GNOME Mutter in particular) the compositor does not forward global key events to XWayland, so `pynput`'s X-based listener sees nothing. Dictate works around this by reading `/dev/input/event*` directly via `evdev`. `run.sh` sets this up. Typing likewise uses `ydotool` (via `/dev/uinput`) because `xdotool` cannot type into native Wayland windows.

If a hotkey silently does nothing after install, the two things to check are:

1. `groups | grep input` — you must be in the `input` group in the current session. A fresh login is required; on Ubuntu the "Log out" menu item is often hidden when only one account exists, use `gnome-session-quit --logout --no-prompt` or reboot.
2. The Dictate GUI window has a status label. If it never turns "Recording" while you hold the hotkey, evdev is not receiving events. If it turns "Recording" but not "Transcribing" when you release, your microphone is not producing audio — see the troubleshooting section.

## Troubleshooting

**A dictation went nowhere**

- Every recording leaves one line in `~/.local/state/dictate/dictate.log` (Windows: `%LOCALAPPDATA%\Dictate\dictate.log`) — length, peak level, characters, decode time, never the text. A failed one also raises a desktop notification: no audio frames, silent microphone (`peak` under 300 of 32768), nothing recognised, recording cancelled by another key or mouse button while the hotkey was held, or typing failed
- The local Parakeet model cannot take more than 400 s in one pass; recordings over 90 s are split at a pause and decoded in pieces. Quiet audio is raised in level before decoding, and a piece that still comes back empty is retried in 20 s parts
- A recording that had sound but produced no text is saved as a WAV in `~/.local/state/dictate/failed/` (Windows: `%LOCALAPPDATA%\Dictate\failed\`), so the failure can be replayed. This is your own voice on disk: only the newest 3 are kept, nothing is uploaded, and you can delete the folder at any time

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

**Windows**

- The log says `keyboard listener: pynput` on every start; there is no evdev here and nothing to set up
- Hotkey does nothing while a program that runs *as Administrator* has the focus: Windows does not deliver keyboard hooks from a normal process to elevated windows. Start Dictate from an elevated PowerShell if you need to dictate into such a program
- Status turns "Recording..." but every dictation reports a silent microphone: check *Settings → Privacy & security → Microphone → Let desktop apps access your microphone*, and that the right device is the default input in *Settings → System → Sound*
- No console window means no stderr. Everything worth knowing is in `%LOCALAPPDATA%\Dictate\dictate.log`; to see live output run `.venv\Scripts\python.exe gui.py` instead of the shortcut

See `WAYLAND_NOTES.md` for the longer story of the four combined bugs that had to be untangled to make this work on GNOME Wayland with NVIDIA.

## CLI variant

There is also a headless `dictate.py` (unchanged from initial release, uses `pynput` + `xdotool` directly) that reads the API key from the `GROQ_API_KEY` environment variable:

```bash
export GROQ_API_KEY=gsk_...
python3 dictate.py --mode ptt --key f9
```

The CLI variant only works on X11 for the reasons described above. Use `gui.py` on Wayland.

## Configuration file

`~/.config/dictate/config.json` (Windows: `%APPDATA%\Dictate\config.json`):

```json
{
  "ui_language": "auto",
  "provider": "groq",
  "api_key": "gsk_...",
  "openai_api_key": "sk-...",
  "mode": "ptt",
  "key": "f9",
  "model": "whisper-large-v3-turbo",
  "threshold": 0.0,
  "local_stt_num_threads": 4,
  "polish_enabled": false,
  "polish_backend": "builtin",
  "polish_external_url": "http://127.0.0.1:11434/v1",
  "polish_external_model": "",
  "polish_external_key": "",
  "polish_model": "gemma4-e2b-q4",
  "polish_device": "auto",
  "polish_threads": 4,
  "polish_timeout": 8.0,
  "polish_style": "",
  "polish_vocabulary": "Dirk Winiecki\nParakeet",
  "polish_default_instructions": "",
  "polish_profiles": [{"match": "linkedin", "instructions": "Beruflich, aber locker."}],
  "polish_modes": [{"name": "English", "instructions": "Translate the text into natural English."}]
}
```

`polish_model` is one of `gemma4-e2b-q4`, `gemma4-e2b-q8`, `gemma4-e4b-q4`, `gemma4-e4b-q8`; `polish_device` one of `auto`, `cuda`, `vulkan`, `cpu`. `polish_profiles: null` and an empty `polish_default_instructions` use the built-in examples.

For `provider`, valid values are `"groq"`, `"openai"`, or `"parakeet_local"`.

## License

MIT. See [LICENSE](LICENSE).
