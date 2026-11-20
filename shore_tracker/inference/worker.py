from __future__ import annotations

import threading
import time
import math
from collections.abc import Callable
from typing import Protocol

from shore_tracker.coordinator import TrackingCoordinator
from shore_tracker.inference.types import TrackObservation
from shore_tracker.inference.frames import DecodedFrame
from shore_tracker.inference.confirmation import DetectionConfirmation
from shore_tracker.preview import PreviewStore
from shore_tracker.status import StatusStore


class InferenceModels(Protocol):
    def load(self) -> None: ...
    def infer_jpeg(self, jpeg: bytes) -> tuple[TrackObservation, ...]: ...


class InferenceWorker:
    """One inference thread with an inline stale-frame check."""

    def __init__(
        self,
        *,
        preview: PreviewStore,
        status: StatusStore,
        coordinator: TrackingCoordinator,
        models: InferenceModels,
        stale_seconds: float,
        max_fps: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        on_demand: bool = False,
        confirmation_frames: int = 1,
        position_frames=None,
    ) -> None:
        self.preview = preview
        self.status = status
        self.coordinator = coordinator
        self.models = models
        self.stale_seconds = stale_seconds
        self.clock = clock
        self.on_demand = on_demand
        self.confirmation = DetectionConfirmation(confirmation_frames)
        self.position = None
        self._position_thread = None
        if position_frames is not None and on_demand:
            from shore_tracker.inference.position import LockedPosition
            self.position = LockedPosition(position_frames, coordinator, status, stale_seconds, clock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rate_condition = threading.Condition()
        self.set_max_fps(max_fps)

    @property
    def max_fps(self) -> float:
        with self._rate_condition:
            return self._max_fps

    def set_max_fps(self, value: float) -> None:
        if type(value) not in {int, float} or not math.isfinite(value) or not (value == 0 or 1 <= value <= 10):
            raise ValueError("辨識頻率必須為 0（不限速）或介於 1–10 Hz")
        with self._rate_condition:
            self._max_fps = float(value)
            self._rate_condition.notify_all()

    def _wait_for_rate_slot(self, previous_start: float | None) -> bool:
        with self._rate_condition:
            while not self._stop.is_set():
                delay = 0.0 if previous_start is None or self._max_fps == 0 else previous_start + 1.0 / self._max_fps - time.monotonic()
                if delay <= 0:
                    return True
                self._rate_condition.wait(delay)
        return False

    def start(self) -> None:
        if self._thread is not None:
            return
        self.status.update_component("inference", "waiting", "loading TensorRT engines")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="surftrack-inference", daemon=True
        )
        self._thread.start()
        if self.position is not None:
            self._position_thread = threading.Thread(target=self.position.watch,
                args=(self._stop,), name="rider-position", daemon=True)
            self._position_thread.start()

    def close(self, *, timeout: float = 2.0) -> bool:
        self._stop.set()
        with self._rate_condition:
            self._rate_condition.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        stopped = thread is None or not thread.is_alive()
        if self._position_thread is not None:
            self._position_thread.join(timeout=max(0.0, timeout))
            stopped = stopped and not self._position_thread.is_alive()
        if stopped:
            self._thread = self._position_thread = None
        return stopped

    def _run(self) -> None:
        try:
            self.models.load()
            sequence = 0
            last_frame_at = self.clock()
            previous_completed_at: float | None = None
            previous_start: float | None = None
            active_run = None
            while not self._stop.is_set():
                run = self.status.inference_run() if self.on_demand else None
                if self.on_demand and run is None:
                    if self.status.snapshot()['components']['inference']['state'] != 'idle':
                        self.status.update_component('inference', 'idle', 'AI 辨識已關閉')
                    previous_completed_at = previous_start = None
                    active_run = None
                    last_frame_at = self.clock()
                    self._stop.wait(.05)
                    continue
                if self.on_demand and active_run != run:
                    active_run = run
                    self.confirmation.reset()
                    tracker = getattr(self.models, 'tracker', None)
                    if tracker is not None:
                        tracker.reset()
                # Wait before reading the store so each inference uses the
                # newest frame, without queuing or slowing preview delivery.
                if not self._wait_for_rate_slot(previous_start):
                    break
                frame = self.preview.wait_after(sequence, 0.1)
                now = self.clock()
                if frame is None:
                    if now - last_frame_at > self.stale_seconds:
                        self.status.update_component(
                            "inference", "stale", "no fresh camera frame"
                        )
                    continue
                sequence = frame.sequence
                if self.on_demand and (self.status.inference_run() != run or
                                       frame.captured_monotonic < run[1]):
                    continue
                previous_start = time.monotonic()
                started = self.clock()
                if isinstance(frame, DecodedFrame):
                    image = frame.to_image()
                    if self.position is None:
                        observations = self.models.infer_image(image)
                    else:
                        observations = self.models.infer_image(image,
                            position_hint=self.position.hint(frame, run))
                else:
                    observations = self.models.infer_jpeg(frame.jpeg)
                completed = self.clock()
                if self.on_demand and self.status.inference_run() != run:
                    continue  # STOP/restart invalidates work already on the GPU.
                if self.on_demand and completed - frame.captured_monotonic > self.stale_seconds:
                    continue  # Includes the first GPU warm-up; never steer from it.
                observations = self.confirmation.update(observations)
                last_frame_at = completed
                elapsed = max(1e-6, completed - started)
                frame_interval = (
                    elapsed
                    if previous_completed_at is None
                    else max(1e-6, completed - previous_completed_at)
                )
                measured_fps = None if previous_completed_at is None else 1.0 / frame_interval
                previous_completed_at = completed
                self.status.update_component("inference", "ready", "native TensorRT")
                self.status.update_metrics(
                    inference_frame_age_ms=round(elapsed * 1000),
                    inference_fps=measured_fps,
                )
                options = {'inference_run_id': run[0]} if self.on_demand else {}
                if self.position is not None and isinstance(frame, DecodedFrame):
                    self.position.detected(frame, run, observations, completed)
                else:
                    self.coordinator.process_frame(observations, now=completed, **options)
        except Exception:
            self.status.update_component(
                "inference", "error", "native TensorRT inference stopped"
            )
