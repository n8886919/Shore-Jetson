from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from collections import deque


@dataclass(frozen=True)
class PreviewFrame:
    sequence: int
    captured_monotonic: float
    jpeg: bytes


class PreviewSession:
    """A generation-scoped preview source for one camera pipeline session."""

    def __init__(self, store: PreviewStore, generation: int) -> None:
        self._store = store
        self._generation = generation
        self._frame: PreviewFrame | None = None

    def publish(self, jpeg: bytes, *, captured_monotonic: float | None = None) -> None:
        self._store._publish_for_session(self, jpeg, captured_monotonic)

    def latest(self) -> PreviewFrame | None:
        with self._store._condition:
            return self._frame


class PreviewStore:
    """Keeps only the newest dashboard JPEG; it is never a recording archive."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._frame: PreviewFrame | None = None
        self._sequence = 0
        self._active_generation = 0
        self._frame_times: deque[float] = deque(maxlen=120)

    def begin_session(self) -> PreviewSession:
        """Invalidate the dashboard frame and return a scoped camera view.

        Late callbacks keep their own generation's last frame, but can never
        publish it into a newer dashboard session.  A segment finalizer holding
        the returned view therefore cannot accidentally use another session's
        thumbnail.
        """

        with self._condition:
            self._active_generation += 1
            self._frame = None
            self._frame_times.clear()
            self._condition.notify_all()
            return PreviewSession(self, self._active_generation)

    def publish(self, jpeg: bytes, *, captured_monotonic: float | None = None) -> None:
        """Publish an unscoped frame (primarily for simple/test producers)."""

        with self._condition:
            frame = self._new_frame(jpeg, captured_monotonic)
            self._frame = frame
            self._note_frame_time(frame.captured_monotonic)
            self._condition.notify_all()

    def _publish_for_session(
        self,
        session: PreviewSession,
        jpeg: bytes,
        captured_monotonic: float | None,
    ) -> None:
        with self._condition:
            frame = self._new_frame(jpeg, captured_monotonic)
            session._frame = frame
            if session._generation == self._active_generation:
                self._frame = frame
                self._note_frame_time(frame.captured_monotonic)
                self._condition.notify_all()

    def _note_frame_time(self, timestamp: float) -> None:
        if self._frame_times and timestamp <= self._frame_times[-1]:
            self._frame_times.clear()
        self._frame_times.append(timestamp)
        while len(self._frame_times) > 2 and self._frame_times[0] < timestamp - 2.0:
            self._frame_times.popleft()

    def frame_rate(self, *, now: float | None = None, stale_seconds: float = 2.0) -> float | None:
        """Recent JPEG production rate, independent of inference and HTTP clients."""
        timestamp = time.monotonic() if now is None else now
        with self._condition:
            if len(self._frame_times) < 2 or timestamp - self._frame_times[-1] > stale_seconds:
                return None
            span = self._frame_times[-1] - self._frame_times[0]
            return (len(self._frame_times) - 1) / span if span > 0 else None

    def _new_frame(
        self,
        jpeg: bytes,
        captured_monotonic: float | None,
    ) -> PreviewFrame:
        if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
            raise ValueError("preview frame must be a complete JPEG")
        with self._condition:
            self._sequence += 1
            return PreviewFrame(
                sequence=self._sequence,
                captured_monotonic=(
                    time.monotonic()
                    if captured_monotonic is None
                    else captured_monotonic
                ),
                jpeg=bytes(jpeg),
            )

    def latest(self) -> PreviewFrame | None:
        with self._condition:
            return self._frame

    def wait_after(self, sequence: int, timeout: float) -> PreviewFrame | None:
        with self._condition:
            if self._frame is None or self._frame.sequence <= sequence:
                self._condition.wait(timeout)
            if self._frame is None or self._frame.sequence <= sequence:
                return None
            return self._frame
