#!/bin/bash
# Self-installing launcher for Dictate. Every step installs only what is missing:
#   1. system packages (apt, via sudo)
#   2. membership in group `input` (global hotkeys on Wayland)
#   3. uv
#   4. nvm, then Node lts/krypton (activated for the app)
#   5. Python 3.13 (from uv), then ./.venv with the libraries from requirements.txt
#      (versions are checked on every start and updated when they differ)
#   6. the application-menu entry
#
#   ./run.sh           set up whatever is missing, then start the GUI
#   ./run.sh --setup   only set up
# Run it from a terminal the first time: apt needs your sudo password.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(pwd)"

# build-essential + python3-dev: evdev has no wheel and is compiled by pip.
APT_PACKAGES=(xdotool ydotool libnotify-bin libxcb-cursor0 libportaudio2 build-essential python3-dev)
UV_VERSION=0.12.24
NVM_VERSION=v0.40.3
NODE_LTS=lts/krypton        # Node 24
MIN_NODE_MAJOR=24
PYTHON_SERIES=3.13          # installed by uv, newest 3.13.x at the first run, then kept
VENV=.venv
USER_NAME="${USER:-$(id -un)}"
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"

download() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$1"
    else
        echo "curl or wget is needed to download $1" >&2
        exit 1
    fi
}

ensure_system_packages() {
    if ! command -v dpkg-query >/dev/null 2>&1; then
        echo "Not a Debian/Ubuntu system: install these yourself: ${APT_PACKAGES[*]}" >&2
        return 0
    fi
    local missing=() p
    for p in "${APT_PACKAGES[@]}"; do
        dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || missing+=("$p")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        echo "Installing missing system packages: ${missing[*]}"
        sudo apt-get update -qq
        sudo apt-get install -y "${missing[@]}"
    fi
}

can_read_input() {
    local f
    for f in /dev/input/event*; do
        [ -r "$f" ] && return 0
    done
    return 1
}

# On Wayland the compositor does not forward global key events, so Dictate
# reads /dev/input/event* directly. Test that permission itself; only when it
# is missing, look at group `input` (`id -nG <name>` reads the group database,
# which changes right away, while the running session needs a new login).
ensure_input_group() {
    can_read_input && return 0
    # In the group, but the session is older: the app itself says so (and what to do)
    # when it fails to open the devices, right next to the fallback it switches to.
    if id -nG "$USER_NAME" | grep -qw input; then
        return 0
    fi

    cat <<EOF

Dictate needs permission to read your keyboard and mouse devices (/dev/input/event*).

  Why:     the dictation hotkey must work while another window has focus. On Wayland
           the desktop does not pass key presses to other programs, so Dictate reads
           the input devices itself.
  What:    add $USER_NAME to the system group 'input' (sudo usermod -a -G input $USER_NAME).
  Caution: every program running as you can then read all your keystrokes, not only
           Dictate. Agree only if you trust the software on this machine.
  Without: Dictate still runs. On X11 the hotkey keeps working through a fallback;
           on Wayland it may not react outside Dictate's own window.

EOF
    if [ ! -t 0 ]; then
        echo "Not asking (no terminal). Run ./run.sh from a terminal to grant it." >&2
        return 0
    fi
    local answer
    read -r -p "Add $USER_NAME to group 'input' now? [Y/n] " answer || answer=n
    case "${answer:-y}" in
        [Yy]*)
            sudo usermod -a -G input "$USER_NAME"
            cat <<EOF

Done. Linux applies group changes only to sessions started afterwards, so Dictate
keeps using its fallback for now (on X11 the hotkey still works, on Wayland it may
not react outside Dictate's own window).

Please log out and log in again (or reboot).

EOF
            ;;
        *)
            echo "Skipped. Run ./run.sh again if you change your mind."
            ;;
    esac
}

ensure_uv() {
    export PATH="$PATH:$HOME/.local/bin"   # where the uv installer puts it
    if ! command -v uv >/dev/null 2>&1; then
        echo "Installing uv $UV_VERSION ..."
        download "https://astral.sh/uv/$UV_VERSION/install.sh" | INSTALLER_NO_MODIFY_PATH=1 sh
    fi
}

# Active node/npm from nvm that is new enough: nothing to do.
node_is_ready() {
    [[ "$(command -v node 2>/dev/null || true)" == "$NVM_DIR"/* ]] &&
        command -v npm >/dev/null 2>&1 &&
        [ "$(node -p 'process.versions.node.split(".")[0]')" -ge "$MIN_NODE_MAJOR" ]
}

ensure_node() {
    node_is_ready && return 0

    if [ ! -s "$NVM_DIR/nvm.sh" ]; then
        echo "Installing nvm $NVM_VERSION into $NVM_DIR ..."
        # The installer aborts when NVM_DIR is set but missing.
        mkdir -p "$NVM_DIR"
        # PROFILE=/dev/null: leave ~/.bashrc alone; this script activates nvm itself.
        download "https://raw.githubusercontent.com/nvm-sh/nvm/$NVM_VERSION/install.sh" | PROFILE=/dev/null bash
    fi

    # nvm.sh is not written for `set -u`.
    set +u
    # shellcheck disable=SC1091
    . "$NVM_DIR/nvm.sh"
    if ! nvm use "$NODE_LTS" --silent >/dev/null 2>&1; then
        echo "Installing Node $NODE_LTS ..."
        nvm install "$NODE_LTS"
        nvm use "$NODE_LTS" --silent
    fi
    set -u
}

# Python for the venv comes from uv, not from the system: the same version on every
# machine. The newest 3.13.x is installed once; after that it is kept. uv never
# fetches or upgrades a Python on its own here (manual downloads only); to move on,
# run `uv python upgrade` yourself and delete .venv.
ensure_python() {
    export UV_PYTHON_PREFERENCE=only-managed
    export UV_PYTHON_DOWNLOADS=manual
    if ! uv python find "$PYTHON_SERIES" >/dev/null 2>&1; then
        echo "Installing Python $PYTHON_SERIES (the newest $PYTHON_SERIES.x, kept from now on) ..."
        uv python install "$PYTHON_SERIES"
    fi
}

venv_python_series() {
    "$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true
}

ensure_python_libs() {
    ensure_python
    # A venv from an earlier run.sh used the system Python; rebuild it on the pinned series.
    if [ -f "$VENV/pyvenv.cfg" ] && [ -x "$VENV/bin/python" ] &&
        [ "$(venv_python_series)" != "$PYTHON_SERIES" ]; then
        echo "Replacing $VENV (Python $(venv_python_series)) by one with Python $PYTHON_SERIES ..."
        rm -rf "$VENV"
    fi
    if [ ! -x "$VENV/bin/python" ]; then
        uv venv --clear --python "$PYTHON_SERIES" "$VENV"
    fi
    # Preflight: compare the installed libraries with requirements.txt, versions included.
    # A dry run changes nothing and needs no network when everything matches.
    local plan
    if ! plan="$(uv pip install --dry-run --python "$VENV/bin/python" -r requirements.txt 2>&1)"; then
        echo "$plan" >&2
        echo "The libraries in requirements.txt cannot be resolved (see above)." >&2
        exit 1
    fi
    if ! grep -q "Would make no changes" <<<"$plan"; then
        echo "The installed libraries differ from requirements.txt, updating:"
        grep -E '^ [-+] ' <<<"$plan" || true
        uv pip install --python "$VENV/bin/python" -r requirements.txt
    fi
}

# The menu entry starts run.sh, so it keeps working after dependency changes.
ensure_desktop_entry() {
    local dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    local file="$dir/dictate.desktop" entry
    entry="[Desktop Entry]
Type=Application
Name=Dictate
GenericName=Voice dictation
Comment=Voice dictation to keyboard
Exec=\"$SCRIPT_DIR/run.sh\"
Path=$SCRIPT_DIR
Icon=audio-input-microphone
Terminal=false
Categories=Utility;Audio;Accessibility;
StartupNotify=false"
    if [ "$(cat "$file" 2>/dev/null || true)" != "$entry" ]; then
        mkdir -p "$dir"
        printf '%s\n' "$entry" > "$file"
        update-desktop-database "$dir" 2>/dev/null || true
        echo "Wrote menu entry $file"
    fi
}

ensure_system_packages
ensure_input_group
ensure_uv
ensure_node
ensure_python_libs
ensure_desktop_entry

if [ "${1:-}" = "--setup" ]; then
    exit 0
fi

exec "$VENV/bin/python" gui.py "$@"
