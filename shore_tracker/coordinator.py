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
from shore_tracker.servo.keyboard import KeyboardJog
from shore_tracker.control.velocity import VelocityPlanner, VelocityPlannerConfig


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class TrackingCoordinator:
    """Connect observations to Pelco velocities or the legacy angle planner."""

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
        command_axes: Callable[[float, float | None], None] | None = None,
        disable_tilt: Callable[[], None] | None = None,
        command_velocity: Callable[[int, int, int], None] | None = None,
        pause_output: Callable[[], None] | None = None,
    ) -> None:
        if not 0 < tracking.frame_stale_seconds <= MAX_FRAME_STALE_SECONDS:
            raise ValueError(
                f"frame stale deadline must be in (0, {MAX_FRAME_STALE_SECONDS}]"
            )
        self._lock = threading.RLock()
        self._control_stop = threading.Event()
        self._tracking_reset = threading.Event()
        self._control_thread = None
        self._control_period = 1.0 / servo.send_hz
        self._decision = None
        self._decision_run_id = None
        self.status = status
        self.status.configure_recognition(chasing_wave=tracking.chasing_threshold,
            takeoff=tracking.takeoff_threshold, surfing=tracking.surfing_threshold,
            stale_seconds=tracking.frame_stale_seconds)
        self.command_target = command_target
        self.command_axes = command_axes
        self.disable_tilt = disable_tilt
        self._pause_output = pause_output
        self._pelco = servo.protocol == "pelco_d"
        self.command_velocity = command_velocity
        if self._pelco and command_velocity is None:
            raise ValueError("Pelco-D requires a velocity output")
        self.tilt_available = (servo.protocol in {"m0_pulse", "pelco_d"}
                               and servo.tilt_direction in {-1, 1}
                               and (command_velocity if self._pelco else command_axes) is not None
                               and disable_tilt is not None)
        if servo.tilt_enabled and not self.tilt_available:
            raise ValueError("俯仰控制尚未校準或未連接")
        self.tilt_enabled = servo.tilt_enabled
        self.clock = clock
        self.keyboard = KeyboardJog(servo, status, command_axes, pause_output, clock)
        self.on_decision = on_decision
        self.home_deg = None if self._pelco else servo.home_deg
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
        if self._pelco:
            self.planner = VelocityPlanner(VelocityPlannerConfig(
                max_speed=servo.pelco_pan_speed, pan_gain_deg=servo.pan_gain_deg,
                deadband=servo.pan_deadband, direction=servo.pan_direction))
            self.tilt_planner = VelocityPlanner(VelocityPlannerConfig(
                max_speed=servo.pelco_tilt_speed, pan_gain_deg=servo.pan_gain_deg,
                deadband=servo.pan_deadband, direction=servo.tilt_direction, axis="tilt"))
            self.status.update_servo_command(protocol="pelco_d", pan=None, tilt=None)
        else:
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
                    max_acceleration_deg_s2=servo.max_acceleration_deg_s2,
                )
            )
            self.tilt_planner = PanPlanner(PanPlannerConfig(
                angle_min_deg=servo.tilt_min_deg, angle_max_deg=servo.tilt_max_deg,
                search_min_deg=servo.tilt_min_deg, search_max_deg=servo.tilt_max_deg,
                home_deg=servo.tilt_home_deg, max_slew_deg_s=servo.max_slew_deg_s,
                search_speed_deg_s=servo.search_speed_deg_s,
                max_acceleration_deg_s2=servo.max_acceleration_deg_s2,
                pan_gain_deg=servo.pan_gain_deg, deadband=servo.pan_deadband,
                direction=servo.tilt_direction or 1, axis="tilt"))
        self.current_tilt_deg = None if self._pelco else servo.tilt_home_deg
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
        self.current_angle_deg: float | None = None if self._pelco else servo.home_deg
        self._last_frame_at: float | None = None
        self._was_armed = False
        self.status.update_tuning(
            pan_gain_deg=self.planner.config.pan_gain_deg,
            pan_deadband=self.planner.config.deadband,
            pan_direction=self.planner.config.direction,
            zoom_maximum=self.zoom_planner.config.maximum,
            tilt_enabled=self.tilt_enabled,
            tilt_available=self.tilt_available,
        )

    @_serialized
    def update_tuning(
        self,
        *,
        pan_gain_deg: float,
        pan_deadband: float,
        pan_direction: int,
        zoom_maximum: float,
        tilt_enabled: bool | None = None,
    ) -> None:
        if not 1.0 <= pan_gain_deg <= 120.0:
            raise ValueError("pan_gain_deg must be in [1, 120]")
        if not 0.05 <= pan_deadband <= 0.40:
            raise ValueError("跟拍容許區必須介於 10%–80%")
        allowed_directions = {-1, 0, 1} if self._pelco else {-1, 1}
        if type(pan_direction) is not int or pan_direction not in allowed_directions:
            raise ValueError("pan_direction must match the configured calibration")
        if pan_direction != self.planner.config.direction:
            raise ValueError("Pan 方向已固定，無法從現場微調變更")
        if not 1.0 <= zoom_maximum <= 4.0:
            raise ValueError("zoom_maximum must be in [1, 4]")
        if tilt_enabled is not None and type(tilt_enabled) is not bool:
            raise ValueError("tilt_enabled must be boolean")
        if tilt_enabled and not self.tilt_available:
            raise ValueError("俯仰控制尚未校準或未連接")
        if tilt_enabled is not None and tilt_enabled != self.tilt_enabled:
            if not tilt_enabled and self.disable_tilt is not None:
                self.disable_tilt()
            self.tilt_enabled = tilt_enabled
            self.tilt_planner.reset()
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
        self.tilt_planner.config = replace(self.tilt_planner.config,
            pan_gain_deg=pan_gain_deg, deadband=pan_deadband)
        self.status.update_tuning(
            pan_gain_deg=pan_gain_deg,
            pan_deadband=pan_deadband,
            pan_direction=pan_direction,
            zoom_maximum=zoom_maximum,
            tilt_enabled=self.tilt_enabled,
            tilt_available=self.tilt_available,
        )

    @_serialized
    def process_frame(
        self,
        observations: Iterable[TrackObservation],
        *,
        now: float | None = None,
        inference_run_id: int | None = None,
        captured_at: float | None = None,
    ) -> FollowDecision:
        timestamp = self.clock() if now is None else now
        self._consume_reset()
        run = self.status.inference_run()
        if inference_run_id is not None and (run is None or run[0] != inference_run_id):
            return FollowDecision(FollowState.SEARCHING, None, None, FollowDecisionReason.SEARCHING)
        items = tuple(observations)
        if not self.status.update_observations(items, inference_run_id=inference_run_id):
            return FollowDecision(FollowState.SEARCHING, None, None, FollowDecisionReason.SEARCHING)
        previous_frame_at = self._last_frame_at
        self._last_frame_at = timestamp if captured_at is None else captured_at
        # The single inference loop publishes readiness after a completed frame.
        armed = bool(self.status.snapshot()["tracking"]["armed"])
        if not armed:
            if self._was_armed:
                self.machine.reset()
                self.planner.reset()
                self.tilt_planner.reset()
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
            self.tilt_planner.reset()
        self._was_armed = True
        decision = self.machine.process(timestamp, items)
        self._decision = decision
        self._decision_run_id = run[0] if run is not None else inference_run_id
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
    def update_locked_position(self, run_id, track_id, box):
        # Position alone never qualifies a ride or extends its detection deadline.
        self._consume_reset()
        run = self.status.inference_run()
        decision = self._decision
        if (run is None or run[0] != run_id or self._decision_run_id != run_id
                or decision is None or decision.target is None
                or decision.target_track_id != track_id or not self.status.snapshot()['tracking']['armed']):
            return
        if box is None:
            self._decision = None
            if self._pause_output is not None:
                self._pause_output()
            return
        target = decision.target
        target = replace(target, detection=replace(target.detection, box=box))
        self._decision = replace(decision, target=target)
        self.status.update_target(mode=decision.state.value, track_id=track_id,
            target_x=box.center_x, chasing_wave=target.action.chasing_wave,
            takeoff=target.action.takeoff, surfing=target.action.surfing,
            reason=decision.reason.value)

    @_serialized
    def check_freshness(self, *, now: float | None = None) -> bool:
        timestamp = self.clock() if now is None else now
        if self.status.inference_run() is None or self._last_frame_at is None:
            return False
        age = max(0.0, timestamp - self._last_frame_at)
        self.status.update_metrics(inference_frame_age_ms=round(age * 1000))
        if age > self.frame_stale_seconds:
            self.status.update_component("inference", "stale", "推論影格逾時")
            self.machine.reset()
            self.planner.reset()
            self.tilt_planner.reset()
            self._was_armed = False
            self._decision = None
            self.zoom_planner.reset()
            return False
        return True

    @_serialized
    def control_step(self, *, now=None):
        timestamp = self.clock() if now is None else now
        self._consume_reset()
        self.keyboard.expire()
        if not self.status.snapshot()['tracking']['armed']:
            self.planner.reset()
            self.tilt_planner.reset()
            self._decision = None
            self._was_armed = False
            return
        if not self.status.snapshot()['inference']['enabled']:
            self._decision = None
            self.current_angle_deg, self.current_tilt_deg = self.keyboard.step(
                self.current_angle_deg, self.current_tilt_deg)
            self.status.update_metrics(servo_angle_deg=self.current_angle_deg, tilt_angle_deg=self.current_tilt_deg)
            return
        if self._decision is None or not self.check_freshness(now=timestamp):
            return
        run = self.status.inference_run()
        if self._decision_run_id is not None and (run is None or run[0] != self._decision_run_id):
            self._decision = None
            return
        if self._pelco:
            if run is None:
                return
            pan = self.planner.step(self._decision, armed=True)
            tilt = self.tilt_planner.step(self._decision, armed=True) if self.tilt_enabled else 0
            try:
                self.command_velocity(pan, tilt, run[0])
            except Exception:
                self.status.update_component('servo', 'error', 'RS485 方向／速度傳送失敗')
                raise
            return
        angle = self.planner.step(now=timestamp, current_angle_deg=self.current_angle_deg,
                                  decision=self._decision, armed=True)
        tilt = None
        if self.tilt_enabled:
            tilt = self.tilt_planner.step(now=timestamp, current_angle_deg=self.current_tilt_deg,
                                          decision=self._decision, armed=True)
        try:
            if self.command_axes is not None:
                self.command_axes(angle, tilt)
            else:
                self.command_target(angle)
        except Exception:
            self.status.update_component('servo', 'error', '目標角度傳送失敗')
            raise
        self.current_angle_deg = angle
        if tilt is not None:
            self.current_tilt_deg = tilt
            self.status.update_metrics(tilt_angle_deg=tilt)
        self.status.update_metrics(servo_angle_deg=angle)

    @_serialized
    def set_inference_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('AI 辨識設定必須為開啟或關閉')
        if self.status.snapshot()['inference']['enabled'] == enabled:
            return
        self.keyboard.stop()
        self.status.set_inference_enabled(enabled)
        if not enabled and self.keyboard.config.protocol != 'm0_pulse':
            self.status.disarm('AI 辨識已關閉')  # Preserve historical non-PWM STOP semantics.
        self._tracking_reset.set()
        self._consume_reset()
        self._last_frame_at = None
        self.zoom_planner.reset()

    @_serialized
    def keyboard_snapshot(self):
        return self.keyboard.snapshot()

    @_serialized
    def keyboard_command(self, action, values):
        return self.keyboard.command(action, values)

    def start(self):
        with self._lock:
            if self._control_thread is not None: return
            self._control_stop.clear()
            self._control_thread = threading.Thread(target=self._control_loop,
                name='pan-control', daemon=True)
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
        self.tilt_planner.reset()

    def _control_loop(self):
        while not self._control_stop.wait(self._control_period):
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
