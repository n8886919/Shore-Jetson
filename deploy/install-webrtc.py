#!/usr/bin/env python3
"""Install a reviewed go2rtc binary as the user's loopback-only signaling relay."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess


def install(binary,port=80):
    base=Path.home()/'.local/share/shore-tracker';(base/'bin').mkdir(parents=True,exist_ok=True)
    shutil.copy2(binary,base/'bin/go2rtc');(base/'bin/go2rtc').chmod(0o700)
    config=Path.home()/'.config/shore-tracker/go2rtc.yaml';config.parent.mkdir(parents=True,exist_ok=True)
    config.write_text(f'''streams:
  preview: http://127.0.0.1:{port}/api/preview.ts
api:
  listen: 127.0.0.1:1984
rtsp:
  listen: ""
webrtc:
  listen: ":18555"
''');config.chmod(0o600)
    unit=Path.home()/'.config/systemd/user/shore-tracker-webrtc.service';unit.parent.mkdir(parents=True,exist_ok=True)
    unit.write_text('''[Unit]
Description=Shore Jetson WebRTC relay
After=shore-tracker.service
[Service]
ExecStart=%h/.local/share/shore-tracker/bin/go2rtc -config %h/.config/shore-tracker/go2rtc.yaml
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
UMask=0077
[Install]
WantedBy=default.target
''')
    env=os.environ.copy();env['XDG_RUNTIME_DIR']=f'/run/user/{os.getuid()}';env['DBUS_SESSION_BUS_ADDRESS']=f'unix:path={env["XDG_RUNTIME_DIR"]}/bus'
    subprocess.run(['systemctl','--user','daemon-reload'],env=env,check=True)
    subprocess.run(['systemctl','--user','enable','--now',unit.name],env=env,check=True)
    print('WebRTC relay installed: signaling on loopback 1984, media on 18555')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('binary',type=Path);p.add_argument('--port',type=int,default=80);args=p.parse_args()
    if not args.binary.is_file() or not 1<=args.port<=65535:p.error('supply an existing executable and valid port')
    install(args.binary,args.port)
