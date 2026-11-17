"""Small, in-memory presentation buffer; annotations belong to one source PTS."""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time


@dataclass(frozen=True)
class DisplayFrame:
    timestamp: float
    jpeg: bytes
    run_id: int | None = None
    recognition: dict | None = None


class DisplayFrames:
    def __init__(self, *, delay: float = 1.0, clock=time.monotonic):
        self.delay = delay
        self.clock = clock
        self._condition = threading.Condition()
        self._generation = 0
        self._anchor = None
        self._frames: dict[int, DisplayFrame] = {}

    def begin_session(self, generation):
        with self._condition:
            self._generation = generation
            self._anchor = None
            self._frames.clear()
            self._condition.notify_all()

    def publish(self, generation, pts, jpeg, *, run_id=None, recognition=None):
        # Gst.CLOCK_TIME_NONE and missing timestamps cannot be matched safely.
        if pts is None or not 0 <= pts < 2**63:
            return False
        with self._condition:
            if generation != self._generation:
                return False
            now = self.clock()
            if self._anchor is None:
                self._anchor = now - pts / 1e9
            timestamp = self._anchor + pts / 1e9
            if recognition is not None and timestamp <= now - self.delay:
                return False  # Never insert a late result into an already played frame.
            previous = self._frames.get(pts)
            if previous is None or recognition is not None:
                self._frames[pts] = DisplayFrame(timestamp, bytes(jpeg), run_id, recognition)
            cutoff = now - self.delay - .6
            self._frames = {k: v for k, v in self._frames.items() if v.timestamp > cutoff}
            # Hard memory bound even if a camera supplies a malformed timeline.
            if len(self._frames) > 48:
                for key in sorted(self._frames)[:-48]:
                    del self._frames[key]
            self._condition.notify_all()
            return True

    def wait_after(self, timestamp, timeout=.4):
        deadline = self.clock() + timeout
        with self._condition:
            while True:
                now = self.clock()
                ready = sorted((f for f in self._frames.values()
                                if timestamp < f.timestamp <= now - self.delay
                                and now - f.timestamp < self.delay + .6),
                               key=lambda f: f.timestamp)
                if ready:
                    return ready[-1] if timestamp == 0 or now - ready[0].timestamp > self.delay + .25 else ready[0]
                remaining = deadline - now
                if remaining <= 0:
                    return None
                self._condition.wait(min(.02, remaining))
