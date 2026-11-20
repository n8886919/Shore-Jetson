"""Bounded decoded frames, independent of JPEG preview and recording."""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time

from PIL import Image


@dataclass(frozen=True)
class DecodedFrame:
    sequence: int
    captured_monotonic: float
    rgba: bytes
    width: int
    height: int
    stride: int
    generation: int = 0
    source_pts_ns: int | None = None

    def to_image(self) -> Image.Image:
        return Image.frombytes("RGB", (self.width, self.height), self.rgba,
                               "raw", "RGBX", self.stride)


class DecodedFrames:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._frame: DecodedFrame | None = None
        self._sequence = 0
        self._generation = 0
        self._camera_generation = None

    def begin_session(self, *, camera_generation: int = 0):
        with self._condition:
            self._generation += 1
            generation = self._generation
            self._camera_generation = camera_generation
            self._frame = None
            self._condition.notify_all()

        def publish(rgba: bytes, width: int, height: int, stride: int,
                    *, source_pts_ns: int | None = None) -> None:
            if min(width, height) <= 0 or stride < width * 4 or len(rgba) < stride * height:
                raise ValueError("invalid RGBA frame layout")
            with self._condition:
                if generation != self._generation:
                    return
                self._sequence += 1
                self._frame = DecodedFrame(self._sequence, time.monotonic(),
                                           rgba, width, height, stride,
                                           camera_generation, source_pts_ns)
                self._condition.notify_all()
        return publish

    def is_current_generation(self, generation):
        with self._condition:
            return generation == self._camera_generation

    def wait_after(self, sequence: int, timeout: float) -> DecodedFrame | None:
        with self._condition:
            self._condition.wait_for(
                lambda: self._frame is not None and self._frame.sequence > sequence,
                timeout=timeout,
            )
            frame = self._frame
            return frame if frame is not None and frame.sequence > sequence else None
