#!/bin/bash
# Dictate installer for Linux (Debian/Ubuntu and Arch families).
#
#   ./install.sh [--distro=debian|arch]
#
# Everything lands next to the scripts: the Python packages go into ./.venv,
# so nothing is installed into the system Python (PEP 668 distros such as
# Arch refuse that anyway). System packages come from apt or pacman; the
# distro is detected from the package manager that is present.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

echo "Dictate installer"
echo "================="
echo

DISTRO=""
for arg in "$@"; do
    case "$arg" in
        --distro=*) DISTRO="${arg#--distro=}" ;;
        -h|--help)  sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done
if [ -z "$DISTRO" ]; then
    if   command -v pacman  >/dev/null 2>&1; then DISTRO=arch
    elif command -v apt-get >/dev/null 2>&1; then DISTRO=debian
    else echo "No supported package manager found (apt-get or pacman); use --distro=debian|arch"; exit 1
    fi
fi

echo "[1/4] Installing system packages ($DISTRO, needs sudo)..."
# xdotool:        X11 typing and the paste shortcut
# wtype:          Wayland typing that follows the keyboard layout (KDE, wlroots; not GNOME)
# ydotool:        /dev/uinput typing fallback for GNOME Wayland (US layout only)
# xclip:          clipboard for "Insert text by: Paste" on X11
# python evdev:   /dev/input/event* reader, works on Wayland where pynput is blind
# portaudio:      runtime library for the sounddevice wheel (missing on a fresh Ubuntu desktop)
# libxcb-cursor:  needed by the PyQt6 wheel on X11
case "$DISTRO" in
    debian)
        sudo apt-get update -qq
        sudo apt-get install -y \
            xdotool wtype ydotool xclip libnotify-bin python3-venv python3-pip \
            libxcb-cursor0 python3-evdev libportaudio2
        ;;
    arch)
        sudo pacman -S --needed \
            xdotool wtype ydotool xclip libnotify python xcb-util-cursor python-evdev portaudio
        ;;
    *) echo "Unknown distro '$DISTRO' (debian or arch)"; exit 1 ;;
esac

# ydotool 1.x (Arch, newer Debian) needs its daemon; Ubuntu's 0.1.8 has no
# such unit and works standalone. Without the daemon `ydotool type` fails and
# the text is transcribed but never typed.
if systemctl --user cat ydotool.service >/dev/null 2>&1; then
    systemctl --user enable --now ydotool.service || true
fi

echo
echo "[2/4] Installing Python packages into $VENV ..."
# --system-site-packages makes the distro's python3-evdev visible inside the
# venv, so pip does not have to compile the evdev wheel.
if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$SCRIPT_DIR/requirements.txt"

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
Comment=Voice dictation to keyboard (Groq / OpenAI Whisper / offline Parakeet)
Exec="${VENV}/bin/python" "${SCRIPT_DIR}/gui.py"
Path=${SCRIPT_DIR}
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
echo "  - From shell:    ${VENV}/bin/python ${SCRIPT_DIR}/gui.py"
echo
echo "On first launch: open Settings and pick a provider."
echo "  - Groq (cloud, free tier):  https://console.groq.com"
echo "  - OpenAI (cloud):           https://platform.openai.com/api-keys"
echo "  - Local (offline German):   click 'Download model' in Settings (~640 MB, one-time)"
