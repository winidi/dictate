#!/usr/bin/env python3
"""Voice dictation -> keyboard typing via Groq Whisper + xdotool."""
import argparse
import io
import os
import subprocess
import sys
import threading
import wave

import numpy as np
import requests
import sounddevice as sd
from pynput import keyboard

SAMPLE_RATE = 16000
CHANNELS = 1
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"

KEY_MAP = {f"f{i}": {getattr(keyboard.Key, f"f{i}")} for i in range(1, 13)}
KEY_MAP.update({
    "pause":       {keyboard.Key.pause},
    "scroll_lock": {keyboard.Key.scroll_lock},
    "insert":      {keyboard.Key.insert},
    "home":        {keyboard.Key.home},
    "end":         {keyboard.Key.end},
    "page_up":     {keyboard.Key.page_up},
    "page_down":   {keyboard.Key.page_down},
})


def notify(msg, urgency="normal"):
    subprocess.run(
        ["notify-send", "-u", urgency, "-t", "1500", "Dictate", msg],
        check=False,
    )


def type_text(text):
    if not text:
        return
    subprocess.run(["xdotool", "type", "--delay", "0", "--", text], check=False)


class Recorder:
    def __init__(self):
        self.frames = []
        self.stream = None
        self.recording = False
        self.lock = threading.Lock()

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"audio status: {status}", file=sys.stderr)
        self.frames.append(indata.copy())

    def start(self):
        with self.lock:
            if self.recording:
                return
            self.frames = []
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                callback=self._callback,
            )
            self.stream.start()
            self.recording = True
        notify("Recording...", "low")

    def stop(self):
        with self.lock:
            if not self.recording:
                return None
            self.stream.stop()
            self.stream.close()
            self.stream = None
            self.recording = False
            if not self.frames:
                return None
            return np.concatenate(self.frames, axis=0)


def to_wav_bytes(audio):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    buf.seek(0)
    return buf


def transcribe(audio, api_key):
    wav = to_wav_bytes(audio)
    files = {"file": ("audio.wav", wav, "audio/wav")}
    data = {"model": GROQ_MODEL, "response_format": "text"}
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        r = requests.post(GROQ_URL, files=files, data=data, headers=headers, timeout=30)
        r.raise_for_status()
        return r.text.strip()
    except requests.HTTPError:
        notify(f"Groq error: {r.status_code}", "critical")
        print(r.text, file=sys.stderr)
        return None
    except Exception as e:
        notify(f"Transcribe failed: {e}", "critical")
        return None


def handle_audio(recorder, api_key):
    audio = recorder.stop()
    if audio is None:
        notify("No audio", "low")
        return
    notify("Transcribing...", "low")
    text = transcribe(audio, api_key)
    if text:
        type_text(text)
        preview = text if len(text) <= 60 else text[:57] + "..."
        notify(f"Typed: {preview}", "low")


def main():
    p = argparse.ArgumentParser(description="Voice dictation -> keyboard.")
    p.add_argument(
        "--mode",
        choices=["ptt", "toggle"],
        default="ptt",
        help="ptt = hold key to record; toggle = tap to start, tap to stop",
    )
    p.add_argument(
        "--key",
        default="f9",
        help=f"hotkey. choices: {', '.join(KEY_MAP)}",
    )
    args = p.parse_args()

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("GROQ_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    keys = KEY_MAP.get(args.key.lower())
    if keys is None:
        print(f"unknown key: {args.key}\nchoices: {', '.join(KEY_MAP)}", file=sys.stderr)
        sys.exit(1)

    recorder = Recorder()
    key_held = {"down": False}

    def on_press(k):
        if k not in keys:
            return
        if key_held["down"]:
            return
        key_held["down"] = True
        if args.mode == "ptt":
            recorder.start()
        else:
            if recorder.recording:
                threading.Thread(
                    target=handle_audio, args=(recorder, api_key), daemon=True
                ).start()
            else:
                recorder.start()

    def on_release(k):
        if k not in keys:
            return
        key_held["down"] = False
        if args.mode == "ptt":
            threading.Thread(
                target=handle_audio, args=(recorder, api_key), daemon=True
            ).start()

    print(f"Dictate ready. mode={args.mode} key={args.key.upper()}")
    print("Ctrl+C to quit.")
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        try:
            listener.join()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
