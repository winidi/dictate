#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Dictate installer"
echo "================="
echo

echo "[1/3] Installing system packages (needs sudo)..."
sudo apt-get update -qq
sudo apt-get install -y xdotool libnotify-bin python3-pip libxcb-cursor0

echo
echo "[2/3] Installing Python packages..."
pip install --user --break-system-packages \
    PyQt6 sounddevice pynput requests numpy

echo
echo "[3/3] Creating desktop entry..."
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/dictate.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Dictate
GenericName=Voice dictation
Comment=Voice dictation to keyboard (Groq Whisper)
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
echo "On first launch: open Settings and paste your Groq API key."
echo "Free key at https://console.groq.com"
