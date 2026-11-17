"""Reject isolated detections before action scores can influence tracking."""
from __future__ import annotations


class DetectionConfirmation:
    def __init__(self, frames=3):
        self.frames = frames
        self._counts = {}

    def reset(self):
        self._counts.clear()

    def update(self, observations):
        items = tuple(observations)
        self._counts = {item.track_id: min(self.frames, self._counts.get(item.track_id, 0) + 1)
                        for item in items}
        return tuple(item for item in items if self._counts[item.track_id] >= self.frames)
