# Wayland notes — the four-bug debug story

This file exists because the same set of bugs will very likely bite anyone else porting a "hold hotkey, transcribe, type" pipeline to modern Ubuntu GNOME on Wayland. It is the log of a 2026-08-02 debugging session on a workstation with:

- Ubuntu 24.04 LTS, GNOME on Wayland (Mutter)
- NVIDIA RTX 5090 with the `595-open` DKMS driver
- Logitech MX Keys + MX Master 3 via Solaar
- PipeWire + WirePlumber

Dictate had worked fine on the same machine two months earlier. Then it stopped. The user rightly refused to accept "it works on my machine" hand-waving; the actual cause turned out to be four *independent* regressions stacked on top of each other. Any single one causes silent failure. Fixing them one at a time only surfaces the next.

## Bug 1 — pynput X11 backend sees zero events on Wayland

`pynput.keyboard.Listener` on Linux is an alias for `pynput.keyboard._xorg.Listener`, which uses the X11 `RECORD` extension. On Wayland this routes through XWayland. Modern Mutter does not forward global (non-focused) key events to XWayland's XRecord tap. Result: a 10-second listener test with `python3 -c "from pynput import keyboard; ..."` receives **zero events**, no matter which app has focus.

The user had been running an Xorg session before an NVIDIA driver upgrade. Look at `/var/log/apt/history.log`:

```
2026-08-02 00:12:58  apt-get install -y nvidia-dkms-595-open nvidia-driver-580-open-
2026-08-02 01:16:35  apt-get install -y xserver-xorg-video-nvidia-595
```

Between 00:13 and 01:16 there was **no Xorg NVIDIA module installed**. Any GDM restart in that window would have detected "no Xorg driver → default to Wayland" and stored that as the user's session preference. The user did not notice the flip.

**Fix:** read `/dev/input/event*` directly via `python-evdev`. See `evdev_listener.py` — it exposes the same `on_press`/`on_release` shape as `pynput.keyboard.Listener` so `gui.py` can swap in one line. Requires membership in group `input`.

## Bug 2 — xdotool cannot type into native Wayland windows

`xdotool type` uses X11's `XTEST` extension. Native Wayland windows (Firefox with the Wayland backend, GNOME Terminal, GNOME Files, most modern GTK4 apps) do not accept synthetic input from XWayland. The characters go into the void with no error.

**Fix:** use `ydotool` instead, which writes to `/dev/uinput`. That is a kernel interface below both X and Wayland, so any focused window receives the keystrokes. Ubuntu 24.04's `ydotool` 0.1.8 works standalone (no `ydotoold` daemon needed), just warns about missing daemon. `/dev/uinput` is accessible when the user is in the `input` group (the ACL is granted automatically by `systemd-logind`).

`core.type_text` tries `ydotool` first and falls back to `xdotool` if the binary is missing.

## Bug 3 — `sd.InputStream.stop()` freezes the Qt main thread

The original `stop_and_transcribe` in `gui.py` called `self.recorder.stop()` (which calls `sd.InputStream.stop()` and `.close()`) directly from the Qt slot for hotkey release. On PipeWire that close can take hundreds of milliseconds — long enough that the Qt event loop is blocked, `set_status()` calls do not repaint, and to the user it looks exactly like the release event was never delivered ("recording stays showing forever").

Symptom in the debug log we added:

```
[dbg] main on_release ENTER key_held=True rec_active=True
[dbg] evdev on_press k=<OtherKey 272>  ← the user clicked the mouse *while GUI was frozen*
```

No further status output between "on_release ENTER" and the next evdev event — the main thread was stuck inside `PulseAudio/PipeWire stream_close`.

**Fix:** `gui.py._stop_and_transcribe_worker` runs the entire pipeline (recorder.stop → API call → emit transcribed) in a `threading.Thread`. Main thread only calls `set_status("transcribing", ...)` synchronously (which is fast) and returns immediately. The two "discard" branches (`on_release` with duration < threshold, and `on_other_press` combo-cancel) also offload `recorder.stop()` to a throwaway daemon thread.

## Bug 4 — PipeWire default source pointing at an unplugged mic

`wpctl status` on the broken system:

```
Sources:
 *  55. OBSBOT Meet 2 Analog Stereo
    57. HyperX 7.1 Audio Analog Stereo
Settings
 └─ Default Configured Node Names:
        1. Audio/Source  alsa_input.usb-352f_PD400X_Podcast_Microphone_...-00.mono-fallback
```

The user's **configured** default source was a Podcast Microphone that was not currently connected. WirePlumber fell back to OBSBOT at runtime (`*`), but for reasons we did not fully diagnose (probably an ALSA-plugin path that trusts the configured name), `sd.rec(device='default', samplerate=16000)` **hung indefinitely** instead of falling back cleanly. The `_callback` in `core.Recorder` was never called, `self.frames` stayed empty, and `recorder.stop()` returned `None`.

Directly opening the hardware devices returned `paInvalidSampleRate` because HyperX / ALC1220 do not natively support 16kHz. The path that had worked historically was `default` → PipeWire → resample to 16kHz. Once the configured default became stale, that path broke silently.

**Fix at the OS level, not in Dictate code:**

```
wpctl set-default 56    # (56 = HyperX in this snapshot; use current ID from wpctl status)
```

or pick an input in GNOME Sound Settings. The bug will silently return if the user again unplugs the mic they set as default. If you want Dictate to defend against it, wrap `sd.rec` with a small timeout and fall back to `sd.query_devices()`-and-pick-any-live-input logic — we did not add that here.

## Bonus gotcha — `systemd --user` linger defeats logout

The `input` group membership added by `usermod -a -G input isee` did not appear in the user's shells even after a full GDM logout / login. `id` in a fresh terminal did NOT show group 995 (input); `getent group input` did. The reason:

```
loginctl show-user isee -p Linger
Linger=yes
```

With linger on, `user@1000.service` (the systemd --user manager, and everything it spawns — `gnome-terminal-server`, `mutter`, ...) keeps running past logout. On next login GDM re-attaches to the existing user-manager, so freshly launched processes inherit the old process credentials.

**Fix:** `loginctl disable-linger isee` followed by logout / login, or just reboot. Alternatively `sg input -c 'python3 gui.py'` runs the command with the correct primary group without needing a session restart.

## Diagnosis order for future breakage

If Dictate goes silent again, run these in order — the first one that reveals a problem is the culprit:

```bash
# 1. Group / evdev access
groups | grep -q input && echo OK || echo "not in input group"

# 2. Can we read /dev/input?
python3 -c "import evdev; d=evdev.InputDevice('/dev/input/event5'); print('read OK')"

# 3. Does sounddevice see input?
python3 -c "
import sounddevice as sd, numpy as np
r = sd.rec(int(2*16000), samplerate=16000, channels=1, dtype='int16'); sd.wait()
print('peak', int(np.abs(r).max()))"
# expect peak > 500 after speaking; 0 means bug 4; hang means bug 4 hardcore

# 4. Does ydotool type?
ydotool type "typed by ydotool"
# should appear in whatever window has focus

# 5. Is Dictate using evdev?
grep -q "keyboard listener: evdev" <(python3 /home/isee/dictate/gui.py 2>&1)
```

If all five pass and Dictate still misbehaves, something newer than 2026-08-02 broke it — grep this file for the debug prints (`[dbg]`) shown above and re-add them to see where the pipeline stops.
