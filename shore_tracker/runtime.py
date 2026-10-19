from __future__ import annotations

import ipaddress
import math
import signal
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shore_tracker.camera import CameraPipeline, ClosedFragment, RtspUri, build_pipeline_spec
from shore_tracker.camera.control import CameraControls
from shore_tracker.config import AppConfig, validate_config
from shore_tracker.controller import AppController
from shore_tracker.manual_recording import ManualRecording
from shore_tracker.recording_triggers import RecordingTriggers
from shore_tracker.coordinator import TrackingCoordinator
from shore_tracker.inference.tensorrt_runtime import TensorRtSurfTrackModels
from shore_tracker.inference.worker import InferenceWorker
from shore_tracker.network import interface_ipv4, route_ipv4
from shore_tracker.preview import PreviewStore
from shore_tracker.servo.simple_service import ServoService, build_servo_service
from shore_tracker.status import StatusStore
from shore_tracker.storage import DiskGuard
from shore_tracker.storage_service import StorageMonitor
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


def recording_pattern_for_session(
    root: str | Path,
    started_at: datetime,
    session_id: str,
) -> Path:
    if started_at.tzinfo is None:
        raise ValueError("started_at must be timezone-aware")
    if not session_id.isalnum() or not 1 <= len(session_id) <= 32:
        raise ValueError("session_id must be 1-32 alphanumeric characters")
    local = started_at.astimezone()
    return (
        Path(root).expanduser().absolute()
        / local.strftime("%Y-%m-%d")
        / f"{local.strftime('%Y%m%dT%H%M%S')}-{session_id}-%05d.mkv"
    )


class ShoreTrackerRuntime:
    """POC runtime: camera, MKV, native TensorRT, pan Servo, and kiosk UI."""

    def __init__(
        self,
        config: AppConfig,
        *,
        dashboard_builder: Callable[..., DashboardServer] = make_server,
        camera_builder: Callable[..., CameraPipeline] = CameraPipeline,
        servo_service_builder: Callable[[Any, StatusStore], ServoService] = build_servo_service,
        inference_worker_builder: Callable[..., InferenceWorker] = InferenceWorker,
        camera_route_validator: Callable[[RtspUri, str], None] = validate_direct_camera_route,
        session_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex[:12],
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        camera_retry_seconds: float = CAMERA_RETRY_SECONDS,
        camera_stall_seconds: float = CAMERA_STALL_SECONDS,
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
        self._camera_builder = camera_builder
        self._servo_service_builder = servo_service_builder
        self._inference_worker_builder = inference_worker_builder
        self._camera_route_validator = camera_route_validator
        self._session_id_factory = session_id_factory
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
        )
        self.controller = AppController(
            self.status,
            arm_callback=self._request_servo_arm,
            stop_callback=self._stop_servo,
            tuning_callback=self._coordinator.update_tuning,
            camera_controls=CameraControls(self._camera_control_host),
            recording=self.manual_recording,
            diagnostics_callback=self.diagnostics,
            camera_reconnect_callback=self._request_camera_reconnect,
            inference_rate_callback=self._set_inference_rate,
        )
        self.status.add_disarm_listener(self.controller.fail_safe_stop)
        self.status.add_disarm_listener(self.manual_recording.cancel_tracking)
        self.status.add_disarm_listener(self._coordinator.cancel_tracking)
        self.server = dashboard_builder(
            (config.web.host, config.web.port),
            self.status,
            self.controller,
            self.preview,
            preview_stale_seconds=config.web.preview_stale_seconds,
        )

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
            self._start_camera()
            self._start_inference()
            self._coordinator.start()
            self._started = True
            self._start_supervisor()

    def stop_services(self) -> None:
        with self._lifecycle_lock:
            if not self._started and not any((self._camera, self._servo, self._inference, self._storage)):
                return
            self._started = False
            self.status.disarm("service stopped")
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
        try:
            self._servo = self._servo_service_builder(self.config.servo, self.status)
            self._servo.start()
        except Exception:
            self._servo = None
            self.status.update_component("servo", "error", "transport start failed")

    def _start_camera(self) -> None:
        with self._camera_lock:
            if self._camera is not None:
                return
            try:
                started_at = self._utcnow()
                rtsp_uri = RtspUri.from_environment(self.config.camera.rtsp_uri_env)
                self._camera_route_validator(rtsp_uri, self.config.camera.interface)
                pattern = recording_pattern_for_session(
                    self.config.recording.root,
                    started_at,
                    self._session_id_factory(),
                )
                pattern.parent.mkdir(parents=True, exist_ok=True)
                spec = build_pipeline_spec(
                    self.config.camera.codec,
                    pattern,
                    segment_seconds=self.config.recording.segment_seconds,
                    rtsp_latency_ms=self.config.camera.latency_ms,
                    rotation_degrees=self.config.camera.rotation_degrees,
                    preview_fps=self.config.camera.preview_fps,
                )
                camera = self._camera_builder(
                    status=self.status,
                    preview=self.preview.begin_session(),
                    rtsp_uri=rtsp_uri,
                    spec=spec,
                    on_fragment_closed=self._on_fragment_closed,
                )
                if camera.start():
                    self._camera = camera
                else:
                    if not self._release_camera(camera, 2.0):
                        self._camera = camera
                    self.status.update_component("camera", "offline", "waiting for camera")
            except Exception:
                self.status.update_component("camera", "offline", "camera start failed")
                self.status.update_component("recording", "paused", "camera unavailable")

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
            preview=self.preview,
            status=self.status,
            coordinator=self._coordinator,
            models=models,
            stale_seconds=self.config.tracking.frame_stale_seconds,
            max_fps=self.config.inference.max_fps,
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
                "protocol", "serial_device", "serial_baud", "angle_min_deg",
                "angle_max_deg", "home_deg")},
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
        with self._recovery_lock:
            self._recovery["previous_recording_finalized"] = bool(finalized)
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
            recording_age = getattr(camera, "recording_progress_age_seconds", None)
            if callable(recording_age):
                recording_age = recording_age()
            if (
                camera is not None and not manual and terminal is None
                and (age is None or age <= self._camera_stall_seconds)
                and (
                    recording_age is None
                    or recording_age <= self._camera_stall_seconds
                )
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

    def _on_fragment_closed(self, fragment: ClosedFragment) -> None:
        self.status.update_metrics(last_recording=fragment.path.name)
        self.status.update_component("recording", "ready", "MKV segment closed")

    def _request_servo_arm(self) -> tuple[bool, str]:
        if self._servo is None:
            return False, "Servo unavailable"
        return self._servo.request_arm()

    def _command_servo(self, degrees: float) -> None:
        if self._servo is not None:
            self._servo.set_target_degrees(degrees)

    def _stop_servo(self, reason: str = "") -> None:
        if self._servo is not None:
            self._servo.stop_motion(reason)

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
