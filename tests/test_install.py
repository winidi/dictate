"""Exercise installer routing without changing packages, groups or services.

Run with: python3 -m unittest discover -s tests -v
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"
STUB = """#!/usr/bin/python3
import json
import os
from pathlib import Path
import sys

command = Path(sys.argv[0]).name
with open(os.environ['INSTALL_LOG'], 'a') as log:
    log.write(json.dumps([command, *sys.argv[1:]]) + '\\n')
if command == 'id':
    print(os.environ.get('TEST_GROUPS', 'users input'))
elif command == 'python3' and sys.argv[1:3] == ['-m', 'venv']:
    python = Path(sys.argv[-1]) / 'bin' / 'python'
    python.parent.mkdir(parents=True)
    python.write_text(Path(sys.argv[0]).read_text())
    python.chmod(0o755)
elif command == 'systemctl' and os.environ.get('TEST_NO_SESSION'):
    sys.exit(1)
elif command == 'systemctl' and 'restart' in sys.argv and os.environ.get('TEST_START_FAIL'):
    sys.exit(1)
"""


class InstallerTests(unittest.TestCase):
    def run_installer(self, release, uinput_writable=True, **extra_env):
        with tempfile.TemporaryDirectory(prefix="dictate install ") as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            commands = root / "commands"
            commands.mkdir()
            for command in ("sudo", "python3", "id", "systemctl", "update-desktop-database"):
                stub = commands / command
                stub.write_text(STUB)
                stub.chmod(0o755)
            os_release = root / "os-release"
            os_release.write_text(release)
            uinput = root / "uinput"
            if uinput_writable:
                uinput.touch()
            installer = root / "install.sh"
            # Substitute only the OS fixture in a temporary copy of the real
            # script. Production detection always reads /etc/os-release.
            script = INSTALLER.read_text().replace("/etc/os-release", f'"{os_release}"')
            script = script.replace("[[ -w /dev/uinput ]]", f'[[ -w "{uinput}" ]]')
            installer.write_text(script)
            log = root / "commands.jsonl"
            env = dict(os.environ, HOME=str(home), USER="tester",
                       PATH=f"{commands}:/usr/bin:/bin", INSTALL_LOG=str(log), **extra_env)
            result = subprocess.run(["/bin/bash", str(installer)], env=env,
                                    capture_output=True, text=True)
            calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            desktop = home / ".local/share/applications/dictate.desktop"
            entry = desktop.read_text() if desktop.exists() else ""
            if desktop.exists() and shutil.which("desktop-file-validate"):
                subprocess.run(["desktop-file-validate", str(desktop)], check=True,
                               capture_output=True, text=True)
            return result, calls, entry, root

    def test_arch_and_derivatives(self):
        for release in ('ID=arch\n', 'ID=cachyos\nID_LIKE=arch\n',
                        'ID=derivative\nID_LIKE="other arch"\n'):
            with self.subTest(release=release):
                result, calls, entry, root = self.run_installer(release)
                self.assertEqual(result.returncode, 0, result.stderr)
                packages = next(call for call in calls if call[:2] == ["sudo", "pacman"])
                self.assertEqual(packages[2:5], ["-S", "--needed", "--noconfirm"])
                for package in ("python-evdev", "portaudio", "libsndfile", "xcb-util-cursor", "wl-clipboard", "libxkbcommon-x11", "xkeyboard-config"):
                    self.assertIn(package, packages)
                self.assertFalse(any("apt-get" in call for call in calls))
                self.assertIn(["python3", "-m", "venv", "--system-site-packages", str(root / ".venv")], calls)
                pip = next(call for call in calls if call[1:4] == ["-m", "pip", "install"])
                self.assertEqual(pip[0], "python")
                self.assertNotIn("--break-system-packages", pip)
                self.assertIn(["systemctl", "--user", "enable", "ydotool.service"], calls)
                self.assertIn(["systemctl", "--user", "restart", "ydotool.service"], calls)
                self.assertIn(["sudo", "modprobe", "uinput"], calls)
                self.assertIn(f'Exec="{root}/.venv/bin/python" "{root}/gui.py"', entry)

    def test_debian_and_ubuntu_keep_existing_setup(self):
        for release in ('ID=debian\n', 'ID=ubuntu\nID_LIKE=debian\n',
                        'ID=derivative\nID_LIKE="ubuntu debian"\n'):
            with self.subTest(release=release):
                result, calls, entry, root = self.run_installer(release)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(["sudo", "apt-get", "update", "-qq"], calls)
                packages = next(call for call in calls if call[:3] == ["sudo", "apt-get", "install"])
                self.assertIn("wl-clipboard", packages)
                self.assertIn("libxkbcommon-x11-0", packages)
                self.assertIn("xkb-data", packages)
                pip = next(call for call in calls if call[1:4] == ["-m", "pip", "install"])
                self.assertEqual(pip[:6], ["python3", "-m", "pip", "install", "--user", "--break-system-packages"])
                self.assertFalse(any("pacman" in call or "systemctl" in call for call in calls))
                self.assertIn(f'Exec="python3" "{root}/gui.py"', entry)

    def test_input_group_setup(self):
        result, calls, _, _ = self.run_installer('ID=arch\n', uinput_writable=False, TEST_GROUPS="users")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["sudo", "usermod", "-a", "-G", "input", "tester"], calls)
        self.assertIn("log out AND log in again", result.stdout)
        self.assertNotIn(["systemctl", "--user", "restart", "ydotool.service"], calls)
        self.assertIn("ydotool enabled; it will start after logging out", result.stdout)

    def test_daemon_start_failure_prints_recovery_command(self):
        result, _, entry, _ = self.run_installer('ID=arch\n', TEST_START_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Could not start ydotool. After login, run: systemctl --user restart", result.stdout)
        self.assertIn(".venv/bin/python", entry)

    def test_no_user_session_prints_recovery_command(self):
        result, _, entry, _ = self.run_installer('ID=cachyos\nID_LIKE=arch\n', TEST_NO_SESSION="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("systemctl --user enable --now ydotool.service", result.stdout)
        self.assertIn(".venv/bin/python", entry)

    def test_unsupported_distribution_exits_before_changes(self):
        result, calls, entry, _ = self.run_installer('ID=fedora\nPRETTY_NAME=Fedora\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported distribution: Fedora", result.stderr)
        self.assertEqual(calls, [])
        self.assertEqual(entry, "")


if __name__ == "__main__":
    unittest.main()
