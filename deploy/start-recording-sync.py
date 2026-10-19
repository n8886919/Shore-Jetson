#!/usr/bin/env python3
"""Start the local receiver once, reusing the user's saved SSH host keys."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ssh', required=True)
    args = parser.parse_args()
    if args.ssh.startswith('-'): parser.error('Invalid SSH target')
    script = Path(__file__).resolve().with_name('recording-sync.py')
    project = script.parent.parent.parent / 'surf-track'
    state = Path.home() / '.local/state/shore-tracker'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    pidfile = state / 'receiver.pid'
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text())
            argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
            if str(script).encode() in argv and args.ssh.encode() in argv:
                print('錄影接收器正在執行。')
                return 0
        except (OSError, ValueError): pass
    log = state / 'receiver.log'
    with log.open('ab') as output:
        process = subprocess.Popen([sys.executable, str(script), '--ssh', args.ssh,
            '--known-hosts', str(Path.home()/'.ssh/known_hosts'), '--surf-track', str(project)],
            stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
    time.sleep(1.5)
    if process.poll() is not None:
        print(f'接收器未啟動或已有另一份執行中，請查看 {log}。', file=sys.stderr)
        return 1
    pidfile.write_text(str(process.pid))
    print('錄影接收器已啟動；完成的手動／自動跟拍影片會送到私人 Drive。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
