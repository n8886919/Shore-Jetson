from __future__ import annotations

import math
import threading
from collections.abc import Callable
from pathlib import Path

from shore_tracker.status import StatusStore
from shore_tracker.storage import DiskGuard, DiskPressure, DiskStatus


class StorageMonitor:
    """Monitor free space and latch recording off at the stop threshold."""

    def __init__(
        self,
        *,
        path: str | Path,
        guard: DiskGuard,
        status: StatusStore,
        on_stop: Callable[[], None],
        poll_seconds: float = 5.0,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("storage poll interval must be positive")
        self.path = Path(path)
        self.guard = guard
        self.status = status
        self.on_stop = on_stop
        self.poll_seconds = poll_seconds
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._stop = threading.Event()
        self._close_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._latched = False

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._latched

    def start(self) -> DiskStatus | None:
        with self._lifecycle_lock:
            if self._close_requested.is_set():
                raise RuntimeError("storage monitor is closed")
            first = self.check_once()
            with self._lock:
                if self._thread is not None:
                    return first
                self._stop.clear()
                thread = threading.Thread(
                    target=self._run,
                    name="storage-monitor",
                    daemon=True,
                )
                self._thread = thread
            try:
                thread.start()
            except BaseException:
                self._stop.set()
                with self._lock:
                    if (
                        self._thread is thread
                        and thread.ident is None
                        and not thread.is_alive()
                    ):
                        self._thread = None
                raise
            return first

    def close(self, *, timeout: float | None = None) -> bool:
        """Stop the monitor, retaining its thread when a callback is still live."""

        if timeout is None:
            timeout_seconds = min(2.0, self.poll_seconds + 0.1)
        elif (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout < 0
        ):
            raise ValueError("storage close timeout must be finite and non-negative")
        else:
            timeout_seconds = float(timeout)
        with self._lifecycle_lock:
            self._close_requested.set()
            self._stop.set()
            with self._lock:
                thread = self._thread
            if thread is None:
                return True
            if thread is threading.current_thread():
                return False
            if thread.ident is not None or thread.is_alive():
                thread.join(timeout=timeout_seconds)
            if thread.is_alive():
                return False
            with self._lock:
                if self._thread is thread:
                    self._thread = None
            return True

    def check_once(self) -> DiskStatus | None:
        try:
            disk = self.guard.check(self.path)
        except OSError:
            self.status.update_component("recording", "error", "錄影磁碟無法讀取")
            self._trip()
            return None
        self.status.update_metrics(
            free_gib=disk.free_gib,
            disk_pressure=disk.pressure.value,
        )
        if disk.pressure is DiskPressure.STOP:
            self.status.update_component(
                "recording", "paused", "磁碟已達停止門檻，保留未驗證影片"
            )
            self._trip()
        return disk

    def _trip(self) -> None:
        with self._lock:
            if self._latched:
                return
            self._latched = True
        try:
            self.on_stop()
        except Exception:
            # The status transition has already disarmed Servo control. The
            # runtime will remain latched and report recording as stopped.
            return

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.poll_seconds):
                self.check_once()
        except BaseException:
            if self._stop.is_set():
                return
            try:
                self.status.update_component(
                    "recording", "error", "磁碟監督 worker 執行失敗"
                )
            finally:
                self._trip()
