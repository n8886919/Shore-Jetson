import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tomllib

import pytest


@pytest.fixture
def update():
    spec = importlib.util.spec_from_file_location('m0_update', Path('deploy/apply-m0-update.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot():
    return dict(tracking=dict(armed=False, mode='disarmed'),
                manual_recording=dict(state='idle', manual_requested=False, tracking_requested=False),
                tuning=dict(pan_gain_deg=45.0, pan_deadband=.04, pan_direction=-1, zoom_maximum=1.0))


@pytest.mark.parametrize('part,key,value', [
    ('tracking','armed',True), ('tracking','mode','arming'),
    ('manual_recording','state','recording'), ('manual_recording','tracking_requested',True),
])
def test_active_operation_blocks_update(update, part, key, value):
    state = snapshot(); state[part][key] = value
    with pytest.raises(RuntimeError): update.idle(state)


def test_only_explicit_uart_and_live_tuning_fields_change(update):
    raw = b'''[camera]
rotation_degrees=180
preview_fps=20
[servo]
transport="serial"
serial_baud=115200
serial_device="/dev/serial/by-id/old"
angle_min_deg=0
angle_max_deg=180
home_deg=90
pan_direction=-1
[zoom]
maximum=2.5
'''
    before = tomllib.loads(raw.decode())
    after = tomllib.loads(update.updated_config(raw, snapshot()['tuning']).decode())
    assert after['camera'] == before['camera']
    assert after['servo']['protocol'] == 'm0_pulse'
    assert after['servo']['serial_baud'] == 9600
    assert after['servo']['serial_device'] == '/dev/ttyTHS1'
    assert (after['servo']['angle_max_deg'], after['servo']['home_deg']) == (180,90)
    assert after['zoom']['maximum'] == 1.0


@pytest.fixture
def transaction(update, tmp_path, monkeypatch):
    app = tmp_path/'app'; package = tmp_path/'release'
    for root,version in [(app,1),(package/'payload',2)]:
        (root/'shore_tracker/web/static').mkdir(parents=True)
        (root/'shore_tracker/main.py').write_text(f'version={version}\n')
        for name in ['app.js','app.css','index.html']:
            (root/'shore_tracker/web/static'/name).write_text(f'asset {version} {name}')
    config = tmp_path/'config.toml'
    original=b'[servo]\nserial_device="/dev/null"\npan_direction=-1\n[zoom]\nmaximum=1.0\n'
    config.write_bytes(original)
    monkeypatch.setattr(update,'APP',app)
    monkeypatch.setattr(update,'HERE',package)
    monkeypatch.setattr(update,'CONFIG',config)
    monkeypatch.setattr(update,'SERIAL_DEVICE','/dev/null')
    manifest=dict(release='shore-update-test',files=update.inventory(package/'payload'),
                  baseline=update.inventory(app),config_sha256=update.digest(config),
                  required_ready_components=['servo'])
    (package/'manifest.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(update,'status',snapshot)
    calls=[]; monkeypatch.setattr(update,'service',calls.append)
    monkeypatch.setattr(update.os,'geteuid',lambda:0)
    monkeypatch.setattr(sys,'argv',['apply.py'])
    monkeypatch.setattr(sys,'path',sys.path[:])
    monkeypatch.setattr(update,'health',lambda tuning:dict(armed=False))
    def urlopen(url, timeout):
        name=url.rsplit('/',1)[-1] or 'index.html'
        return io.BytesIO((app/'shore_tracker/web/static'/name).read_bytes())
    monkeypatch.setattr(update.urllib.request,'urlopen',urlopen)
    return update,original,calls,manifest


def test_apply_and_rollback_preserve_original_install(transaction, monkeypatch):
    update,original,calls,manifest=transaction
    update.main()
    assert update.inventory(update.APP)==manifest['files']
    assert tomllib.loads(update.CONFIG.read_text())['servo']['protocol']=='m0_pulse'
    assert calls==['stop','start']
    assert json.loads((update.HERE/'result.json').read_text())['state']=='deployed'
    monkeypatch.setattr(sys,'argv',['apply.py','--rollback'])
    update.main()
    assert update.CONFIG.read_bytes()==original
    assert update.inventory(update.APP)==manifest['baseline']


def test_health_failure_restores_previous_source_and_config(transaction, monkeypatch):
    update,original,calls,manifest=transaction
    def fail(_):raise RuntimeError('simulated health failure')
    monkeypatch.setattr(update,'health',fail)
    with pytest.raises(RuntimeError,match='simulated'):update.main()
    assert calls==['stop','start','stop','start']
    assert update.CONFIG.read_bytes()==original
    assert update.inventory(update.APP)==manifest['baseline']


def test_drift_blocks_before_service_change(transaction):
    update,original,calls,manifest=transaction
    update.CONFIG.write_bytes(original+b'\n# new field tuning\n')
    with pytest.raises(RuntimeError,match='changed since'):update.main()
    assert calls==[]
