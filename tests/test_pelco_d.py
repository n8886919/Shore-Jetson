import pytest

from shore_tracker.servo.pelco_d import PelcoManualController, encode_motion


def arm(control):
    return control.arm(epoch=control.snapshot()["epoch"])


@pytest.mark.parametrize(("direction", "expected"), [
    ("stop", "ff 01 00 00 00 00 01"),
    ("left", "ff 01 00 04 08 00 0d"),
    ("right", "ff 01 00 02 08 00 0b"),
    ("up", "ff 01 00 08 00 08 11"),
    ("down", "ff 01 00 10 00 08 19"),
])
def test_known_wire_frames(direction, expected):
    assert encode_motion(1, direction) == bytes.fromhex(expected)


def test_checksum_wrap_and_no_turbo_or_broadcast():
    assert encode_motion(255, "left", 63) == bytes.fromhex("ff ff 00 04 3f 00 42")
    for args in [(0, "left", 8), (256, "left", 8), (True, "left", 8),
                 (1, "left", 64), (1, "left", 0), (1, "left", True),
                 (1, "left", 1.2), (1, "preset", 8), (1, [], 8)]:
        with pytest.raises(ValueError):
            encode_motion(*args)


class Port:
    def __init__(self):
        self.writes = []
        self.closed = False
        self.fail = False

    def write(self, frame):
        self.writes.append(frame)
        return 0 if self.fail else len(frame)

    def close(self):
        self.closed = True


def real_control(monkeypatch):
    monkeypatch.setattr("shore_tracker.servo.pelco_d.require_control_port", lambda _: None)
    port, calls, now = Port(), [], [0.0]
    def factory(**kwargs):
        calls.append(kwargs)
        return port
    control = PelcoManualController(device="/dev/test485", serial_factory=factory,
                                  clock=lambda: now[0])
    return control, port, calls, now


def test_constructor_connect_and_arm_never_send_motion(monkeypatch):
    control, port, calls, _ = real_control(monkeypatch)
    assert not calls
    with pytest.raises(ValueError):
        arm(control)
    control.connect()
    session = arm(control)
    assert session and not port.writes
    assert calls[0] == dict(port="/dev/test485", baudrate=9600, bytesize=8,
                           parity="N", stopbits=1, timeout=0, write_timeout=0.1,
                           exclusive=True, xonxoff=False, rtscts=False, dsrdtr=False)
    with pytest.raises(ValueError):
        arm(control)  # A second browser cannot silently replace the owner.


def test_stop_rejects_in_flight_commands_and_allows_next_press(monkeypatch):
    control, port, _, _ = real_control(monkeypatch)
    control.connect()
    session = arm(control)
    control.move(session=session, sequence=1, direction="right", speed=8)
    control.stop(session=session, sequence=3)
    with pytest.raises(ValueError):
        control.move(session=session, sequence=2, direction="right", speed=8)
    assert port.writes[-1] == encode_motion(1)
    control.move(session=session, sequence=4, direction="left", speed=8)
    control.stop(disarm=True)
    with pytest.raises(ValueError):
        control.move(session=session, sequence=5, direction="left", speed=8)


@pytest.mark.parametrize("deadline", [0.4, 0.401, 5])
def test_expired_browser_lease_cannot_be_revived_by_delayed_http(monkeypatch, deadline):
    control, port, _, now = real_control(monkeypatch)
    control.connect()
    session = arm(control)
    control.move(session=session, sequence=1, direction="up", speed=8)
    now[0] = deadline
    with pytest.raises(ValueError):
        control.move(session=session, sequence=2, direction="up", speed=8)
    assert port.writes == [encode_motion(1, "up"), encode_motion(1)]
    assert not control.snapshot()["armed"]


def test_heartbeats_cannot_extend_absolute_two_second_limit(monkeypatch):
    control, port, _, now = real_control(monkeypatch)
    control.connect()
    session = arm(control)
    for sequence in range(1, 21):
        now[0] = (sequence - 1) / 10
        control.move(session=session, sequence=sequence, direction="down", speed=1)
    now[0] = 2
    control.expire()
    assert port.writes[-1] == encode_motion(1)
    assert not control.snapshot()["armed"]


def test_write_failure_attempts_stop_latches_fault_and_disconnect_closes(monkeypatch):
    control, port, _, _ = real_control(monkeypatch)
    control.connect()
    session = arm(control)
    port.fail = True
    with pytest.raises(OSError, match="切斷雲台電源"):
        control.move(session=session, sequence=1, direction="left", speed=8)
    assert port.writes[-1] == encode_motion(1)
    assert not control.snapshot()["armed"]
    assert control.snapshot()["direction"] == "unknown"
    with pytest.raises(ValueError):
        arm(control)
    with pytest.raises(OSError):
        control.disconnect()
    assert port.closed and not control.snapshot()["connected"]
