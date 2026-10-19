#!/usr/bin/env python3
"""Apply this bounded Shore Tracker release; preserve the existing installation."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request

HERE = Path(__file__).resolve().parent
APP = Path('/opt/shore-tracker')
CONFIG = Path('/etc/shore-tracker/config.toml')
SERVICE = 'shore-tracker.service'
SERIAL_DEVICE = '/dev/ttyTHS1'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(root):
    return {str(p.relative_to(root)): digest(p)
            for p in (root / 'shore_tracker').rglob('*')
            if p.is_file() and '__pycache__' not in p.parts
            and p.suffix in ('.py', '.js', '.css', '.html')}


def status():
    with urllib.request.urlopen('http://127.0.0.1:8090/api/status', timeout=4) as response:
        return json.load(response)


def idle(snapshot):
    recording = snapshot.get('manual_recording', {})
    if (snapshot.get('tracking', {}).get('armed') is not False
            or snapshot.get('tracking', {}).get('mode') == 'arming'):
        raise RuntimeError('Tracking must remain DISARMED before deployment')
    if recording.get('state') != 'idle' or recording.get('manual_requested') or recording.get('tracking_requested'):
        raise RuntimeError('An event recording is active; finish it before deployment')


def edit_key(text, section, key, value):
    pattern = re.compile(r'(?ms)^\[' + re.escape(section) + r'\][^\n]*\n(.*?)(?=^\[|\Z)')
    match = pattern.search(text)
    if not match:
        raise RuntimeError('Missing expected config section: ' + section)
    body = re.sub(r'(?m)^\s*' + re.escape(key) + r'\s*=[^\n]*\n?', '', match.group(1))
    if value is not None:
        body = key + ' = ' + json.dumps(value, allow_nan=False) + '\n' + body
    return text[:match.start(1)] + body + text[match.end(1):]


def updated_config(original, tuning):
    text = original.decode()
    changes = [('servo', 'transport', 'serial'),
               ('servo', 'protocol', 'm0_pulse'),
               ('servo', 'serial_device', SERIAL_DEVICE),
               ('servo', 'serial_baud', 9600), ('servo', 'send_hz', 20),
               ('servo', 'wifi_interface', None), ('servo', 'control_port', None),
               ('servo', 'pan_gain_deg', tuning['pan_gain_deg']),
               ('servo', 'pan_deadband', tuning['pan_deadband']),
               ('servo', 'pan_direction', tuning['pan_direction']),
               ('zoom', 'maximum', tuning['zoom_maximum'])]
    expected = copy.deepcopy(tomllib.loads(text))
    for section, key, value in changes:
        text = edit_key(text, section, key, value)
        if value is None:
            expected[section].pop(key, None)
        else:
            expected[section][key] = value
    if tomllib.loads(text) != expected:
        raise RuntimeError('Unexpected configuration change')
    return text.encode()


def write_atomic(path, data, mode=0o644):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def service(action):
    subprocess.run(['systemctl', action, SERVICE], check=True, timeout=100)


def health(tuning):
    manifest = json.loads((HERE / 'manifest.json').read_text())
    required = manifest.get('required_ready_components', ['camera', 'inference', 'servo', 'recording'])
    deadline = time.monotonic() + 65
    good = 0
    pid = None
    last = None
    while time.monotonic() < deadline:
        try:
            snapshot = status()
            idle(snapshot)
            components = snapshot.get('components', {})
            if not all(k in components for k in ('camera', 'inference', 'servo', 'recording')):
                raise RuntimeError('Missing component status')
            if not all(components.get(k, {}).get('state') == 'ready' for k in required):
                raise RuntimeError('Components are not ready')
            metrics = snapshot.get('metrics', {})
            age_keys = [k + '_frame_age_ms' for k in ('camera', 'inference') if components[k]['state'] == 'ready']
            if not all(isinstance(metrics.get(k), (int, float)) and metrics[k] < 1500
                       for k in age_keys):
                raise RuntimeError('Frames are stale')
            if components['camera']['state'] == 'ready' and (not isinstance(metrics.get('preview_fps'), (int, float)) or metrics['preview_fps'] <= 0):
                raise RuntimeError('Preview is not advancing')
            if snapshot.get('tuning') != tuning:
                raise RuntimeError('Tuning readback mismatch')
            info = subprocess.check_output(['systemctl', 'show', SERVICE, '-p', 'MainPID', '-p', 'NRestarts'], text=True)
            props = dict(line.split('=', 1) for line in info.splitlines())
            if props['NRestarts'] != '0' or props['MainPID'] == '0':
                raise RuntimeError('Service restarted unexpectedly')
            if pid is not None and pid != props['MainPID']:
                raise RuntimeError('Service PID changed')
            pid = props['MainPID']
            good += 1
            if good >= 3:
                return {'components': {k: v['state'] for k, v in components.items()},
                        'armed': False, 'pid': pid,
                        'preview_fps': metrics.get('preview_fps'), 'inference_fps': metrics.get('inference_fps'),
                        'hardware_ready': all(v.get('state') == 'ready' for v in components.values()),
                        'verification_scope': 'application/config/HTTP; hardware states reported separately'}
        except Exception as exc:
            last = type(exc).__name__
            good = 0
        time.sleep(5)
    raise RuntimeError('Health verification failed: ' + str(last))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    manifest = json.loads((HERE / 'manifest.json').read_text())
    release = manifest['release']
    if not re.fullmatch(r'shore-update-[a-zA-Z0-9_-]+', release):
        raise RuntimeError('Invalid release ID')
    backup = APP / '.deploy-backups' / release
    current = APP / 'shore_tracker'
    incoming = APP / ('.next-' + release)
    if args.rollback:
        if args.check or os.geteuid() != 0:
            raise RuntimeError('Rollback requires sudo without --check')
        idle(status())
        if inventory(APP) != manifest['files']:
            raise RuntimeError('Installed source changed; refusing to overwrite it')
        if not (backup / 'applied-config.toml').is_file() or CONFIG.read_bytes() != (backup / 'applied-config.toml').read_bytes():
            raise RuntimeError('Installed config changed; refusing to overwrite it')
        if not (backup / 'shore_tracker').is_dir() or not (backup / 'config.toml').is_file():
            raise RuntimeError('Rollback backup is incomplete')
        service('stop')
        os.replace(current, backup / 'reverted-release')
        os.replace(backup / 'shore_tracker', current)
        write_atomic(CONFIG, (backup / 'config.toml').read_bytes())
        service('start')
        print('Rollback restored the previous source and config; verify live service status.')
        return
    if inventory(HERE / 'payload') != manifest['files']:
        raise RuntimeError('Release checksum mismatch')
    if inventory(APP) != manifest['baseline'] or digest(CONFIG) != manifest['config_sha256']:
        raise RuntimeError('Installed source or config changed since preparation; rebuild the release')
    snapshot = status()
    idle(snapshot)
    tuning = snapshot['tuning']
    original = CONFIG.read_bytes()
    proposed = updated_config(original, tuning)
    # Config validation imports no runtime and does not access hardware/secrets.
    sys.path.insert(0, str(HERE / 'payload'))
    from shore_tracker.config import load_config
    with tempfile.TemporaryDirectory(prefix='shore-config-check-') as temp:
        proposed_path = Path(temp) / 'config.toml'
        proposed_path.write_bytes(proposed)
        cfg = load_config(proposed_path)
        if (cfg.servo.transport != 'serial' or cfg.servo.protocol != 'm0_pulse'
                or cfg.servo.serial_baud != 9600 or cfg.servo.angle_max_deg > 180):
            raise RuntimeError('UART configuration validation mismatch')
        from shore_tracker.servo.port_guard import require_control_port
        require_control_port(cfg.servo.serial_device)
        if cfg.servo.enabled and not Path(cfg.servo.serial_device).exists():
            raise RuntimeError('Selected UART device does not exist')
    for p in (HERE / 'payload/shore_tracker').rglob('*.py'):
        compile(p.read_bytes(), str(p), 'exec')
    print('Preflight passed: DISARMED, event recorder idle, source/config checksums match.', flush=True)
    print('Required ready components: ' + ','.join(manifest.get('required_ready_components', ['camera', 'inference', 'servo', 'recording'])), flush=True)
    print('Update: GXServo 180/M0 pulse UART on ttyTHS1 at 9600/20 Hz; tuning and camera settings preserved. No motion test is run.', flush=True)
    if args.check:
        return
    if os.geteuid() != 0:
        raise RuntimeError('Run this prepared update through sudo')
    if backup.exists() or incoming.exists():
        raise RuntimeError('Deployment backup/staging already exists; inspect before retrying')
    backup.mkdir(mode=0o700, parents=True)
    write_atomic(backup / 'config.toml', original, 0o600)
    write_atomic(backup / 'applied-config.toml', proposed, 0o600)
    shutil.copytree(HERE / 'payload/shore_tracker', incoming, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for p in [incoming, *incoming.rglob('*')]:
        os.chmod(p, 0o755 if p.is_dir() else 0o644)
    latest = status()
    idle(latest)
    if latest['tuning'] != tuning:
        raise RuntimeError('Tuning changed immediately before stop; prepare again')
    if digest(CONFIG) != manifest['config_sha256'] or inventory(APP) != manifest['baseline']:
        raise RuntimeError('Installation changed immediately before stop')
    moved_old = False
    installed_new = False
    stopped = False
    try:
        stopped = True
        service('stop')
        os.replace(current, backup / 'shore_tracker')
        moved_old = True
        os.replace(incoming, current)
        installed_new = True
        write_atomic(CONFIG, proposed)
        service('start')
        result = health(tuning)
        if inventory(APP) != manifest['files'] or CONFIG.read_bytes() != proposed:
            raise RuntimeError('Installed release readback mismatch')
        for name in ('app.js', 'app.css', 'index.html'):
            path = '/' if name == 'index.html' else '/' + name
            with urllib.request.urlopen('http://127.0.0.1:8090' + path, timeout=5) as response:
                actual = hashlib.sha256(response.read()).hexdigest()
            if actual != manifest['files']['shore_tracker/web/static/' + name]:
                raise RuntimeError('HTTP asset checksum mismatch')
        result.update(release=release, backup=str(backup), state='deployed')
        write_atomic(HERE / 'result.json', (json.dumps(result, indent=2) + '\n').encode())
        print(json.dumps(result, indent=2), flush=True)
    except BaseException:
        print('Deployment failed; restoring the previous source/config.', flush=True)
        if stopped:
            service('stop')
            if installed_new:
                os.replace(current, backup / 'failed-release')
            if moved_old:
                os.replace(backup / 'shore_tracker', current)
            write_atomic(CONFIG, original)
            service('start')
        raise


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Update failed:', type(exc).__name__, str(exc), file=sys.stderr)
        sys.exit(1)
