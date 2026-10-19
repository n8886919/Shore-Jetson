from shore_tracker.controller import AppController
from shore_tracker.status import StatusStore
import threading
import time


def _ready_status() -> StatusStore:
    status = StatusStore()
    for component in ("camera", "inference", "servo", "recording"):
        status.update_component(component, "ready")
    return status


def test_hardware_arm_waits_for_callback_ack() -> None:
    status = _ready_status()
    controller = AppController(status, arm_callback=lambda: (True, "ARM sent"))
    assert controller.arm() == (True, "ARM sent")
    snapshot = status.snapshot()
    assert snapshot["tracking"]["mode"] == "arming"
    assert snapshot["tracking"]["armed"] is False


def test_serial_service_can_confirm_arm_before_callback_returns() -> None:
    from shore_tracker.servo.simple import SimpleServoClient
    from shore_tracker.servo.simple_service import ServoService

    class Writer:
        def __init__(self):
            self.payloads = []

        def open(self): pass
        def write(self, payload): self.payloads.append(payload)
        def close(self): pass

    status = _ready_status()
    writer = Writer()
    client = SimpleServoClient(writer, angle_min_deg=0, angle_max_deg=180)
    service = ServoService(client, status)
    controller = AppController(
        status, arm_callback=service.request_arm, stop_callback=service.stop_motion,
    )
    service.start()
    try:
        assert controller.arm() == (True, "armed")
        assert status.snapshot()["tracking"]["armed"] is True
        assert client.snapshot().armed is True
        assert writer.payloads == []  # Local enable waits for a fresh target.
    finally:
        service.close()


def test_fault_after_synchronous_arm_confirmation_still_stops() -> None:
    status = _ready_status()
    operations = []

    def arm_callback():
        operations.append("ARM")
        status.confirm_armed()
        status.update_component("camera", "error", "lost")
        return True, "armed"

    controller = AppController(
        status, arm_callback=arm_callback,
        stop_callback=lambda: operations.append("STOP"),
    )
    status.add_disarm_listener(controller.fail_safe_stop)
    assert controller.arm()[0] is False
    assert status.snapshot()["tracking"]["armed"] is False
    assert operations[-1] == "STOP"


def test_rejected_hardware_arm_fails_closed() -> None:
    status = _ready_status()
    controller = AppController(status, arm_callback=lambda: (False, "profile mismatch"))
    assert controller.arm() == (False, "profile mismatch")
    assert status.snapshot()["tracking"]["armed"] is False


def test_stop_disarms_locally_even_if_delivery_fails() -> None:
    status = _ready_status()
    status.arm()

    def fail() -> None:
        raise OSError("do not expose details")

    controller = AppController(status, stop_callback=fail)
    assert controller.stop()[0] is False
    assert status.snapshot()["tracking"]["armed"] is False


def test_component_fault_racing_arm_leaves_stop_as_last_hardware_operation() -> None:
    status = _ready_status()
    entered = threading.Event()
    release = threading.Event()
    operations = []

    def arm_callback():
        entered.set()
        assert release.wait(2)
        operations.append("ARM")
        return True, "ARM sent"

    controller = AppController(
        status,
        arm_callback=arm_callback,
        stop_callback=lambda: operations.append("STOP"),
    )
    status.add_disarm_listener(controller.fail_safe_stop)
    arm_result = []
    arm_thread = threading.Thread(target=lambda: arm_result.append(controller.arm()))
    fault_thread = threading.Thread(
        target=lambda: status.update_component("camera", "error", "lost")
    )
    arm_thread.start()
    assert entered.wait(1)
    fault_thread.start()
    deadline = time.monotonic() + 1
    while status.snapshot()["tracking"]["mode"] != "disarmed":
        assert time.monotonic() < deadline
        time.sleep(0.001)
    release.set()
    arm_thread.join(timeout=2)
    fault_thread.join(timeout=2)

    assert arm_result and arm_result[0][0] is False
    assert operations[-1] == "STOP"
    assert status.snapshot()["tracking"]["armed"] is False


def test_pan_direction_change_requires_stop() -> None:
    status = _ready_status()
    status.arm()
    changes = []
    controller = AppController(
        status,
        tuning_callback=lambda **values: changes.append(values),
    )

    ok, detail = controller.tune(
        pan_gain_deg=45,
        pan_deadband=0.04,
        pan_direction=-1,
        zoom_maximum=2.5,
    )

    assert not ok
    assert "STOP" in detail
    assert changes == []
