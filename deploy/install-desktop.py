#!/usr/bin/env python3
"""Install a prebuilt native desktop and replace the user's kiosk autostart."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import time


def install(binary,port=80):
    if not binary.is_file() or not os.access(binary,os.X_OK):
        raise SystemExit('Build native/ first and supply its executable')
    home=Path.home();base=home/'.local/share/shore-tracker'
    desktop_dir=Path(subprocess.check_output(['xdg-user-dir','DESKTOP'],text=True).strip())
    desktop_launcher=desktop_dir/'shore-tracker.desktop'
    backup=base/'desktop-backups'/time.strftime('%Y%m%d-%H%M%S')
    backup.mkdir(parents=True,exist_ok=False)
    files=[base/'start-desktop',base/'bin/shore-tracker-desktop',home/'.config/autostart/shore-tracker-kiosk.desktop',home/'.local/share/applications/shore-tracker.desktop',desktop_launcher]
    for i,path in enumerate(files):
        if path.exists():shutil.copy2(path,backup/(str(i)+'-'+path.name))
    (base/'bin').mkdir(parents=True,exist_ok=True)
    temporary=base/'bin/shore-tracker-desktop.new'
    shutil.copy2(binary,temporary)
    temporary.chmod(0o700)
    temporary.replace(base/'bin/shore-tracker-desktop')
    script=base/'start-desktop'
    script.write_text('''#!/bin/sh
set -eu
base="$HOME/.local/share/shore-tracker"
"$base/venv/bin/python" - <<'WAIT'
import time, urllib.request
for _ in range(90):
    try:
        with urllib.request.urlopen('http://127.0.0.1:PORT/healthz', timeout=1): break
    except OSError: time.sleep(1)
WAIT
exec "$base/bin/shore-tracker-desktop" --url http://127.0.0.1:PORT
'''.replace('PORT',str(port)))
    script.chmod(0o700)
    desktop=('[Desktop Entry]\nType=Application\nName=Shore Jetson\n'
        f'Exec="{script}"\nIcon=shore-tracker\nTerminal=false\nStartupWMClass=ShoreTracker\n'
        'Categories=AudioVideo;Video;\nX-GNOME-Autostart-enabled=true\n')
    for path in files[2:]:
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(desktop);path.chmod(0o700)
    env=os.environ.copy()
    env.setdefault('XDG_RUNTIME_DIR',f'/run/user/{os.getuid()}')
    env.setdefault('DBUS_SESSION_BUS_ADDRESS',f'unix:path={env["XDG_RUNTIME_DIR"]}/bus')
    subprocess.run(['gio','set',str(desktop_launcher),'metadata::trusted','true'],env=env,check=True)
    subprocess.run(['gsettings','set','org.gnome.desktop.background','show-desktop-icons','true'],env=env,check=True)
    print(f'Desktop installed; rollback files: {backup}')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary',type=Path);parser.add_argument('--port',type=int,default=80)
    args=parser.parse_args()
    if not 1<=args.port<=65535:parser.error('invalid port')
    install(args.binary,args.port)
