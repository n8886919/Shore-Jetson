from pathlib import Path
import threading
import time

from shore_tracker.status import StatusStore
from shore_tracker.storage import DiskPressure, DiskStatus
from shore_tracker.storage_service import StorageMonitor


class FakeGuard:
    def __init__(self, values):
        self.values = iter(values)

    def check(self, _path):
        value = next(self.values)
        if isinstance(value, BaseException):
            raise value
        return value


def _disk(pressure: DiskPressure, gib: int) -> DiskStatus:
    return DiskStatus(pressure, gib * 1024**3, 500 * 1024**3, (499 - gib) * 1024**3)


def _eventually(predicate, timeout=1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.002)


def test_warning_reports_metric_without_stopping_recording(tmp_path: Path) -> None:
    status = StatusStore()
    status.update_component("recording", "ready")
    stops = []
    monitor = StorageMonitor(
        path=tmp_path,
        guard=FakeGuard([_disk(DiskPressure.WARNING, 110)]),  # type: ignore[arg-type]
        status=status,
        on_stop=lambda: stops.append(True),
    )

    result = monitor.check_once()

    assert result is not None
    assert result.pressure is DiskPressure.WARNING
    assert stops == []
    assert status.snapshot()["metrics"]["used_gib"] == 389
    snapshot = status.snapshot()
    assert snapshot["components"]["recording"]["state"] == "ready"
    assert snapshot["metrics"]["disk_pressure"] == "warning"


def test_stop_threshold_latches_and_invokes_stop_only_once(tmp_path: Path) -> None:
    status = StatusStore()
    status.update_component("recording", "ready")
    stops = []
    monitor = StorageMonitor(
        path=tmp_path,
        guard=FakeGuard(
            [_disk(DiskPressure.STOP, 100), _disk(DiskPressure.STOP, 99)]
        ),  # type: ignore[arg-type]
        status=status,
        on_stop=lambda: stops.append(True),
    )

    monitor.check_once()
    monitor.check_once()

    assert monitor.stopped is True
    assert stops == [True]
    assert status.snapshot()["components"]["recording"]["state"] == "paused"


def test_disk_error_fails_closed(tmp_path: Path) -> None:
    status = StatusStore()
    stops = []
    monitor = StorageMonitor(
        path=tmp_path,
        guard=FakeGuard([OSError("operator path must not leak")]),  # type: ignore[arg-type]
        status=status,
        on_stop=lambda: stops.append(True),
    )

    status.update_metrics(used_gib=10, free_gib=50)
    assert monitor.check_once() is None
    assert status.snapshot()["metrics"]["used_gib"] is None
    assert status.snapshot()["metrics"]["free_gib"] is None
    assert stops == [True]
    assert "operator path" not in repr(status.snapshot())


def test_close_retains_worker_while_stop_callback_is_running(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def on_stop() -> None:
        entered.set()
        release.wait()

    monitor = StorageMonitor(
        path=tmp_path,
        guard=FakeGuard(
            [_disk(DiskPressure.WARNING, 110), _disk(DiskPressure.STOP, 100)]
        ),  # type: ignore[arg-type]
        status=StatusStore(),
        on_stop=on_stop,
        poll_seconds=0.002,
    )
    monitor.start()
    assert entered.wait(0.5)
    thread = monitor._thread

    assert monitor.close(timeout=0.01) is False
    assert monitor._thread is thread
    assert thread is not None and thread.is_alive()

    release.set()
    assert monitor.close(timeout=0.5) is True
    assert monitor._thread is None


def test_unexpected_worker_failure_trips_recording_fail_safe(tmp_path: Path) -> None:
    stops = []
    monitor = StorageMonitor(
        path=tmp_path,
        guard=FakeGuard(
            [_disk(DiskPressure.WARNING, 110), RuntimeError("unexpected")]
        ),  # type: ignore[arg-type]
        status=StatusStore(),
        on_stop=lambda: stops.append(True),
        poll_seconds=0.002,
    )
    monitor.start()

    _eventually(lambda: monitor.stopped)
    assert stops == [True]
    assert monitor.status.snapshot()["components"]["recording"]["state"] == "error"
    assert monitor.close(timeout=0.5) is True
