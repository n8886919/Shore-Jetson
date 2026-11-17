"""Main settings API and exclusive actuator handoff; no hardware is opened."""
import http.client
import json
import threading
import time
from dataclasses import replace

import pytest

from shore_tracker.coordinator import TrackingCoordinator
from shore_tracker.runtime import ShoreTrackerRuntime
from shore_tracker.servo.pelco_d import PelcoManualController, encode_motion
from shore_tracker.web.server import make_server
from test_runtime import Camera, Inference, Servo, config


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('SHORE_TRACKER_RTSP_URI', 'rtsp://test:secret@192.0.2.8/live')
    monkeypatch.setattr(TrackingCoordinator, 'start', lambda _: None)
    events = []; owner = [None]; writes = []
    class Auto(Servo):
        close_ok = True
        def start(self):
            assert owner[0] is None
            owner[0] = 'automatic'; events.append('automatic_open'); super().start()
        def close(self, **kwargs):
            if not self.close_ok: return False
            owner[0] = None; events.append('automatic_close'); return True
    class Port:
        def __init__(self, **kwargs):
            assert owner[0] is None
            assert kwargs['exclusive'] is True
            owner[0] = 'manual'; events.append('manual_open')
        def write(self, payload):
            assert owner[0] == 'manual'
            writes.append(payload); return len(payload)
        def close(self): owner[0] = None; events.append('manual_close')
    base = config(tmp_path)
    cfg = replace(base, servo=replace(base.servo, protocol='pelco_d'))
    runtime = ShoreTrackerRuntime(cfg,
        dashboard_builder=lambda address, *a, **k: make_server(('127.0.0.1', 0), *a, **k),
        camera_builder=Camera, inference_worker_builder=Inference,
        servo_service_builder=Auto, camera_route_validator=lambda *a: None,
        camera_retry_seconds=60,
        pan_tilt_builder=lambda **kwargs: PelcoManualController(**kwargs, serial_factory=Port))
    runtime.start_services()
    runtime.events, runtime.writes, runtime.owner = events, writes, owner
    thread = threading.Thread(target=runtime.server.serve_forever, daemon=True); thread.start()
    yield runtime
    runtime.stop_services(); runtime.server.shutdown(); runtime.server.server_close(); thread.join(2)


def request(runtime, method, body=None, path='/api/pan-tilt', **headers):
    connection = http.client.HTTPConnection('127.0.0.1', runtime.server.server_port, timeout=2)
    headers = {'Content-Type': 'application/json', 'X-Pan-Tilt-Token': runtime.server.pan_tilt_token, **headers}
    connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    response = connection.getresponse(); data = response.read(); connection.close()
    if response.getheader('Content-Type', '').startswith('application/json'): data = json.loads(data)
    return response.status, data


def command(runtime, action, **kwargs):
    return request(runtime, 'POST', {'action': action, **kwargs})


def arm_manual(runtime):
    assert command(runtime, 'connect')[0] == 200
    code, result = command(runtime, 'arm', epoch=runtime._pan_tilt.snapshot()['epoch'])
    assert code == 200
    return result['session']


def test_main_page_and_status_are_read_only_and_old_test_assets_are_removed(runtime):
    before = runtime.events[:]
    for path in ('/kiosk?layout=nano&controls=1', '/pan-tilt.js'):
        assert request(runtime, 'GET', path=path)[0] == 200
    code, snapshot = request(runtime, 'GET')
    assert code == 200 and snapshot['available'] and not snapshot['connected']
    assert snapshot['control_token'] == runtime.server.pan_tilt_token
    assert runtime.events == before and runtime.writes == []
    for path in ('/pelco', '/pelco.html', '/pelco.js', '/pelco.css'):
        assert request(runtime, 'GET', path=path)[0] == 404


@pytest.mark.parametrize('headers,code', [({'Host':'evil.example'},421),
    ({'Origin':'https://evil.example'},403), ({'X-Pan-Tilt-Token':'bad'},403),
    ({'Content-Type':'text/plain'},403)])
def test_manual_api_rejects_foreign_origin_host_and_token(runtime, headers, code):
    assert request(runtime, 'POST', {'action':'connect'}, **headers)[0] == code
    assert runtime.owner[0] == 'automatic' and runtime.writes == []


@pytest.mark.parametrize('body', [[], {'action':[]}, {'action':'move'}, {'action':'connect','extra':True},
    {'action':'preset'}, {'action':'arm'}, None])
def test_manual_api_rejects_unknown_actions_and_bad_shapes(runtime, body):
    assert request(runtime, 'POST', body)[0] == 400
    assert runtime.owner[0] == 'automatic'


def test_exclusive_handoff_manual_motion_release_and_restore_disarmed(runtime):
    session = arm_manual(runtime)
    assert runtime.events == ['automatic_open','automatic_close','manual_open']
    assert runtime.writes == []  # Connect and arm never move.
    assert not runtime.controller.arm()[0]
    assert command(runtime, 'move', session=session, sequence=1, direction='right', speed=8)[0] == 200
    assert runtime.writes[-1] == encode_motion(1,'right',8)
    assert command(runtime, 'stop', session=session, sequence=3)[0] == 200
    assert runtime.writes[-1] == encode_motion(1)
    assert command(runtime, 'move', session=session, sequence=2, direction='right', speed=8)[0] == 409
    assert command(runtime, 'disconnect')[0] == 200
    assert runtime.owner[0] == 'automatic' and runtime.events[-2:] == ['manual_close','automatic_open']
    assert not runtime.status.snapshot()['tracking']['armed']
    assert runtime.controller.arm()[0]


def test_manual_connection_is_blocked_during_automatic_tracking(runtime):
    assert runtime.controller.arm()[0]
    assert command(runtime, 'connect')[0] == 409
    assert runtime.events == ['automatic_open']
    assert command(runtime, 'disarm', session=None, sequence=1)[0] == 200
    assert not runtime.status.snapshot()['tracking']['armed']


def test_failed_automatic_close_never_opens_another_port(runtime):
    runtime._servo.close_ok = False
    assert command(runtime, 'connect')[0] == 503
    assert runtime.owner[0] == 'automatic'
    assert 'manual_open' not in runtime.events
    runtime._servo.close_ok = True


def test_manual_calibration_is_available_with_automatic_output_disabled(runtime):
    runtime._servo.close(); runtime._servo = None
    runtime.config = replace(runtime.config, servo=replace(runtime.config.servo,
                             enabled=False, pan_direction=0))
    session = arm_manual(runtime)
    assert command(runtime, 'move', session=session, sequence=1, direction='left', speed=8)[0] == 200
    assert command(runtime, 'disconnect')[0] == 200
    assert runtime.owner[0] is None and runtime._servo is None
    assert runtime.status.snapshot()['components']['servo']['state'] == 'offline'


def test_failed_manual_open_restores_the_automatic_service_disarmed(runtime):
    def unavailable(**kwargs): raise OSError('adapter busy')
    runtime._pan_tilt.serial_factory = unavailable
    assert command(runtime, 'connect')[0] == 503
    assert runtime.events == ['automatic_open','automatic_close','automatic_open']
    assert runtime.owner[0] == 'automatic' and not runtime._pan_tilt.snapshot()['connected']
    assert not runtime.status.snapshot()['tracking']['armed']
    assert runtime.controller.arm()[0]


def test_main_runtime_watchdog_stops_lost_browser_and_fences_old_arm(runtime):
    session = arm_manual(runtime)
    assert command(runtime, 'move', session=session, sequence=1, direction='left', speed=8)[0] == 200
    deadline = time.monotonic() + 1
    while runtime._pan_tilt.snapshot()['armed'] and time.monotonic() < deadline: time.sleep(.02)
    assert not runtime._pan_tilt.snapshot()['armed']
    assert runtime.writes[-1] == encode_motion(1)
    assert command(runtime, 'arm', epoch=0)[0] == 409
    assert command(runtime, 'move', session=session, sequence=2, direction='left', speed=8)[0] == 409


def test_global_stop_and_shutdown_revoke_manual_control(runtime):
    session = arm_manual(runtime)
    command(runtime, 'move', session=session, sequence=1, direction='up', speed=8)
    assert runtime.controller.stop()[0]
    assert runtime.writes[-1] == encode_motion(1) and not runtime._pan_tilt.snapshot()['armed']
    assert command(runtime, 'move', session=session, sequence=2, direction='up', speed=8)[0] == 409
    session = command(runtime, 'arm', epoch=runtime._pan_tilt.snapshot()['epoch'])[1]['session']
    command(runtime, 'move', session=session, sequence=1, direction='down', speed=8)
    runtime.stop_services()
    assert runtime.writes[-1] == encode_motion(1) and runtime.owner[0] is None
    assert not runtime._pan_tilt.snapshot()['connected']
    assert command(runtime, 'connect')[0] == 409
