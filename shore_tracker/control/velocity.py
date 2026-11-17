"""Image-error following without an assumed shaft angle or home position."""
from dataclasses import dataclass
from math import ceil, isfinite

from shore_tracker.inference import FollowDecision


@dataclass(frozen=True)
class VelocityPlannerConfig:
    max_speed: int = 8
    pan_gain_deg: float = 45.0  # Existing tuning API key; dimensionless here.
    deadband: float = .125
    direction: int = 0
    axis: str = "pan"


class VelocityPlanner:
    def __init__(self, config: VelocityPlannerConfig):
        if type(config.max_speed) is not int or not 1 <= config.max_speed <= 63:
            raise ValueError("Pelco speed limit must be in [1, 63]")
        if (not isfinite(config.pan_gain_deg) or config.pan_gain_deg <= 0
                or not 0 <= config.deadband < .5
                or type(config.direction) is not int or config.direction not in {-1, 0, 1}
                or config.axis not in {"pan", "tilt"}):
            raise ValueError("Invalid velocity planner configuration")
        self.config = config

    def reset(self):
        pass  # No integrated position or velocity survives a decision.

    def step(self, decision: FollowDecision | None, *, armed: bool) -> int:
        cfg = self.config
        if not armed or not cfg.direction or decision is None or decision.target is None:
            return 0
        box = decision.target.detection.box
        error = box.pan_error if cfg.axis == "pan" else box.tilt_error
        if abs(error) <= cfg.deadband:
            return 0
        fraction = (abs(error) - cfg.deadband) / (.5 - cfg.deadband)
        speed = min(cfg.max_speed, max(1, ceil(fraction * cfg.max_speed * cfg.pan_gain_deg / 45)))
        return cfg.direction * (speed if error > 0 else -speed)
