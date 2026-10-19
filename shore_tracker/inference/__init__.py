"""Inference contracts and target-follow policy."""

from .selection import TargetSelector, TargetSelectorConfig
from .state_machine import (
    FollowDecision,
    FollowDecisionReason,
    FollowState,
    TargetFollowConfig,
    TargetFollowStateMachine,
)
from .types import (
    ACTION_NAMES,
    ActionPrediction,
    DetectionPrediction,
    NormalizedBox,
    TrackObservation,
)
from .tracker import IoUTracker, RawDetection, box_iou

__all__ = [
    "ACTION_NAMES",
    "ActionPrediction",
    "DetectionPrediction",
    "FollowDecision",
    "FollowDecisionReason",
    "FollowState",
    "IoUTracker",
    "NormalizedBox",
    "RawDetection",
    "TargetFollowConfig",
    "TargetFollowStateMachine",
    "TargetSelector",
    "TargetSelectorConfig",
    "TrackObservation",
    "box_iou",
]
