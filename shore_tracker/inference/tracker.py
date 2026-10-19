from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import time
from typing import Iterable

from .types import DetectionPrediction, NormalizedBox


@dataclass(frozen=True, slots=True)
class RawDetection:
    box: NormalizedBox
    confidence: float

    def __post_init__(self) -> None:
        if not isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be in [0, 1]")


@dataclass(slots=True)
class _Track:
    track_id: int
    box: NormalizedBox
    last_seen_at: float


def box_iou(first: NormalizedBox, second: NormalizedBox) -> float:
    left = max(first.x, second.x)
    top = max(first.y, second.y)
    right = min(first.x + first.width, second.x + second.width)
    bottom = min(first.y + first.height, second.y + second.height)
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    union = first.width * first.height + second.width * second.height - intersection
    return min(1.0, intersection / union) if union > 0 else 0.0


class IoUTracker:
    """Small deterministic tracker used by the native TensorRT POC."""

    def __init__(self, *, match_iou: float = 0.30, grace_seconds: float = 5.0,
                 clock=time.monotonic) -> None:
        if not 0 < match_iou <= 1:
            raise ValueError("match_iou must be in (0, 1]")
        if isinstance(grace_seconds, bool) or not isfinite(grace_seconds) or grace_seconds <= 0:
            raise ValueError("grace_seconds must be positive and finite")
        self.match_iou = match_iou
        self.grace_seconds = grace_seconds
        self.clock = clock
        self._last_now: float | None = None
        self._next_id = 1
        self._tracks: dict[int, _Track] = {}

    def reset(self) -> None:
        self._tracks.clear()
        self._next_id = 1
        self._last_now = None

    def update(self, detections: Iterable[RawDetection], *, now: float | None = None) -> tuple[DetectionPrediction, ...]:
        now = self.clock() if now is None else now
        if not isfinite(now) or (self._last_now is not None and now < self._last_now):
            raise ValueError("tracker time must be finite and monotonic")
        found = tuple(detections)
        self._last_now = now
        # Expire before matching so a returning box cannot revive an expired ID.
        # Use elapsed time, independent of inference FPS and skipped frames.
        for track_id, track in tuple(self._tracks.items()):
            if now - track.last_seen_at >= self.grace_seconds:
                del self._tracks[track_id]
        candidates = sorted(
            (
                (box_iou(track.box, detection.box), track_id, index)
                for track_id, track in self._tracks.items()
                for index, detection in enumerate(found)
            ),
            key=lambda item: (-item[0], item[1], item[2]),
        )
        matched_tracks: set[int] = set()
        matched_detections: set[int] = set()
        assignments: dict[int, int] = {}
        for overlap, track_id, index in candidates:
            if overlap < self.match_iou:
                break
            if track_id in matched_tracks or index in matched_detections:
                continue
            matched_tracks.add(track_id)
            matched_detections.add(index)
            assignments[index] = track_id

        output: list[DetectionPrediction] = []
        for index, detection in enumerate(found):
            track_id = assignments.get(index)
            if track_id is None:
                track_id = self._next_id
                self._next_id += 1
                self._tracks[track_id] = _Track(track_id, detection.box, now)
            else:
                track = self._tracks[track_id]
                track.box = detection.box
                track.last_seen_at = now
            output.append(DetectionPrediction(track_id, detection.box, detection.confidence))
        return tuple(output)
