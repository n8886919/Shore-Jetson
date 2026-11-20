import http.client
import json
import threading
from dataclasses import replace

import pytest

from shore_tracker.config import ServoConfig, TrackingConfig
from shore_tracker.controller import AppController
from shore_tracker.coordinator import TrackingCoordinator
from shore_tracker.preview import PreviewStore
from shore_tracker.servo.simple import SimpleServoClient
from shore_tracker.status import StatusStore
from shore_tracker.web.server import make_server


def setup(**overrides):
    status = StatusStore()
    for name in status.COMPONENTS:
        status.update_component(name, 'ready')
    now, outputs, pauses = [0.], [], []
    cfg = replace(ServoConfig(), **{'pan_direction': 1, 'tilt_direction': 1, **overrides})
    coordinator = TrackingCoordinator(TrackingConfig(), cfg, status, lambda _: None,
        command_axes=lambda pan, tilt: outputs.append((pan, tilt)),
        pause_output=lambda: pauses.append(True), clock=lambda: now[0])
    controller = AppController(status, inference_enabled_callback=coordinator.set_inference_enabled,
        keyboard_snapshot=coordinator.keyboard_snapshot, keyboard_callback=coordinator.keyboard_command)
    return status, coordinator, controller, now, outputs, pauses


def start(controller, direction):
    return controller.keyboard_command('start',
        {'epoch': controller.keyboard_snapshot()['epoch'], 'direction': direction})['session']


@pytest.mark.parametrize('direction,axis,sign', [('left', 0, -1), ('right', 0, 1),
                                               ('up', 1, -1), ('down', 1, 1)])
def test_hold_keeps_moving_with_bounds_and_release_never_replays(direction, axis, sign):
    status, coordinator, controller, now, outputs, pauses = setup()
    assert outputs == []
    assert not controller.keyboard_snapshot()['enabled']
    assert controller.arm()[0]
    with pytest.raises(ValueError): start(controller, direction)
    assert controller.set_inference_enabled(False)[0]
    assert not coordinator.tilt_enabled, 'manual vertical control is independent of automatic tilt'
    epoch = controller.keyboard_snapshot()['epoch']
    token = start(controller, direction)
    for index in range(1, 101):
        now[0] = index * .05
        if index % 2 == 0:
            controller.keyboard_command('move', {'session': token, 'sequence': index, 'direction': direction})
        coordinator.control_step()
    assert sign * (outputs[40][axis] - 90) > 0
    cfg = coordinator.keyboard.config
    assert all(cfg.angle_min_deg <= p <= cfg.angle_max_deg
               and (t is None or 70 <= t <= 110) for p, t in outputs)
    if axis == 0:
        assert all(t is None for _, t in outputs)
        velocities = [(b[0]-a[0])/.05 for a,b in zip(outputs,outputs[1:])]
        assert max(map(abs, velocities)) <= 5.000001
        assert max(abs(b-a)/.05 for a,b in zip(velocities,velocities[1:])) <= 10.000001
    controller.keyboard_command('stop', {'epoch': epoch})
    count = len(outputs)
    now[0] += .05; coordinator.control_step()
    assert len(outputs) == count and pauses
    assert status.snapshot()['tracking']['armed']
    with pytest.raises(ValueError):
        controller.keyboard_command('move', {'session': token, 'sequence': 102, 'direction': direction})
    with pytest.raises(ValueError):
        controller.keyboard_command('start', {'epoch': epoch, 'direction': direction})


def test_lease_expiry_and_stop_before_start_response_fence_late_requests():
    _, c, controller, now, outputs, pauses = setup()
    controller.arm(); controller.set_inference_enabled(False)
    epoch = controller.keyboard_snapshot()['epoch']
    controller.keyboard_command('stop', {'epoch': epoch})
    with pytest.raises(ValueError):
        controller.keyboard_command('start', {'epoch': epoch, 'direction': 'right'})
    token = start(controller, 'right')
    now[0] = .1; c.control_step()
    count = len(outputs)
    now[0] = .5; c.control_step()
    assert len(outputs) == count
    assert not controller.keyboard_snapshot()['moving']
    with pytest.raises(ValueError):
        controller.keyboard_command('move', {'session': token, 'sequence': 1, 'direction': 'right'})


@pytest.mark.parametrize('interrupt', ['ai', 'stop', 'camera', 'servo'])
def test_interrupts_cancel_manual_output_and_cannot_be_revived(interrupt):
    status, c, controller, now, outputs, pauses = setup()
    controller.arm(); controller.set_inference_enabled(False)
    token = start(controller, 'right')
    now[0] = .1; c.control_step()
    if interrupt == 'ai': controller.set_inference_enabled(True)
    elif interrupt == 'stop': controller.stop()
    else: status.update_component(interrupt, 'error')
    count = len(outputs)
    now[0] += .1; c.control_step()
    assert len(outputs) == count
    assert not controller.keyboard_snapshot()['moving']
    with pytest.raises(ValueError):
        controller.keyboard_command('move', {'session': token, 'sequence': 1, 'direction': 'right'})


def test_second_client_and_replayed_sequences_are_rejected():
    _, _, controller, _, _, _ = setup()
    controller.arm(); controller.set_inference_enabled(False)
    token = start(controller, 'left')
    with pytest.raises(ValueError): start(controller, 'right')
    values = {'session': token, 'sequence': 1, 'direction': 'left'}
    controller.keyboard_command('move', values)
    with pytest.raises(ValueError): controller.keyboard_command('move', values)


def test_braking_at_configured_limit_preserves_speed_and_acceleration():
    _, c, controller, now, outputs, _ = setup(angle_min_deg=88, angle_max_deg=92,
        search_min_deg=88, search_max_deg=92)
    controller.arm(); controller.set_inference_enabled(False)
    token = start(controller, 'right')
    for index in range(1, 301):
        now[0] = index * .02
        if index % 5 == 0:
            controller.keyboard_command('move', {'session': token, 'sequence': index, 'direction': 'right'})
        c.control_step()
    assert outputs[-1][0] == pytest.approx(92)
    speeds = [(b[0]-a[0])/.02 for a,b in zip(outputs, outputs[1:])]
    assert max(map(abs, speeds)) <= 5.000001
    assert max(abs(b-a)/.02 for a,b in zip(speeds,speeds[1:])) <= 10.000001


@pytest.mark.parametrize('direction,bound,pulse', [
    ('up', 70, b'#01P1278!'), ('down', 110, b'#01P1722!'),
])
def test_keyboard_tilt_reaches_shared_test_limits_through_uart(
        monkeypatch, tmp_path, direction, bound, pulse):
    from test_runtime import config, Dashboard
    from shore_tracker.runtime import ShoreTrackerRuntime
    from shore_tracker.servo.simple_service import build_servo_service

    status, coordinator, controller, now, outputs, _ = setup()
    cfg = coordinator.keyboard.config
    writes = []

    class Writer:
        def open(self): pass
        def close(self): pass
        def write(self, payload): writes.append(payload)

    def serial_client(**kwargs):
        kwargs.pop('device'); kwargs.pop('baud')
        client = SimpleServoClient(Writer(), clock=lambda: now[0], **kwargs)
        monkeypatch.setattr(client, '_run', lambda: None)
        return client

    monkeypatch.setattr('shore_tracker.servo.simple_service.serial_client', serial_client)
    service = build_servo_service(cfg, status)
    runtime = ShoreTrackerRuntime(replace(config(tmp_path), servo=cfg),
        dashboard_builder=lambda *a, **k: Dashboard(),
        servo_service_builder=lambda servo_config, _: servo_config)
    test_config = runtime._build_servo_test_service()
    assert (service.client.tilt_min_deg, service.client.tilt_max_deg) == (
        test_config.tilt_min_deg, test_config.tilt_max_deg) == (70, 110)
    assert (cfg.tilt_min_deg, cfg.tilt_max_deg) == (85, 95)
    assert (coordinator.tilt_planner.config.angle_min_deg,
            coordinator.tilt_planner.config.angle_max_deg) == (85, 95)

    def output(pan, tilt):
        outputs.append((pan, tilt))
        service.set_axes(pan, tilt)

    coordinator.keyboard.output = output
    coordinator.keyboard.pause = service.pause_motion
    service.start()
    try:
        service.client.arm()
        controller.arm(); controller.set_inference_enabled(False)
        assert writes == []
        token = start(controller, direction)
        for index in range(1, 601):
            now[0] = index * .02
            if index % 5 == 0:
                controller.keyboard_command('move', {
                    'session': token, 'sequence': index, 'direction': direction})
            coordinator.control_step()
            service.client._send_target()
        assert outputs[-1][1] == pytest.approx(bound)
        assert all(70 <= tilt <= 110 for _, tilt in outputs)
        assert pulse in writes[-1]
        speeds = [(b[1]-a[1])/.02 for a, b in zip(outputs, outputs[1:])]
        assert max(map(abs, speeds)) <= 5.000001
        assert max(abs(b-a)/.02 for a, b in zip(speeds, speeds[1:])) <= 10.000001
        for target in (69.9, 110.1):
            with pytest.raises(ValueError, match='Tilt target'):
                service.set_axes(90, target)
    finally:
        service.close()


@pytest.mark.parametrize('current', [60, 120])
def test_keyboard_does_not_jump_into_manual_tilt_limits(current):
    _, coordinator, controller, now, outputs, _ = setup(
        tilt_min_deg=60, tilt_max_deg=120)
    coordinator.current_tilt_deg = current
    controller.arm(); controller.set_inference_enabled(False)
    start(controller, 'down' if current < 70 else 'up')
    now[0] = .1
    coordinator.control_step()
    assert outputs == []
    assert coordinator.current_tilt_deg == current
    assert not controller.keyboard_snapshot()['moving']


def test_manual_speed_updates_without_arming_or_output_and_applies_while_held():
    status, coordinator, controller, now, outputs, _ = setup()
    controller.keyboard_command('speed', {'speed_deg_s': 2})
    assert controller.keyboard_snapshot()['speed_deg_s'] == 2
    assert not status.snapshot()['tracking']['armed'] and outputs == []
    controller.arm(); controller.set_inference_enabled(False)
    token = start(controller, 'right')
    for index in range(1, 101):
        now[0] = index * .02
        if index % 5 == 0:
            controller.keyboard_command('move', {'session': token, 'sequence': index, 'direction': 'right'})
        coordinator.control_step()
    assert (outputs[-1][0]-outputs[-2][0])/.02 == pytest.approx(2)
    controller.keyboard_command('speed', {'speed_deg_s': .5})
    assert controller.keyboard_snapshot()['moving']
    for index in range(101, 126):
        now[0] = index * .02
        if index % 5 == 0:
            controller.keyboard_command('move', {'session': token, 'sequence': index, 'direction': 'right'})
        coordinator.control_step()
    assert (outputs[-1][0]-outputs[-2][0])/.02 == pytest.approx(.5)
    speeds = [(b[0]-a[0])/.02 for a, b in zip(outputs, outputs[1:])]
    assert max(abs(b-a)/.02 for a, b in zip(speeds, speeds[1:])) <= 10.000001


@pytest.mark.parametrize('value', [0, -.1, 5.1, True, '2', None, float('nan'), float('inf')])
def test_invalid_manual_speed_does_not_change_the_current_value(value):
    _, _, controller, _, outputs, _ = setup()
    with pytest.raises(ValueError):
        controller.keyboard_command('speed', {'speed_deg_s': value})
    assert controller.keyboard_snapshot()['speed_deg_s'] == 5 and outputs == []


def test_manual_speed_cannot_exceed_configured_slew_cap():
    _, _, controller, _, _, _ = setup(max_slew_deg_s=2)
    assert controller.keyboard_snapshot()['max_speed_deg_s'] == 2
    with pytest.raises(ValueError):
        controller.keyboard_command('speed', {'speed_deg_s': 2.1})


def test_runtime_wires_ai_pause_keyboard_and_recording_independence(tmp_path):
    from test_runtime import config, Dashboard
    from shore_tracker.runtime import ShoreTrackerRuntime
    class Servo:
        paused = 0
        def request_arm(self): runtime.status.confirm_armed(); return True, 'armed'
        def pause_motion(self): self.paused += 1
        def stop_motion(self, *_): pass
        def set_axes(self, pan, tilt): outputs.append((pan, tilt))
        def set_target_degrees(self, pan): outputs.append((pan, None))
    outputs = []
    runtime = ShoreTrackerRuntime(replace(config(tmp_path),
        servo=replace(ServoConfig(), pan_direction=1, tilt_direction=1)),
        dashboard_builder=lambda *a, **k: Dashboard())
    runtime._servo = Servo()
    for component in runtime.status.COMPONENTS:
        runtime.status.update_component(component, 'ready')
    runtime._recovery['state'] = 'ready'
    assert runtime.controller.arm()[0]
    assert runtime.controller.set_inference_enabled(False)[0]
    assert runtime._servo.paused == 1
    assert runtime.manual_recording.activity_callback is None
    assert runtime.status.snapshot()['tracking']['armed']
    start(runtime.controller, 'up')
    runtime._coordinator.control_step()
    assert outputs
    epoch = runtime.controller.keyboard_snapshot()['epoch']
    runtime.controller.keyboard_command('stop', {'epoch': epoch})
    assert runtime._servo.paused == 2
    assert runtime.controller.set_inference_enabled(True)[0]
    count = len(outputs)
    runtime._coordinator.control_step()
    assert len(outputs) == count, 'AI resume must wait for a new decision'


def test_uncalibrated_or_swapped_channels_do_not_enable_keyboard():
    for values in ({'tilt_direction': 0}, {'pan_channel': 1, 'tilt_channel': 0}, {'enabled': False}):
        status, _, controller, _, _, _ = setup(**values)
        status.arm(); controller.set_inference_enabled(False)
        assert not controller.keyboard_snapshot()['enabled']


def test_historical_non_pwm_control_keeps_disarm_semantics():
    status, _, controller, _, _, _ = setup(protocol='shore_spotter')
    controller.arm(); controller.set_inference_enabled(False)
    assert not status.snapshot()['tracking']['armed']
    assert not controller.keyboard_snapshot()['available']


def test_unavailable_pelco_panel_cannot_disarm_pwm_keyboard_control():
    status, _, controller, _, _, _ = setup()
    controller._pan_tilt_callback = lambda *a: None
    controller._pan_tilt_snapshot = lambda: {'available': False}
    controller.arm(); controller.set_inference_enabled(False)
    with pytest.raises(ValueError): controller.pan_tilt_command('disarm', {})
    assert status.snapshot()['tracking']['armed']


def test_pause_drops_pending_positions_without_latching_timeout_or_disarming():
    class Writer:
        def open(self): pass
        def write(self, data): writes.append(data)
        def close(self): pass
    writes = []
    now = [0.]
    client = SimpleServoClient(Writer(), angle_min_deg=70, angle_max_deg=110,
        protocol='m0_pulse', clock=lambda: now[0])
    client.start()
    try:
        client.arm(); client.set_target_degrees(90, tilt_degrees=90)
        client.pause_motion(); count = len(writes)
        now[0] = 10
        assert client.snapshot().armed
        assert client.snapshot().target_degrees is None
        client._send_target()
        assert len(writes) == count
        assert client.snapshot().last_error is None
        client.set_target_degrees(91)
        client._send_target()
        assert len(writes) > count
    finally: client.close()


def test_keyboard_api_requires_origin_token_shape_and_armed_ai_off():
    _, _, controller, _, outputs, _ = setup()
    server = make_server(('127.0.0.1', 0), controller.status, controller, PreviewStore())
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
    def post(body, token=None, origin=None):
        headers = {'Content-Type': 'application/json', 'X-Pan-Tilt-Token': token or ''}
        if origin: headers['Origin'] = origin
        conn.request('POST', '/api/keyboard', json.dumps(body), headers)
        response = conn.getresponse(); return response.status, json.loads(response.read())
    try:
        conn.request('GET', '/api/keyboard'); response = conn.getresponse()
        state = json.loads(response.read()); token = state['control_token']
        assert outputs == []
        body = {'action': 'start', 'epoch': state['epoch'], 'direction': 'right'}
        assert post(body)[0] == 403
        assert post(body, token, 'http://evil.example')[0] == 403
        assert post({'action': 'start'}, token)[0] == 400
        assert post(body, token)[0] == 409
        assert post({'action': 'speed', 'speed_deg_s': 2}, token)[0] == 200
        assert controller.keyboard_snapshot()['speed_deg_s'] == 2
        assert outputs == []
        assert post({'action': 'speed', 'speed_deg_s': True}, token)[0] == 409
        assert post({'action': 'speed', 'speed_deg_s': 2, 'extra': 1}, token)[0] == 400
        controller.arm(); controller.set_inference_enabled(False)
        body['epoch'] = controller.keyboard_snapshot()['epoch']
        assert post(body, token)[0] == 200
        assert post({'action': 'stop', 'epoch': body['epoch']}, token)[0] == 200
        conn.request('GET', '/keyboard-jog.js'); response = conn.getresponse()
        assert response.status == 200; response.read()
    finally:
        conn.close(); server.shutdown(); server.server_close(); thread.join(2)
