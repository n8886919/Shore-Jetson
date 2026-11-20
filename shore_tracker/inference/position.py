"""Local template matching for a single detector-confirmed surf rider."""
from __future__ import annotations

from collections import deque
from dataclasses import replace
import threading

import numpy as np
from PIL import Image



def green_plane(frame):
    # Native BGRx and detector RGBA have the same green byte. Use one common
    # coordinate system; neither path needs a JPEG encoder or an OpenCV wheel.
    plane = Image.frombuffer("RGBA", (frame.width, frame.height), frame.rgba,
                             "raw", "RGBA", frame.stride, 1).getchannel("G")
    return np.asarray(plane.resize((480, 270), Image.Resampling.BILINEAR))


class RiderPatch:
    def __init__(self, plane, box):
        self.box = box
        h, w = plane.shape
        self.x = round((box.x + box.width / 2) * w)
        self.y = round((box.y + box.height / 2) * h)
        rx = min(13, int(box.width * w * .38))
        ry = min(18, int(box.height * h * .38))
        self.template = None
        if (min(rx, ry) < 2 or self.x-rx < 0 or self.y-ry < 0
                or self.x+rx >= w or self.y+ry >= h):
            return
        self.dx = np.unique(np.linspace(-rx, rx, 9).astype(int))
        self.dy = np.unique(np.linspace(-ry, ry, 11).astype(int))
        values = plane[self.y + self.dy[:, None], self.x + self.dx].astype(np.float32)
        if values.std() >= 7:
            self.template = (values - values.mean()) / values.std()

    def locate(self, plane, center):
        if self.template is None:
            return None
        h, w = plane.shape
        cx, cy = center
        xs = np.arange(max(-self.dx[0], cx - 20), min(w - self.dx[-1], cx + 21))
        ys = np.arange(max(-self.dy[0], cy - 16), min(h - self.dy[-1], cy + 17))
        if not len(xs) or not len(ys):
            return None
        pixels = plane[(ys[:, None, None, None] + self.dy[None, None, :, None]),
                       (xs[None, :, None, None] + self.dx)].astype(np.float32)
        score = (pixels * self.template).mean(axis=(2, 3)) / np.maximum(
            pixels.std(axis=(2, 3)), 4)
        row, col = np.unravel_index(score.argmax(), score.shape)
        peak = score[row, col]
        score[max(0,row-2):row+3, max(0,col-2):col+3] = -1
        if peak < .76 or peak - score.max() < .08:
            return None
        x = self.box.x + (xs[col] - self.x) / w
        y = self.box.y + (ys[row] - self.y) / h
        if not (0 <= x <= 1-self.box.width and 0 <= y <= 1-self.box.height):
            return None
        return replace(self.box, x=float(x), y=float(y))


class LockedPosition:
    def __init__(self, frames, coordinator, status, stale_seconds, clock):
        self.frames = frames
        self.coordinator = coordinator
        self.status = status
        self.clock = clock
        self.stale_seconds = stale_seconds
        self.lock = threading.RLock()
        self.patch = None
        self.history = deque(maxlen=24)
        self.run = self.anchor = self.track_id = None

    @staticmethod
    def stamp(frame):
        pts = frame.source_pts_ns
        return pts if isinstance(pts, int) and 0 <= pts < 2**63 else None

    def _match(self, frame, run):
        stamp = self.stamp(frame)
        if (self.patch is None or self.run != run or self.anchor.generation != frame.generation
                or stamp is None or stamp < self.stamp(self.anchor)
                or self.clock() - self.anchor.captured_monotonic >= self.stale_seconds):
            return None
        for t, center in reversed(self.history):
            if t <= stamp:
                if stamp - t > 350_000_000 or center is None:
                    return None
                return self.patch.locate(green_plane(frame), center)
        return None

    def hint(self, frame, run):
        with self.lock:
            box = self._match(frame, run)
            return (self.track_id, box) if box is not None else None

    def detected(self, frame, run, observations, now):
        with self.lock:
            if not self.frames.is_current_generation(frame.generation):
                return
            self.patch = self.anchor = None
            decision = self.coordinator.process_frame(observations, now=now,
                inference_run_id=run[0], captured_at=frame.captured_monotonic)
            if decision.target is None or self.stamp(frame) is None:
                return
            self.run, self.anchor = run, frame
            self.track_id = decision.target_track_id
            self.patch = RiderPatch(green_plane(frame), decision.target.detection.box)
            self.history.clear()
            self.history.append((self.stamp(frame), (self.patch.x, self.patch.y)))
            latest = self.frames.wait_after(0, 0)
            if latest is not None:
                self.advance(latest)

    def advance(self, frame):
        with self.lock:
            if self.patch is None:
                return
            run = self.status.inference_run()
            stamp = self.stamp(frame)
            if run != self.run:
                self.patch = None
                return
            if frame.generation < self.anchor.generation:
                return
            if (frame.generation == self.anchor.generation and stamp is not None
                    and stamp <= self.history[-1][0]):
                return
            box = self._match(frame, run)
            if box is None or self.clock() - frame.captured_monotonic > self.stale_seconds:
                self.coordinator.update_locked_position(run[0], self.track_id, None)
                self.patch = None
                return
            self.history.append((stamp, (round(box.center_x * 480),
                                         round((box.y + box.height/2) * 270))))
            self.coordinator.update_locked_position(run[0], self.track_id, box)

    def watch(self, stop):
        sequence = 0
        while not stop.is_set():
            frame = self.frames.wait_after(sequence, .15)
            if frame is not None:
                sequence = frame.sequence
                self.advance(frame)
