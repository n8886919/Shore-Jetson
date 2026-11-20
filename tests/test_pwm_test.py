import pytest

from shore_tracker.servo.pwm_test import PwmServoTest
from shore_tracker.servo.simple import SimpleServoClient
from shore_tracker.servo.simple_service import ServoService
from shore_tracker.status import StatusStore


def fixture_test(monkeypatch, pan=-1, tilt=-1):
    now, clients = [0.], []

    class Writer:
        def __init__(self): self.writes = []; self.connected = False
        def open(self): self.connected = True
        def write(self, payload): self.writes.append(payload)
        def close(self): self.connected = False
        def is_connected(self): return self.connected

    def factory():
        client = SimpleServoClient(Writer(), angle_min_deg=70, angle_max_deg=110,
            tilt_min_deg=70, tilt_max_deg=110, protocol='m0_pulse', pan_channel=0, tilt_channel=1)
        monkeypatch.setattr(client, '_run', lambda: None)
        clients.append(client)
        return ServoService(client, StatusStore())

    test = PwmServoTest(service_factory=factory, pan_direction=pan, tilt_direction=tilt,
                        clock=lambda: now[0])
    return test, now, clients


@pytest.mark.parametrize('direction', [-1, 1])
def test_relative_sequence_channels_endpoints_and_bounded_command_trajectory(monkeypatch, direction):
    test, now, clients = fixture_test(monkeypatch, direction, direction)
    assert not test.snapshot()['connected'] and clients == []
    session = test.start(epoch=0)
    phases, endpoints, samples = [], {}, []
    try:
        for sequence in range(1, 2401):
            now[0] += .025
            test.renew(session=session, sequence=sequence)
            test.tick()
            clients[-1]._send_target()
            data = test.snapshot()
            if not phases or phases[-1] != data['phase']: phases.append(data['phase'])
            samples.append((data['pan_requested_deg'], data['tilt_requested_deg']))
            endpoints[data['phase']] = samples[-1]
            if not data['running']: break
        assert phases == ['center','left','right','up','down','complete']
        assert endpoints['left'] == pytest.approx((90 - 20*direction, 90))
        assert endpoints['right'] == pytest.approx((90 + 20*direction, 90))
        assert endpoints['up'] == pytest.approx((90 + 20*direction, 90 - 20*direction))
        assert samples[-1] == pytest.approx((90 + 20*direction, 90))
        assert 34 < now[0] < 36, 'half tilt excursion keeps the original low speed'
        assert clients[-1].writer.writes[0] == b'#00P1500!#01P1500!'
        assert not clients[-1].snapshot().armed
        assert all(70 <= pan <= 110 and 70 <= tilt <= 110 for pan, tilt in samples)
        for axis in (0, 1):
            velocities = [(b[axis]-a[axis])/.025 for a,b in zip(samples,samples[1:])]
            assert max(map(abs, velocities)) <= 5.01
            assert max(abs(b-a)/.025 for a,b in zip(velocities,velocities[1:])) <= 10.1
        assert len(clients) == 1
        assert '指令完成' in data['notice']
    finally:
        assert test.close()


def test_lease_expiry_revokes_delayed_heartbeat_and_old_start(monkeypatch):
    test, now, clients = fixture_test(monkeypatch)
    session = test.start(epoch=0)
    try:
        test.tick(); clients[-1]._send_target()
        count = len(clients[-1].writer.writes)
        now[0] = 2
        with pytest.raises(ValueError): test.renew(session=session, sequence=1)
        test.tick(); clients[-1]._send_target()
        assert len(clients[-1].writer.writes) == count
        assert not test.snapshot()['running']
        assert test.close()
        with pytest.raises(ValueError): test.start(epoch=0)
        assert len(clients) == 1
    finally:
        test.close()


def test_cancel_before_start_fences_delayed_request_and_never_opens_port(monkeypatch):
    test, now, clients = fixture_test(monkeypatch)
    test.stop()
    with pytest.raises(ValueError): test.start(epoch=0)
    assert clients == []


def test_maximum_duration_still_stops_with_fresh_heartbeats(monkeypatch):
    test, now, clients = fixture_test(monkeypatch)
    session = test.start(epoch=0)
    try:
        for sequence in range(1, 60):
            now[0] = sequence
            test.renew(session=session, sequence=sequence)
        now[0] = 60
        with pytest.raises(ValueError): test.renew(session=session, sequence=60)
        assert test.snapshot()['phase'] == 'stopped'
        assert not clients[-1].snapshot().armed
        assert clients[-1].writer.writes == []
    finally:
        test.close()


def test_unknown_direction_is_rejected_before_port_open(monkeypatch):
    test, now, clients = fixture_test(monkeypatch, pan=0)
    with pytest.raises(ValueError): test.start(epoch=0)
    assert clients == []


def test_unplug_stops_sequence_and_clears_pending_targets(monkeypatch):
    test, now, clients = fixture_test(monkeypatch)
    test.start(epoch=0)
    try:
        clients[-1].writer.connected = False
        assert test.tick()
        assert test.snapshot()['phase'] == 'error'
        assert not clients[-1].snapshot().armed
        assert clients[-1].snapshot().target_degrees is None
        assert clients[-1].writer.writes == []
    finally:
        test.close()
