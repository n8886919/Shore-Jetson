#!/usr/bin/env python3
"""Install the user's Shore Jetson desktop launcher and pin it in GNOME dock."""
import ast
from pathlib import Path
import os
import subprocess


def install():
    home = Path.home()
    base = home / '.local/share/shore-tracker'
    icons = home / '.local/share/icons/hicolor/scalable/apps'
    applications = home / '.local/share/applications'
    icons.mkdir(parents=True, exist_ok=True)
    applications.mkdir(parents=True, exist_ok=True)
    (icons / 'shore-tracker.svg').write_bytes(Path(__file__).with_name('shore-tracker.svg').read_bytes())
    launcher = applications / 'shore-tracker.desktop'
    launcher.write_text('[Desktop Entry]\nType=Application\nName=Shore Jetson\n'
                        'Comment=Shore Jetson 即時監看與跟拍\n'
                        f'Exec="{base / "start-kiosk"}"\n'
                        'Icon=shore-tracker\nTerminal=false\nStartupWMClass=ShoreTracker\n'
                        'Categories=AudioVideo;Video;\n')
    launcher.chmod(0o755)
    env = os.environ.copy()
    env.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
    env.setdefault('DBUS_SESSION_BUS_ADDRESS', f'unix:path={env["XDG_RUNTIME_DIR"]}/bus')
    favorites = ast.literal_eval(subprocess.check_output(
        ['gsettings', 'get', 'org.gnome.shell', 'favorite-apps'], env=env, text=True))
    if launcher.name not in favorites:
        favorites.append(launcher.name)
        subprocess.run(['gsettings', 'set', 'org.gnome.shell', 'favorite-apps', repr(favorites)], env=env, check=True)
    print('SHORE_TRACKER_LAUNCHER_PINNED')


if __name__ == '__main__':
    install()
