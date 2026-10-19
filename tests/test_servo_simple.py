import threading
from dataclasses import replace

import pytest

from shore_tracker.config import ServoConfig
from shore_tracker.controller import AppController
from shore_tracker.servo.simple import SimpleServoClient, encode_command, serial_client
from shore_tracker.servo.simple_service import ServoService, build_servo_service
from shore_tracker.status import StatusStore


class Writer:
    def __init__(self):
        self.payloads = []
        self.opened = False
        self.opens = 0
        self.changed = threading.Condition()

    def open(self):
        self.opened = True
        self.opens += 1

    def write(self, payload):
        assert self.opened
        with self.changed:
            self.payloads.append(payload)
            self.changed.notify_all()

    def close(self):
        self.opened = False

    def wait_for_count(self, count, timeout=1):
        with self.changed:
            return self.changed.wait_for(lambda: len(self.payloads) >= count, timeout)


@pytest.fixture
def controlled_client(monkeypatch):
    writer = Writer()
    client = SimpleServoClient(writer, angle_min_deg=0, angle_max_deg=180)
    # Drive send ticks explicitly for deterministic ordering tests.
    monkeypatch.setattr(client, "_run", lambda: None)
    client.start()
    yield client, writer
    client.close()


@pytest.mark.parametrize("degrees, payload", [
    (0, b"SET 0\n"), (90.125, b"SET 90125\n"), (180, b"SET 180000\n"),
])
def test_uart_set_uses_unsigned_millidegrees(degrees, payload):
    assert encode_command("SET", degrees) == payload


@pytest.mark.parametrize("command, degrees", [
    ("ARM", None), ("STOP", None), ("SET", None), ("SET", -0.0001),
    ("SET", 180.0001), ("SET", float("nan")), ("SET", float("inf")),
])
def test_invalid_uart_commands_are_rejected(command, degrees):
    with pytest.raises(ValueError):
        encode_command(command, degrees)


def test_start_and_arm_require_fresh_target(controlled_client):
    client, writer = controlled_client
    client.set_target_degrees(90)
    client._send_target()
    assert client.snapshot().target_degrees is None
    client.arm()
    client._send_target()
    assert writer.payloads == []
    client.set_target_degrees(87)
    client.set_target_degrees(88)
    assert writer.payloads == []  # Producers do not bypass the send cadence.
    client._send_target()
    assert writer.payloads == [b"SET 88000\n"]


def test_stop_discards_target_and_rearm_cannot_replay_it(controlled_client):
    client, writer = controlled_client
    client.arm()
    client.set_target_degrees(87)
    client._send_target()
    client.stop_motion()
    assert client.snapshot().target_degrees is None
    client.set_target_degrees(86)  # An in-flight producer finishes after STOP.
    client._send_target()
    client.arm()
    client._send_target()
    assert writer.payloads == [b"SET 87000\n"]
    client.set_target_degrees(90)
    client._send_target()
    assert writer.payloads[-1] == b"SET 90000\n"
    client.close()
    client._send_target()
    assert len(writer.payloads) == 2
    client.start()
    client.arm()
    client._send_target()
    assert len(writer.payloads) == 2


def test_worker_repeats_set_until_stopped_without_arm_or_stop_bytes():
    writer = Writer()
    client = SimpleServoClient(writer, angle_min_deg=0, angle_max_deg=180)
    client.start()
    client.start()
    try:
        assert writer.opens == 1
        assert not writer.wait_for_count(1, timeout=0.12)
        client.arm()
        client.set_target_degrees(90)
        assert writer.wait_for_count(2)
        client.stop_motion()
        count = len(writer.payloads)
        assert not writer.wait_for_count(count + 1, timeout=0.12)
        assert all(payload == b"SET 90000\n" for payload in writer.payloads)
    finally:
        assert client.close()


def test_stop_racing_write_waits_for_it_then_prevents_further_sends(controlled_client):
    client, writer = controlled_client
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    original_write = writer.write

    def blocked_write(payload):
        entered.set()
        assert release.wait(1)
        original_write(payload)

    writer.write = blocked_write
    client.arm()
    client.set_target_degrees(90)
    sender = threading.Thread(target=client._send_target)
    stopper = threading.Thread(target=lambda: (client.stop_motion(), stopped.set()))
    sender.start()
    try:
        assert entered.wait(1)
        stopper.start()
        assert not stopped.wait(0.02)
    finally:
        release.set()
        sender.join(1)
        if stopper.ident is not None:
            stopper.join(1)
    assert stopped.is_set()
    client._send_target()
    assert writer.payloads == [b"SET 90000\n"]
    assert client.snapshot().target_degrees is None


def test_write_failure_disarms_status_and_requires_service_restart(controlled_client):
    client, writer = controlled_client
    status = StatusStore()
    service = ServoService(client, status)
    service.start()
    for component in ("camera", "inference"):
        status.update_component(component, "ready")
    controller = AppController(status, arm_callback=service.request_arm,
                               stop_callback=service.stop_motion)
    status.add_disarm_listener(controller.fail_safe_stop)
    assert controller.arm()[0]
    client.set_target_degrees(90)

    def fail(_payload):
        raise OSError("private device details")

    writer.write = fail
    client._send_target()
    health = client.snapshot()
    assert not health.ready and not health.armed
    assert health.target_degrees is None
    assert health.last_error == "transport_write_failed"
    snapshot = status.snapshot()
    assert snapshot["components"]["servo"]["state"] == "error"
    assert not snapshot["tracking"]["armed"]
    assert "private device" not in str(snapshot)
    assert not controller.arm()[0]
    with pytest.raises(RuntimeError, match="restart"):
        service.start()
    client.close()
    with pytest.raises(RuntimeError, match="restart"):
        client.start()


def test_serial_is_115200_8n1_and_short_write_fails_closed():
    arguments = {}

    class Port:
        def write(self, payload): return len(payload) - 1
        def close(self): pass

    def factory(**kwargs):
        arguments.update(kwargs)
        return Port()

    client = serial_client(device="/dev/serial/by-id/test", baud=115200,
                           angle_min_deg=0, angle_max_deg=180, send_hz=20,
                           serial_factory=factory)
    client.start()
    try:
        client.arm()
        client.set_target_degrees(90)
        client._send_target()
        assert client.snapshot().last_error == "transport_write_failed"
        assert arguments == dict(port="/dev/serial/by-id/test", baudrate=115200,
                                 bytesize=8, parity="N", stopbits=1, timeout=0,
                                 write_timeout=0.1, exclusive=True)
    finally:
        client.close()


def test_uart_service_rejects_udp_even_without_config_loader():
    with pytest.raises(ValueError, match="requires UART"):
        build_servo_service(replace(ServoConfig(), transport="api", pan_direction=-1),
                            StatusStore())


def test_background_failure_reports_without_another_target():
    client = SimpleServoClient(Writer(), angle_min_deg=0, angle_max_deg=180)
    status = StatusStore()
    service = ServoService(client, status)
    reported = threading.Event()
    status.add_disarm_listener(lambda _reason: reported.set())
    service.start()
    try:
        for component in ("camera", "inference"):
            status.update_component(component, "ready")
        assert status.request_arm()[0]
        assert service.request_arm()[0]

        def fail(_payload):
            raise OSError("write failed")

        client.writer.write = fail
        client.set_target_degrees(90)
        assert reported.wait(1)
        assert not status.snapshot()["tracking"]["armed"]
        assert not client.snapshot().armed
    finally:
        service.close()


@pytest.mark.parametrize("minimum, maximum, hz", [(-1, 180, 20), (0, 181, 20),
                                                         (0, 180, 4)])
def test_client_rejects_limits_or_cadence_outside_uart_contract(minimum, maximum, hz):
    with pytest.raises(ValueError):
        SimpleServoClient(Writer(), angle_min_deg=minimum, angle_max_deg=maximum,
                          send_hz=hz)


def test_expired_producer_cannot_keep_board_lease_alive(controlled_client):
    client, writer = controlled_client
    now = [0.0]
    client.clock = lambda: now[0]
    status = StatusStore()
    service = ServoService(client, status)
    service.start()
    for component in ('camera', 'inference'):
        status.update_component(component, 'ready')
    assert status.request_arm()[0]
    assert service.request_arm()[0]
    client.set_target_degrees(90)
    now[0] = .249
    client._send_target()
    assert writer.payloads == [b'SET 90000\n']
    now[0] = .250
    client._send_target()
    assert writer.payloads == [b'SET 90000\n']
    assert client.snapshot().last_error == 'target_expired'
    assert not client.snapshot().armed and not client.snapshot().ready
    assert not status.snapshot()['tracking']['armed']
    assert '目標更新逾時' in status.snapshot()['components']['servo']['detail']
    with pytest.raises(RuntimeError):
        client.arm()


def test_identical_fresh_targets_can_hold_angle_without_expiring(controlled_client):
    client, writer = controlled_client
    now = [0.0]
    client.clock = lambda: now[0]
    client.arm()
    for tick in range(100):
        now[0] = tick * .05
        client.set_target_degrees(90)
        client._send_target()
    assert len(writer.payloads) == 100
    assert client.snapshot().last_error is None


def test_target_timestamp_is_not_refreshed_by_waiting_for_writer_lock(controlled_client):
    client, writer = controlled_client
    timestamp_read = threading.Event()
    producer_finished = threading.Event()
    now = [0.0]
    def clock():
        value = now[0]
        timestamp_read.set()
        return value
    client.clock = clock
    client.arm()
    def produce():
        client.set_target_degrees(90)
        producer_finished.set()
    with client._lock:
        producer = threading.Thread(target=produce)
        producer.start()
        assert timestamp_read.wait(1)
        now[0] = .300
        assert not producer_finished.is_set()
    producer.join(1)
    assert producer_finished.is_set()
    client._send_target()
    assert writer.payloads == []
    assert client.snapshot().last_error == 'target_expired'
