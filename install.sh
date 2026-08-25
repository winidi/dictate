#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Dictate installer"
echo "================="
echo

echo "[1/4] Installing system packages (needs sudo)..."
sudo apt-get update -qq
# xdotool: X11 typing fallback
# ydotool: /dev/uinput typing, works in native Wayland windows
# python3-evdev: /dev/input/event* reader, works on Wayland where pynput is blind
# libportaudio2: runtime library for the sounddevice wheel (missing on a fresh Ubuntu desktop)
sudo apt-get install -y \
    xdotool ydotool libnotify-bin python3-pip libxcb-cursor0 python3-evdev libportaudio2

echo
echo "[2/4] Installing Python packages..."
# Core deps: GUI, audio capture, hotkey listener, HTTP client, arrays.
# Local-STT deps: sherpa-onnx runs Parakeet on CPU offline; soundfile is a
# common companion; huggingface_hub downloads the model on demand.
pip install --user --break-system-packages \
    PyQt6 sounddevice pynput requests numpy \
    sherpa-onnx soundfile huggingface_hub

echo
echo "[3/4] Setting up Wayland/evdev access..."
# On Wayland the compositor doesn't forward global key events to XWayland,
# so pynput sees nothing. Dictate reads /dev/input/event* directly via evdev
# instead. That requires membership in group `input`.
if ! id -nG "$USER" | grep -qw input; then
    echo "  Adding $USER to group 'input' (needed for global-hotkey capture on Wayland)."
    sudo usermod -a -G input "$USER"
    echo "  NOTE: log out AND log in again for the new group to take effect."
    echo "        (or reboot — see WAYLAND_NOTES.md for the 'linger' gotcha)"
else
    echo "  Already in group 'input'."
fi

echo
echo "[4/4] Creating desktop entry..."
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/dictate.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Dictate
GenericName=Voice dictation
Comment=Voice dictation to keyboard (Groq / OpenAI Whisper)
Exec=python3 ${SCRIPT_DIR}/gui.py
Icon=audio-input-microphone
Terminal=false
Categories=Utility;Audio;Accessibility;
StartupNotify=false
EOF

update-desktop-database ~/.local/share/applications 2>/dev/null || true

echo
echo "Installed."
echo
echo "Launch:"
echo "  - From app menu: 'Dictate'"
echo "  - From shell:    python3 ${SCRIPT_DIR}/gui.py"
echo
echo "On first launch: open Settings and pick a provider."
echo "  - Groq (cloud, free tier):  https://console.groq.com"
echo "  - OpenAI (cloud):           https://platform.openai.com/api-keys"
echo "  - Local (offline German):   click 'Download model' in Settings (~640 MB, one-time)"
