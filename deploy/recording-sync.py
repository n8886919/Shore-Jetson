#!/usr/bin/env python3
"""Keep a private SSH tunnel and SurfTrack recording receiver alive."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ssh', required=True)
    parser.add_argument('--known-hosts', required=True)
    parser.add_argument('--surf-track', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18090)
    args = parser.parse_args()
    if args.ssh.startswith('-'): parser.error('invalid SSH target')
    project = args.surf_track.resolve()
    sys.path.insert(0, str(project))
    from surf_track.config import Settings
    from surf_track.recordings import RecordingReceiver
    receiver = RecordingReceiver(Settings(data_dir=project / 'var'), f'http://127.0.0.1:{args.port}')
    stopping = False
    def stop(*_args):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    tunnel = None
    with (receiver.root / '.receiver.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('錄影接收程式已在執行。', flush=True)
            return 1
        try:
            while not stopping:
                if tunnel is None or tunnel.poll() is not None:
                    # Never attach to an unrelated listener occupying the port.
                    with socket.socket() as probe:
                        try: probe.bind(('127.0.0.1', args.port))
                        except OSError:
                            print('錄影 tunnel port 已被使用，停止接收。', flush=True)
                            return 1
                    tunnel = subprocess.Popen(['ssh', '-N', '-T', '-L', f'127.0.0.1:{args.port}:127.0.0.1:80',
                        '-o', 'ExitOnForwardFailure=yes', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                        '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
                        '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + args.known_hosts,
                        args.ssh], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    time.sleep(1)
                try:
                    failures = receiver.sync()
                    if failures: print(f'{failures} 部影片上傳未完成，將重試。', flush=True)
                except Exception:
                    print('等待 Jetson 錄影服務；已保存影片不受影響。', flush=True)
                for _ in range(30):
                    if stopping: break
                    time.sleep(.5)
        finally:
            if tunnel and tunnel.poll() is None:
                tunnel.terminate()
                try: tunnel.wait(timeout=5)
                except subprocess.TimeoutExpired: tunnel.kill()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
