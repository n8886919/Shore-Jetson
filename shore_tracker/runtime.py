from __future__ import annotations

import ipaddress
import math
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from shore_tracker.camera import CameraPipeline, RtspUri, build_pipeline_spec
from shore_tracker.camera.control import CameraControls
from shore_tracker.config import AppConfig, PWM_MANUAL_TILT_LIMITS_DEG, validate_config
from shore_tracker.controller import AppController
from shore_tracker.manual_recording import ManualRecording
from shore_tracker.recording_triggers import RecordingTriggers
from shore_tracker.coordinator import TrackingCoordinator
from shore_tracker.inference.tensorrt_runtime import TensorRtSurfTrackModels
from shore_tracker.inference.worker import InferenceWorker
from shore_tracker.network import interface_ipv4, route_ipv4
from shore_tracker.native_frames import NativeFrames
from shore_tracker.preview import PreviewStore
from shore_tracker.inference.frames import DecodedFrames
from shore_tracker.camera.pipeline import GStreamerPipelineFactory
from shore_tracker.servo.simple_service import ServoService, build_servo_service
from shore_tracker.servo.pelco_d import PelcoManualController
from shore_tracker.servo.pwm_test import PwmServoTest
from shore_tracker.status import StatusStore
from shore_tracker.storage import DiskGuard
from shore_tracker.storage_service import StorageMonitor
from shore_tracker.web.hevc import HevcPreview
from shore_tracker.web.server import DashboardServer, make_server


CAMERA_RETRY_SECONDS = 2.0
CAMERA_STALL_SECONDS = 10.0


def validate_direct_camera_route(rtsp_uri: RtspUri, interface: str) -> None:
    destination = ipaddress.IPv4Address(rtsp_uri.destination_ipv4())
    local = interface_ipv4(interface)
    subnet = ipaddress.IPv4Network(
        f"{local.address}/{local.prefix_length}", strict=False
    )
    route = route_ipv4(str(destination))
    if (
        route.interface != interface
        or route.source != local.address
        or route.gateway is not None
        or destination not in subnet
        or destination in {subnet.network_address, subnet.broadcast_address}
    ):
        raise RuntimeError("camera route is not confined to its dedicated interface")




class ShoreTrackerRuntime:
    """Preview, requested event recording, TensorRT, servos, and kiosk UI."""

    def __init__(
        self,
        config: AppConfig,
        *,
        dashboard_builder: Callable[..., DashboardServer] = make_server,
        camera_builder: Callable[..., CameraPipeline] = CameraPipeline,
        servo_service_builder: Callable[[Any, StatusStore], ServoService] = build_servo_service,
        inference_worker_builder: Callable[..., InferenceWorker] = InferenceWorker,
        camera_route_validator: Callable[[RtspUri, str], None] = validate_direct_camera_route,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        camera_retry_seconds: float = CAMERA_RETRY_SECONDS,
        camera_stall_seconds: float = CAMERA_STALL_SECONDS,
        pan_tilt_builder=PelcoManualController,
        servo_test_builder=PwmServoTest,
    ) -> None:
        validate_config(config)
        if not math.isfinite(camera_retry_seconds) or camera_retry_seconds <= 0:
            raise ValueError("camera_retry_seconds must be positive")
        if not math.isfinite(camera_stall_seconds) or camera_stall_seconds <= 0:
            raise ValueError("camera_stall_seconds must be positive")
        self.config = config
        self.status = StatusStore()
        self.status.configure_recognition(detection=config.inference.score_threshold)
        self.preview = PreviewStore()
        self.native_frames = NativeFrames() if config.camera.native_display else None
        self.webrtc_preview = HevcPreview() if config.web.h264_preview else None
        self.decoded_frames = DecodedFrames()
        self.position_frames = (DecodedFrames() if config.camera.native_display
                                and config.camera.decoded_inference
                                and config.servo.protocol == "m0_pulse" else None)
        self._camera_builder = camera_builder
        self._servo_service_builder = servo_service_builder
        self._inference_worker_builder = inference_worker_builder
        self._camera_route_validator = camera_route_validator
        self._utcnow = utcnow
        self._camera_retry_seconds = camera_retry_seconds
        self._camera_stall_seconds = camera_stall_seconds
        self._camera: CameraPipeline | None = None
        self._servo: ServoService | None = None
        self._inference: InferenceWorker | None = None
        self._storage: StorageMonitor | None = None
        self._camera_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._supervisor_stop = threading.Event()
        self._supervisor: threading.Thread | None = None
        self._started = False
        self._actuator_lock = threading.RLock()
        self._pan_tilt = (pan_tilt_builder(device=config.servo.serial_device,
                          address=config.servo.pelco_address)
                          if config.servo.protocol == "pelco_d" else None)
        self._pan_tilt_stop = threading.Event()
        self._pan_tilt_thread = None
        self._servo_test = (servo_test_builder(service_factory=self._build_servo_test_service,
                            pan_direction=config.servo.pan_direction,
                            tilt_direction=config.servo.tilt_direction)
                            if config.servo.protocol == 'm0_pulse' else None)
        self._recovery_lock = threading.Lock()
        self._reconnect_requested = threading.Event()
        self._recovery = {"state": "waiting", "attempts": 0,
                          "last_reason": None, "last_attempt_at": None,
                          "previous_recording_finalized": None}
        self._coordinator = TrackingCoordinator(
            self.config.tracking,
            self.config.servo,
            self.status,
            self._command_servo,
            self.config.zoom,
            on_decision=self._on_follow_decision,
            command_axes=self._command_axes,
            disable_tilt=self._disable_tilt,
            command_velocity=self._command_velocity,
            pause_output=self._pause_servo,
        )
        clip_recorder = ManualRecording(
            config.data_dir / "manual-recordings", self._recording_uri,
            config.camera.codec, config.camera.rotation_degrees,
            config.recording.free_stop_gib,
        )
        self.manual_recording = RecordingTriggers(
            clip_recorder,
            armed_reader=lambda: self.status.snapshot()["tracking"]["armed"],
            stale_seconds=config.tracking.frame_stale_seconds,
            settings_path=config.data_dir / 'recording-settings.json',
        )
        self.status.update_component("recording", "ready", "等待錄影觸發")
        self.controller = AppController(
            self.status,
            arm_callback=self._request_servo_arm,
            stop_callback=self._stop_servo,
            tuning_callback=self._coordinator.update_tuning,
            camera_controls=CameraControls(self._camera_control_host),
            recording=self.manual_recording,
            diagnostics_callback=self.diagnostics,
            camera_reconnect_callback=self._request_camera_reconnect,
            servo_recovery_callback=self._check_servo_once,
            inference_rate_callback=self._set_inference_rate,
            inference_enabled_callback=self._coordinator.set_inference_enabled,
            keyboard_snapshot=self._coordinator.keyboard_snapshot,
            keyboard_callback=self._coordinator.keyboard_command,
            pan_tilt_snapshot=self._pan_tilt_snapshot,
            pan_tilt_callback=self._pan_tilt_command,
            servo_test_snapshot=self._servo_test_snapshot,
            servo_test_callback=self._servo_test_command,
        )
        self.status.add_disarm_listener(self.controller.fail_safe_stop)
        self.status.add_disarm_listener(self.manual_recording.cancel_tracking)
        self.status.add_disarm_listener(self._coordinator.cancel_tracking)
        dashboard_options = {"allow_lan": True} if config.web.allow_lan else {}
        self.server = dashboard_builder(
            (config.web.host, config.web.port),
            self.status,
            self.controller,
            self.preview,
            preview_stale_seconds=config.web.preview_stale_seconds,
            **dashboard_options,
        )

        self.server.local_display = self.native_frames
        self.server.webrtc_preview = self.webrtc_preview
        self.server.camera_interface = config.camera.interface

    def _publish_native_frame(self, data, width, height, stride, **metadata):
        self.native_frames.publish(data, width, height, stride, **metadata)
        if self.webrtc_preview is not None:
            self.webrtc_preview.publish(data, width, height, stride, **metadata)

    def _on_follow_decision(self, decision, now):
        self.manual_recording.follow(decision, now)

    def _recording_uri(self) -> RtspUri:
        uri = RtspUri.from_environment(self.config.camera.rtsp_uri_env)
        self._camera_route_validator(uri, self.config.camera.interface)
        return uri

    def _camera_control_host(self) -> str:
        uri = RtspUri.from_environment(self.config.camera.rtsp_uri_env)
        self._camera_route_validator(uri, self.config.camera.interface)
        return uri.destination_ipv4()

    def start_services(self) -> None:
        with self._lifecycle_lock:
            if self._started:
                return
            self.config.data_dir.mkdir(parents=True, exist_ok=True)
            self.config.recording.root.mkdir(parents=True, exist_ok=True)
            self.manual_recording.recover()
            self._start_storage()
            self._start_servo()
            if self.native_frames is not None:
                self.native_frames.start()
            self._start_camera()
            self._start_inference()
            self._coordinator.start()
            self._started = True
            if self._pan_tilt is not None or self._servo_test is not None:
                self._pan_tilt_stop.clear()
                self._pan_tilt_thread = threading.Thread(target=self._watch_pan_tilt,
                    name="manual-pan-tilt-watchdog", daemon=True)
                self._pan_tilt_thread.start()
            self._start_supervisor()

    def stop_services(self) -> None:
        with self._lifecycle_lock:
            if not self._started and not any((self._camera, self._servo, self._inference, self._storage)):
                return
            self._started = False
            self.status.disarm("service stopped")
            if self._servo_test is not None:
                self._servo_test.stop('主程式停止，測試已中止')
            self._pan_tilt_stop.set()
            if self._pan_tilt_thread is not None:
                self._pan_tilt_thread.join(timeout=1)
                self._pan_tilt_thread = None
            if self._servo_test is not None:
                with self._actuator_lock:
                    try:
                        self._servo_test.close()
                    except OSError:
                        self.status.update_component('servo', 'error', '測試串口關閉失敗')
            if self._pan_tilt is not None:
                with self._actuator_lock:
                    try:
                        self._pan_tilt.disconnect()
                    except OSError:
                        self.status.update_component("servo", "error", "雲台停止指令未確認")
            self._coordinator.close()
            self.manual_recording.close()
            self._stop_supervisor()
            if self._inference is not None and self._inference.close():
                self._inference = None
            if self._storage is not None and self._storage.close():
                self._storage = None
            with self._camera_lock:
                camera = self._camera
            if camera is not None and self._release_camera(camera, 30.0):
                with self._camera_lock:
                    if self._camera is camera:
                        self._camera = None
            if self._servo is not None and self._servo.close():
                self._servo = None

    def run(self) -> None:
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self._signal_stop)
            signal.signal(signal.SIGTERM, self._signal_stop)
        try:
            self.start_services()
            self.server.serve_forever(poll_interval=0.25)
        finally:
            self.stop_services()
            self.server.server_close()
            if self.webrtc_preview is not None:
                self.webrtc_preview.close()
            if self.native_frames is not None:
                self.native_frames.close()

    def _start_storage(self) -> None:
        self._storage = StorageMonitor(
            path=self.config.recording.root,
            guard=DiskGuard(
                self.config.recording.free_warning_gib,
                self.config.recording.free_stop_gib,
            ),
            status=self.status,
            on_stop=self._stop_camera_for_storage,
        )
        self._storage.start()

    def _start_servo(self) -> None:
        if not self.config.servo.enabled:
            self.status.update_component("servo", "offline", "disabled")
            return
        if self._servo is not None:
            return  # A previous writer must release its port before replacement.
        try:
            self._servo = self._servo_service_builder(self.config.servo, self.status)
            self._servo.start()
        except Exception:
            if self._servo is not None:
                try:
                    if self._servo.close():
                        self._servo = None
                except Exception:
                    pass  # Retain the owner until a later close succeeds.
            detail = ("舵機串口無法開啟，等待自動恢復"
                      if self.config.servo.protocol == "m0_pulse" else "transport start failed")
            self.status.update_component("servo", "error", detail)

    def _check_servo_once(self) -> bool:
        # Called under the controller lock, before the actuator lock, just like
        # ARM/STOP/manual handoff. Never reopen or auto-arm the Pelco-D path.
        if (not self._started or self._supervisor_stop.is_set()
                or self.config.servo.protocol != "m0_pulse"):
            return False
        with self._actuator_lock:
            if self._servo_test is not None:
                test = self._servo_test.snapshot()
                if test['running']:
                    return False
                if test['connected']:
                    try:
                        released = self._servo_test.close()
                    except Exception:
                        released = False
                    if not released:
                        self.status.update_component('servo', 'error', '測試串口尚未釋放')
                        return True
            if not self.config.servo.enabled:
                return False
            servo = self._servo
            state = self.status.snapshot()["components"]["servo"]["state"]
            if (servo is not None and state == "ready"
                    and getattr(servo, "is_connected", lambda: True)()):
                return False
            self.status.update_component("servo", "error", "舵機串口中斷，正在自動恢復")
            if servo is not None:
                try:
                    released = servo.close()
                except Exception:
                    released = False
                if not released:
                    self.status.update_component("servo", "error", "舵機串口尚未釋放，等待自動恢復")
                    return True
                self._servo = None
            if self._started and not self._supervisor_stop.is_set():
                self._start_servo()  # Fresh client, DISARMED, no cached targets.
            return True

    def _start_camera(self) -> None:
        with self._camera_lock:
            if self._camera is not None:
                return
            try:
                rtsp_uri = RtspUri.from_environment(self.config.camera.rtsp_uri_env)
                self._camera_route_validator(rtsp_uri, self.config.camera.interface)
                spec = build_pipeline_spec(
                    self.config.camera.codec,
                    None,
                    rtsp_latency_ms=self.config.camera.latency_ms,
                    rotation_degrees=self.config.camera.rotation_degrees,
                    preview_fps=self.config.camera.preview_fps,
                    native_display=self.config.camera.native_display,
                    decoded_inference=self.config.camera.decoded_inference,
                    inference_frame_fps=self.config.camera.inference_frame_fps,
                )
                camera_options = {}
                preview_session = self.preview.begin_session()
                if self.config.camera.decoded_inference or self.native_frames is not None:
                    native_sink = self._publish_native_frame if self.native_frames else None
                    if self.position_frames is not None:
                        publish_position = self.position_frames.begin_session(
                            camera_generation=preview_session.generation)

                        def native_sink(data, width, height, stride, **metadata):
                            self._publish_native_frame(data, width, height, stride, **metadata)
                            publish_position(data, width, height, stride, **metadata)
                    camera_options['factory'] = GStreamerPipelineFactory(
                        on_native_frame=native_sink,
                        on_decoded_frame=self.decoded_frames.begin_session(
                            camera_generation=preview_session.generation) if self.config.camera.decoded_inference else None)
                camera = self._camera_builder(
                    status=self.status,
                    preview=preview_session,
                    rtsp_uri=rtsp_uri,
                    spec=spec,
                    on_fragment_closed=lambda _fragment: None,
                    **camera_options,
                )
                if camera.start():
                    self._camera = camera
                else:
                    if not self._release_camera(camera, 2.0):
                        self._camera = camera
                    self.status.update_component("camera", "offline", "waiting for camera")
            except Exception:
                self.status.update_component("camera", "offline", "camera start failed")

    def _start_inference(self) -> None:
        if not self.config.inference.enabled:
            self.status.update_component("inference", "offline", "disabled")
            return
        models = TensorRtSurfTrackModels(
            self.config.inference.detector_model,
            self.config.inference.action_model,
            self.config.inference.engine_dir,
            score_threshold=self.config.inference.score_threshold,
            ride_exit_seconds=self.config.tracking.ride_exit_seconds,
        )
        self._inference = self._inference_worker_builder(
            preview=self.decoded_frames if self.config.camera.decoded_inference else self.preview,
            status=self.status,
            coordinator=self._coordinator,
            models=models,
            stale_seconds=self.config.tracking.frame_stale_seconds,
            max_fps=self.config.inference.max_fps,
            on_demand=True,
            confirmation_frames=self.config.inference.confirmation_frames,
            position_frames=self.position_frames,
        )
        self._inference.start()

    def _set_inference_rate(self, value: float) -> None:
        if self._inference is None:
            raise ValueError("辨識服務尚未就緒")
        self._inference.set_max_fps(value)

    def _start_supervisor(self) -> None:
        self._supervisor_stop.clear()
        self._supervisor = threading.Thread(
            target=self._supervise_camera,
            name="camera-recovery",
            daemon=True,
        )
        self._supervisor.start()

    def _stop_supervisor(self) -> None:
        self._supervisor_stop.set()
        thread = self._supervisor
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if thread is None or not thread.is_alive():
            self._supervisor = None

    def _supervise_camera(self) -> None:
        while not self._supervisor_stop.wait(self._camera_retry_seconds):
            self.controller.recover_servo()
            self._check_camera_once()

    def diagnostics(self) -> dict:
        with self._recovery_lock:
            recovery = dict(self._recovery)
        return {
            "available": True,
            "recovery": {"automatic": True, **recovery},
            "video": {"codec": self.config.camera.codec,
                      "rotation_degrees": self.config.camera.rotation_degrees,
                      "preview_fps": self.config.camera.preview_fps},
            "inference": {"max_fps": getattr(self._inference, "max_fps", self.config.inference.max_fps)},
            "servo": {key: getattr(self.config.servo, key) for key in (
                ("protocol", "serial_device", "serial_baud", "pelco_address",
                 "pelco_pan_speed", "pelco_tilt_speed", "pan_direction", "tilt_direction")
                if self.config.servo.protocol == "pelco_d" else
                ("protocol", "serial_device", "serial_baud", "angle_min_deg",
                 "angle_max_deg", "home_deg"))},
        }

    def _request_camera_reconnect(self) -> tuple[bool, str]:
        if not self._started or self._supervisor_stop.is_set():
            return False, "服務尚未就緒"
        if self.status.snapshot()["metrics"].get("disk_pressure") == "stop":
            return False, "儲存空間不足，請先處理磁碟"
        with self._recovery_lock:
            if self._recovery["state"] in {"queued", "reconnecting"}:
                return False, "相機已在重新連接中"
            self._recovery["state"] = "queued"
            self._reconnect_requested.set()
        # The caller holds the control lock: ARM and recording start cannot
        # slip between acceptance and this readiness gate.
        self.status.update_component("camera", "starting", "reconnect queued")
        return True, "已排入重新連接，請等待影像恢復"

    def _release_camera(self, camera, timeout: float) -> bool:
        try:
            finalized = camera.stop(timeout_seconds=timeout)
        except Exception:
            finalized = False
        return finalized or bool(getattr(camera, "resources_released", False))

    def _check_camera_once(self) -> None:
        if self._supervisor_stop.is_set():
            return
        if self.status.snapshot()["metrics"].get("disk_pressure") == "stop":
            return
        with self._camera_lock:
            camera = self._camera
            manual = self._reconnect_requested.is_set()
            terminal = getattr(camera, "terminal_kind", None)
            age = getattr(camera, "progress_age_seconds", None)
            if callable(age):
                age = age()
            if (
                camera is not None and not manual and terminal is None
                and (age is None or age <= self._camera_stall_seconds)
            ):
                with self._recovery_lock:
                    if self._reconnect_requested.is_set():
                        return
                    self._recovery["state"] = (
                        "ready" if self.status.snapshot()["components"]["camera"]["state"] == "ready"
                        else "waiting"
                    )
                return
            self._reconnect_requested.clear()
            with self._recovery_lock:
                self._recovery.update(
                    state="reconnecting", attempts=self._recovery["attempts"] + 1,
                    last_reason="manual" if manual else "stream_unavailable",
                    last_attempt_at=self._utcnow().isoformat(),
                )
            self.status.disarm("camera recovery")
            if camera is None or self._release_camera(camera, 10.0):
                self._camera = None
                if self._supervisor_stop.is_set():
                    return
                self._start_camera()
                with self._recovery_lock:
                    self._recovery["state"] = "waiting"
            else:
                with self._recovery_lock:
                    self._recovery["state"] = "blocked"


    def _request_servo_arm(self) -> tuple[bool, str]:
        if self._servo is None:
            return False, "Servo unavailable"
        return self._servo.request_arm()

    def _command_servo(self, degrees: float) -> None:
        if self._servo is not None:
            self._servo.set_target_degrees(degrees)

    def _command_axes(self, degrees: float, tilt_degrees: float | None) -> None:
        if self._servo is not None:
            if tilt_degrees is None:
                self._servo.set_target_degrees(degrees)
            else:
                self._servo.set_axes(degrees, tilt_degrees)

    def _disable_tilt(self) -> None:
        servo = self._servo
        if servo is not None:
            servo.disable_tilt()

    def _command_velocity(self, pan: int, tilt: int, run_id: int) -> None:
        # Keep this reference stable across a manual-port handoff. A closed old
        # client rejects the old run instead of dereferencing a replaced service.
        servo = self._servo
        if servo is not None:
            servo.set_velocity(pan, tilt, run_id)

    def _pause_servo(self):
        servo = self._servo
        if servo is not None:
            servo.pause_motion()

    def _stop_servo(self, reason: str = "") -> None:
        with self._actuator_lock:
            if self._servo_test is not None:
                self._servo_test.stop('測試已停止；停止送目標不等於斷電')
            try:
                if self._pan_tilt is not None:
                    self._pan_tilt.stop(disarm=True)
            finally:
                if self._servo is not None:
                    self._servo.stop_motion(reason)

    def _pan_tilt_snapshot(self):
        return ({"available": True, **self._pan_tilt.snapshot()}
                if self._pan_tilt is not None else {"available": False})

    def _build_servo_test_service(self):
        low, high = PWM_MANUAL_TILT_LIMITS_DEG
        cfg = replace(self.config.servo, enabled=True, angle_min_deg=low, angle_max_deg=high,
                      tilt_min_deg=low, tilt_max_deg=high, send_hz=50)
        return self._servo_service_builder(cfg, self.status)

    def _servo_test_snapshot(self):
        if self._servo_test is None:
            return {'available': False}
        cfg = self.config.servo
        calibrated = (cfg.pan_direction in {-1, 1} and cfg.tilt_direction in {-1, 1}
                      and (cfg.pan_channel, cfg.tilt_channel) == (0, 1))
        return {'available': True, 'calibrated': calibrated, **self._servo_test.snapshot()}

    def _servo_test_command(self, action, values):
        with self._actuator_lock:
            test = self._servo_test
            if test is None:
                raise ValueError('目前設定不是雙軸 PWM 舵機')
            if action == 'start':
                if not self._started:
                    raise ValueError('主程式尚未啟動或正在停止')
                if not self._servo_test_snapshot()['calibrated']:
                    raise ValueError('請先設定水平 M0、垂直 M1 並確認兩軸方向')
                snapshot = test.snapshot()
                if type(values.get('epoch')) is not int or values['epoch'] != snapshot['epoch']:
                    raise ValueError('測試請求已過期，請重新操作')
                if snapshot['connected']:
                    raise ValueError('請等待前一次測試釋放串口')
                if self._servo is not None:
                    if not self._servo.close():
                        raise OSError('跟拍串口尚未釋放')
                    self._servo = None
                try:
                    session = test.start(**values)
                except Exception:
                    if test.close() and self._started:
                        self._start_servo()
                    raise
                self.status.update_component('servo', 'manual', '雲台 PWM 測試中')
                return {'session': session}
            if action == 'heartbeat':
                test.renew(**values)
            elif action == 'stop':
                test.stop()
                if not test.close():
                    raise OSError('測試串口尚未釋放')
                if self._started:
                    self._start_servo()
            else:
                raise ValueError('未知測試動作')

    def _pan_tilt_command(self, action, values):
        with self._actuator_lock:
            control = self._pan_tilt
            if control is None:
                raise ValueError("目前設定不是 Pelco-D 雲台")
            if not self._started and action not in {"stop", "disarm", "disconnect"}:
                raise ValueError("主程式尚未啟動或正在停止")
            if action == "connect":
                if self._servo is not None:
                    if not self._servo.close():
                        raise OSError("追蹤串口尚未安全關閉，無法切換手動控制")
                    self._servo = None
                try:
                    control.connect()
                except (OSError, ImportError, ValueError):
                    # Failed manual acquisition must not strand an otherwise
                    # usable automatic service. Reopen it DISARMED only after
                    # confirming manual control did not acquire the port.
                    if not control.snapshot()["connected"] and self._started:
                        self._start_servo()
                    raise
                self.status.update_component("servo", "manual", "雲台由手動控制使用")
            elif action == "disconnect":
                control.disconnect()
                if self._started:
                    self._start_servo()  # Opens DISARMED; never automatically re-arms.
            elif action == "arm":
                return {"session": control.arm(epoch=values.get("epoch"))}
            elif action == "move":
                control.move(**values)
            elif action in {"stop", "disarm"}:
                control.stop(**values, disarm=action == "disarm")
            else:
                raise ValueError("未知雲台控制動作")

    def _watch_pan_tilt(self):
        while not self._pan_tilt_stop.wait(.025):
            try:
                if self._pan_tilt is not None:
                    self._pan_tilt.expire()
                if self._servo_test is not None and self._servo_test.tick():
                    self.controller.recover_servo()
            except OSError:
                pass  # Manual controller latches and exposes the serial fault.

    def _stop_camera_for_storage(self) -> None:
        self.manual_recording.stop_all()
        self.status.disarm("disk stop threshold")
        with self._camera_lock:
            camera = self._camera
        if camera is not None and self._release_camera(camera, 30.0):
            with self._camera_lock:
                if self._camera is camera:
                    self._camera = None

    def _signal_stop(self, _signum: int, _frame: object) -> None:
        threading.Thread(target=self.server.shutdown, daemon=True).start()
