#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN=python3

# Derivatives such as CachyOS identify their base distribution in ID_LIKE.
if [[ -r /etc/os-release ]]; then
    . /etc/os-release
fi
case " ${ID:-} ${ID_LIKE:-} " in
    *" arch "*) DISTRO=arch ;;
    *" debian "*|*" ubuntu "*) DISTRO=debian ;;
    *)
        echo "Unsupported distribution: ${PRETTY_NAME:-unknown}. Supported: Debian/Ubuntu and Arch Linux derivatives." >&2
        exit 1
        ;;
esac

echo "Dictate installer"
echo "================="
echo

echo "[1/4] Installing system packages (needs sudo)..."
# xdotool: X11 typing fallback
# ydotool: /dev/uinput typing, works in native Wayland windows
# python3-evdev: /dev/input/event* reader, works on Wayland where pynput is blind
# libportaudio2: runtime library for the sounddevice wheel (missing on a fresh Ubuntu desktop)
if [[ "$DISTRO" == arch ]]; then
    # Do not refresh databases here: pacman -Sy would risk a partial upgrade.
    sudo pacman -S --needed --noconfirm \
        python python-pip python-evdev xdotool ydotool wl-clipboard libnotify \
        xcb-util-cursor libxkbcommon-x11 xkeyboard-config portaudio libsndfile desktop-file-utils
else
    sudo apt-get update -qq
    sudo apt-get install -y \
        xdotool ydotool wl-clipboard libnotify-bin python3-pip libxcb-cursor0 \
        libxkbcommon-x11-0 xkb-data python3-evdev libportaudio2
fi

echo
echo "[2/4] Installing Python packages..."
# Core deps: GUI, audio capture, hotkey listener, HTTP client, arrays.
# Local-STT deps: sherpa-onnx runs Parakeet on CPU offline; soundfile is a
# common companion; huggingface_hub downloads the model on demand.
PIP_ARGS=(--user --break-system-packages)
if [[ "$DISTRO" == arch ]]; then
    # Keep pacman's Python environment intact. Inherit packaged evdev so no
    # compiler or kernel headers are needed to build it with pip.
    python3 -m venv --system-site-packages "${SCRIPT_DIR}/.venv"
    PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python"
    PIP_ARGS=()
fi
"$PYTHON_BIN" -m pip install "${PIP_ARGS[@]}" \
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

if [[ "$DISTRO" == arch ]]; then
    # Arch's ydotool 1.x requires ydotoold, provided as a user service.
    # Reload the packaged udev rule so a first install grants input access.
    sudo modprobe uinput
    sudo udevadm control --reload-rules
    sudo udevadm trigger --name-match=uinput
    if systemctl --user enable ydotool.service; then
        if [[ -w /dev/uinput ]]; then
            if ! systemctl --user restart ydotool.service; then
                echo "  Could not start ydotool. After login, run: systemctl --user restart ydotool.service"
            fi
        else
            echo "  ydotool enabled; it will start after logging out and back in with the input group."
        fi
    else
        echo "  No user systemd session available. After login, run: systemctl --user enable --now ydotool.service"
    fi
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
Exec="${PYTHON_BIN}" "${SCRIPT_DIR}/gui.py"
Icon=audio-input-microphone
Terminal=false
Categories=Utility;AudioVideo;Audio;Accessibility;
StartupNotify=false
EOF

update-desktop-database ~/.local/share/applications 2>/dev/null || true

echo
echo "Installed."
echo
echo "Launch:"
echo "  - From app menu: 'Dictate'"
echo "  - From shell:    \"${PYTHON_BIN}\" \"${SCRIPT_DIR}/gui.py\""
echo
echo "On first launch: open Settings and pick a provider."
echo "  - Groq (cloud, free tier):  https://console.groq.com"
echo "  - OpenAI (cloud):           https://platform.openai.com/api-keys"
echo "  - Local (offline German):   click 'Download model' in Settings (~640 MB, one-time)"
