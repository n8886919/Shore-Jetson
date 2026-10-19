from __future__ import annotations

import time
import threading
from functools import wraps
from collections.abc import Callable, Iterable
from dataclasses import replace

from shore_tracker.config import (
    MAX_FRAME_STALE_SECONDS,
    ServoConfig,
    TrackingConfig,
    ZoomConfig,
)
from shore_tracker.control import (
    PanPlanner,
    PanPlannerConfig,
    ZoomPlanner,
    ZoomPlannerConfig,
)
from shore_tracker.inference import (
    FollowDecision,
    FollowDecisionReason,
    FollowState,
    TargetFollowConfig,
    TargetFollowStateMachine,
    TrackObservation,
)
from shore_tracker.status import StatusStore


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class TrackingCoordinator:
    """Connect tracker observations to bounded high-level Servo angles."""

    def __init__(
        self,
        tracking: TrackingConfig,
        servo: ServoConfig,
        status: StatusStore,
        command_target: Callable[[float], None],
        zoom: ZoomConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        on_decision: Callable[[FollowDecision, float], None] | None = None,
    ) -> None:
        if not 0 < tracking.frame_stale_seconds <= MAX_FRAME_STALE_SECONDS:
            raise ValueError(
                f"frame stale deadline must be in (0, {MAX_FRAME_STALE_SECONDS}]"
            )
        self._lock = threading.RLock()
        self._control_stop = threading.Event()
        self._tracking_reset = threading.Event()
        self._control_thread = None
        self._decision = None
        self.status = status
        self.status.configure_recognition(chasing_wave=tracking.chasing_threshold,
            takeoff=tracking.takeoff_threshold, surfing=tracking.surfing_threshold,
            stale_seconds=tracking.frame_stale_seconds)
        self.command_target = command_target
        self.clock = clock
        self.on_decision = on_decision
        self.home_deg = servo.home_deg
        self.frame_stale_seconds = tracking.frame_stale_seconds
        self.machine = TargetFollowStateMachine(
            TargetFollowConfig.from_values(
                chasing_threshold=tracking.chasing_threshold,
                takeoff_threshold=tracking.takeoff_threshold,
                surfing_threshold=tracking.surfing_threshold,
                candidate_hold_seconds=tracking.candidate_hold_seconds,
                finished_hold_seconds=tracking.finished_hold_seconds,
                ride_exit_seconds=tracking.ride_exit_seconds,
            )
        )
        self.planner = PanPlanner(
            PanPlannerConfig(
                angle_min_deg=servo.angle_min_deg,
                angle_max_deg=servo.angle_max_deg,
                search_min_deg=servo.search_min_deg,
                search_max_deg=servo.search_max_deg,
                max_slew_deg_s=servo.max_slew_deg_s,
                search_speed_deg_s=servo.search_speed_deg_s,
                pan_gain_deg=servo.pan_gain_deg,
                deadband=servo.pan_deadband,
                direction=servo.pan_direction or 1,
                home_deg=servo.home_deg,
            )
        )
        zoom_config = zoom or ZoomConfig()
        self.zoom_planner = ZoomPlanner(
            ZoomPlannerConfig(
                maximum=zoom_config.maximum if zoom_config.enabled else 1.0,
                target_box_height=zoom_config.target_box_height,
                edge_margin=zoom_config.edge_margin,
                fast_target_speed=zoom_config.fast_target_speed,
                slow_inference_seconds=zoom_config.slow_inference_seconds,
                zoom_in_per_second=zoom_config.zoom_in_per_second,
                zoom_out_per_second=zoom_config.zoom_out_per_second,
            )
        )
        # POC deliberately has no controller state sync. This is the last angle
        # commanded by this process, not physical shaft feedback.
        self.current_angle_deg: float = servo.home_deg
        self._last_frame_at: float | None = None
        self._was_armed = False
        self.status.update_tuning(
            pan_gain_deg=self.planner.config.pan_gain_deg,
            pan_deadband=self.planner.config.deadband,
            pan_direction=self.planner.config.direction,
            zoom_maximum=self.zoom_planner.config.maximum,
        )

    @_serialized
    def update_tuning(
        self,
        *,
        pan_gain_deg: float,
        pan_deadband: float,
        pan_direction: int,
        zoom_maximum: float,
    ) -> None:
        if not 1.0 <= pan_gain_deg <= 120.0:
            raise ValueError("pan_gain_deg must be in [1, 120]")
        if not 0.0 <= pan_deadband <= 0.20:
            raise ValueError("pan_deadband must be in [0, 0.2]")
        if isinstance(pan_direction, bool) or pan_direction not in {-1, 1}:
            raise ValueError("pan_direction must be -1 or +1")
        if pan_direction != self.planner.config.direction:
            raise ValueError("Pan 方向已固定，無法從現場微調變更")
        if not 1.0 <= zoom_maximum <= 4.0:
            raise ValueError("zoom_maximum must be in [1, 4]")
        self.planner.config = replace(
            self.planner.config,
            pan_gain_deg=pan_gain_deg,
            deadband=pan_deadband,
            direction=pan_direction,
        )
        self.zoom_planner.config = replace(
            self.zoom_planner.config,
            maximum=zoom_maximum,
        )
        self.status.update_tuning(
            pan_gain_deg=pan_gain_deg,
            pan_deadband=pan_deadband,
            pan_direction=pan_direction,
            zoom_maximum=zoom_maximum,
        )

    @_serialized
    def process_frame(
        self,
        observations: Iterable[TrackObservation],
        *,
        now: float | None = None,
    ) -> FollowDecision:
        timestamp = self.clock() if now is None else now
        self._consume_reset()
        items = tuple(observations)
        self.status.update_observations(items)
        previous_frame_at = self._last_frame_at
        self._last_frame_at = timestamp
        # The single inference loop publishes readiness after a completed frame.
        armed = bool(self.status.snapshot()["tracking"]["armed"])
        if not armed:
            if self._was_armed:
                self.machine.reset()
                self.planner.reset()
            self._was_armed = False
            self._decision = None
            self.zoom_planner.reset()
            return FollowDecision(
                FollowState.SEARCHING,
                None,
                None,
                FollowDecisionReason.SEARCHING,
            )

        if not self._was_armed:
            self.machine.reset()
            self.planner.reset()
        self._was_armed = True
        decision = self.machine.process(timestamp, items)
        self._decision = decision
        target = decision.target
        interval = (
            0.0
            if previous_frame_at is None
            else max(0.0, timestamp - previous_frame_at)
        )
        zoom = self.zoom_planner.step(
            decision,
            now=timestamp,
            inference_interval_seconds=interval,
        )
        self.status.update_metrics(
            zoom_scale=zoom.scale,
            zoom_center_x=zoom.center_x,
            target_speed=zoom.target_speed,
        )
        self.status.update_target(
            mode=decision.state.value,
            track_id=decision.target_track_id,
            target_x=(target.detection.box.center_x if target else None),
            chasing_wave=(target.action.chasing_wave if target else None),
            takeoff=(target.action.takeoff if target else None),
            surfing=(target.action.surfing if target else None),
            reason=decision.reason.value,
        )
        if self.on_decision is not None:
            self.on_decision(decision, timestamp)
        return decision

    @_serialized
    def check_freshness(self, *, now: float | None = None) -> bool:
        timestamp = self.clock() if now is None else now
        if self._last_frame_at is None:
            return False
        age = max(0.0, timestamp - self._last_frame_at)
        self.status.update_metrics(inference_frame_age_ms=round(age * 1000))
        if age > self.frame_stale_seconds:
            self.status.update_component("inference", "stale", "推論影格逾時")
            self.machine.reset()
            self.planner.reset()
            self._was_armed = False
            self._decision = None
            self.zoom_planner.reset()
            return False
        return True

    @_serialized
    def control_step(self, *, now=None):
        timestamp = self.clock() if now is None else now
        self._consume_reset()
        if not self.status.snapshot()['tracking']['armed']:
            self.planner.reset()
            self._decision = None
            self._was_armed = False
            return
        if self._decision is None or not self.check_freshness(now=timestamp):
            return
        angle = self.planner.step(now=timestamp, current_angle_deg=self.current_angle_deg,
                                  decision=self._decision, armed=True)
        try:
            self.command_target(angle)
        except Exception:
            self.status.update_component('servo', 'error', '目標角度傳送失敗')
            raise
        self.current_angle_deg = angle
        self.status.update_metrics(servo_angle_deg=angle)

    def start(self):
        with self._lock:
            if self._control_thread is not None: return
            self._control_stop.clear()
            self._control_thread = threading.Thread(target=self._control_loop,
                name='pan-control-20hz', daemon=True)
            self._control_thread.start()

    def cancel_tracking(self, _reason=''):
        # Status listeners may run while the API holds its control lock.
        # Never wait for the coordinator lock from that callback.
        self._tracking_reset.set()

    def _consume_reset(self):
        if not self._tracking_reset.is_set(): return
        self._tracking_reset.clear()
        self._decision = None
        self._was_armed = False
        self.machine.reset()
        self.planner.reset()

    def _control_loop(self):
        while not self._control_stop.wait(.05):
            try:
                self.control_step()
            except Exception:
                self.status.update_component('servo', 'error', '角度控制迴圈失敗')

    def close(self):
        self._control_stop.set()
        thread = self._control_thread
        if thread is not None: thread.join(timeout=2)
        if thread is None or not thread.is_alive():
            self._control_thread = None
            return True
        return False
