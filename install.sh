#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Dictate installer"
echo "================="
echo

echo "[1/4] Installing system packages (needs sudo)..."
# Distro is auto-detected from the package manager; override with
#   ./install.sh --distro=arch|debian
DISTRO=""
for arg in "$@"; do
    case "$arg" in
        --distro=*) DISTRO="${arg#--distro=}" ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done
if [ -z "$DISTRO" ]; then
    if   command -v pacman  >/dev/null; then DISTRO=arch
    elif command -v apt-get >/dev/null; then DISTRO=debian
    else echo "No supported package manager found; use --distro=arch|debian"; exit 1
    fi
fi
echo "  Distro family: $DISTRO"

# xdotool:       X11 typing
# wtype:         Wayland typing that follows the keyboard layout (not GNOME)
# ydotool:       /dev/uinput typing fallback, US layout only
# python evdev:  /dev/input/event* reader, works on Wayland where pynput is blind
# portaudio:     runtime library for the sounddevice wheel
case "$DISTRO" in
    arch)
        sudo pacman -S --needed \
            xdotool wtype ydotool libnotify python-pip xcb-util-cursor python-evdev portaudio
        ;;
    debian)
        sudo apt-get update -qq
        sudo apt-get install -y \
            xdotool wtype ydotool libnotify-bin python3-pip python3-venv libxcb-cursor0 \
            python3-evdev libportaudio2
        ;;
    *) echo "Unknown distro '$DISTRO'"; exit 1 ;;
esac

# ydotool 1.x (Arch, newer Debian) needs its daemon running; Ubuntu's
# 0.1.8 has no such unit and works standalone.
if systemctl --user cat ydotool.service >/dev/null 2>&1; then
    systemctl --user enable --now ydotool.service
fi

echo
echo "[2/4] Installing Python packages into $SCRIPT_DIR/.venv ..."
# Core deps: GUI, audio capture, hotkey listener, HTTP client, arrays.
# Local-STT deps: sherpa-onnx runs Parakeet on CPU offline; soundfile is a
# common companion; huggingface_hub downloads the model on demand.
# A venv (with system site-packages so the distro's python-evdev is visible)
# works on PEP 668 distros such as Arch, where plain pip refuses.
python3 -m venv --system-site-packages "$SCRIPT_DIR/.venv"
"$SCRIPT_DIR/.venv/bin/pip" install \
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
echo "[4/4] Installing 'dictate' command and desktop entry..."
LAUNCHER="$(mktemp)"
cat > "$LAUNCHER" <<EOF
#!/bin/sh
exec "${SCRIPT_DIR}/.venv/bin/python" "${SCRIPT_DIR}/gui.py" "\$@"
EOF
sudo install -Dm755 "$LAUNCHER" /usr/local/bin/dictate
rm -f "$LAUNCHER"

mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/dictate.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Dictate
GenericName=Voice dictation
Comment=Voice dictation to keyboard (Groq / OpenAI Whisper)
Exec=dictate
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
echo "  - From shell:    dictate"
echo
echo "On first launch: open Settings and pick a provider."
echo "  - Groq (cloud, free tier):  https://console.groq.com"
echo "  - OpenAI (cloud):           https://platform.openai.com/api-keys"
echo "  - Local (offline German):   click 'Download model' in Settings (~640 MB, one-time)"
