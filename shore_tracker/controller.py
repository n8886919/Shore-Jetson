from __future__ import annotations

import threading
from collections.abc import Callable

from shore_tracker.status import StatusStore


class AppController:
    """Narrow UI control surface. Hardware adapters attach here later."""

    def __init__(
        self,
        status: StatusStore,
        *,
        arm_callback: Callable[[], tuple[bool, str]] | None = None,
        stop_callback: Callable[[], None] | None = None,
        tuning_callback: Callable[..., None] | None = None,
        camera_controls=None,
        recording=None,
        diagnostics_callback=None,
        camera_reconnect_callback=None,
        servo_recovery_callback=None,
        inference_rate_callback=None,
        pan_tilt_snapshot=None,
        pan_tilt_callback=None,
        servo_test_snapshot=None,
        servo_test_callback=None,
    ) -> None:
        self.status = status
        self._arm_callback = arm_callback
        self._stop_callback = stop_callback
        self._tuning_callback = tuning_callback
        self._camera_controls = camera_controls
        self.recording = recording
        self._diagnostics_callback = diagnostics_callback
        self._camera_reconnect_callback = camera_reconnect_callback
        self._servo_recovery_callback = servo_recovery_callback
        self._servo_recovery_epoch = 0
        self._inference_rate_callback = inference_rate_callback
        self._pan_tilt_snapshot = pan_tilt_snapshot
        self._pan_tilt_callback = pan_tilt_callback
        self._servo_test_snapshot = servo_test_snapshot
        self._servo_test_callback = servo_test_callback
        self._control_lock = threading.RLock()

    def arm(self) -> tuple[bool, str]:
        recovery_epoch = self._servo_recovery_epoch
        with self._control_lock:
            if recovery_epoch != self._servo_recovery_epoch:
                return False, "舵機連線狀態已改變，請重新開始追蹤"
            if self.servo_test_snapshot().get('connected'):
                return False, '請先停止雲台測試並等待串口恢復'
            if self.pan_tilt_snapshot().get("connected"):
                return False, "請先停用並斷開雲台手動控制"
            if self._camera_recovery_pending():
                return False, "請等待相機重新連接完成"
            if self._arm_callback is None:
                return self.status.arm()
            accepted, detail = self.status.request_arm()
            if not accepted:
                return False, detail
            if not self.status.arm_request_active():
                self._deliver_stop()
                return False, "ARM 已被安全狀態取消"
            try:
                accepted, detail = self._arm_callback()
            except Exception:
                self.status.disarm("UART 傳送啟用失敗")
                return False, "UART 傳送啟用失敗"
            if not accepted:
                self.status.disarm(detail)
                return False, detail
            tracking = self.status.snapshot()["tracking"]
            if not (tracking["armed"] or tracking["mode"] == "arming"):
                # A component fault raced the non-blocking hardware request.
                # STOP is delivered before releasing the same control lock, so
                # a queued fail-safe callback can never leave SET enabled.
                # A synchronous callback may already have confirmed ARM; that
                # transition is successful and must not be mistaken for STOP.
                self._deliver_stop()
                return False, "ARM 已被安全狀態取消"
            return True, detail

    def stop(self) -> tuple[bool, str]:
        with self._control_lock:
            self.status.disarm("manual STOP")
            if not self._deliver_stop():
                return False, "local DISARM complete; UART send shutdown failed"
            return True, "disarmed"

    def fail_safe_stop(self, _reason: str = "") -> None:
        """Serialize component-fault STOP behind any in-flight ARM request."""

        with self._control_lock:
            self._deliver_stop()

    def tune(
        self,
        *,
        pan_gain_deg: float,
        pan_deadband: float,
        pan_direction: int,
        zoom_maximum: float,
        tilt_enabled: bool | None = None,
    ) -> tuple[bool, str]:
        if self._tuning_callback is None:
            return False, "tuning unavailable"
        snapshot = self.status.snapshot()
        if (
            snapshot["tracking"]["armed"]
            and pan_direction != snapshot["tuning"]["pan_direction"]
        ):
            return False, "切換 Pan 方向前請先 STOP"
        try:
            extra = {} if tilt_enabled is None else {"tilt_enabled": tilt_enabled}
            self._tuning_callback(
                pan_gain_deg=pan_gain_deg,
                pan_deadband=pan_deadband,
                pan_direction=pan_direction,
                zoom_maximum=zoom_maximum,
                **extra,
            )
        except (TypeError, ValueError) as exc:
            return False, str(exc)
        return True, "updated"

    def _deliver_stop(self) -> bool:
        if self._stop_callback is None:
            return True
        try:
            self._stop_callback()
        except Exception:
            return False
        return True

    def pan_tilt_snapshot(self) -> dict:
        snapshot = self._pan_tilt_snapshot() if self._pan_tilt_snapshot else {"available": False}
        tracking = self.status.snapshot()["tracking"]
        return {**snapshot, "tracking_active": tracking["armed"] or tracking["mode"] == "arming"}

    def pan_tilt_command(self, action: str, values: dict) -> dict:
        with self._control_lock:
            if self._pan_tilt_callback is None:
                raise ValueError("雲台手動控制尚未連接")
            tracking = self.status.snapshot()["tracking"]
            active = tracking["armed"] or tracking["mode"] == "arming"
            if active and action not in {"stop", "disarm"}:
                raise ValueError("請先停止自動跟拍，再使用雲台手動控制")
            if active:
                self.status.disarm("manual pan-tilt STOP")
                if not self._deliver_stop():
                    raise OSError("停止指令未確認，請檢查雲台")
            result = self._pan_tilt_callback(action, values)
            return {"ok": True, "status": self.pan_tilt_snapshot(), **(result or {})}

    def camera_snapshot(self) -> dict:
        if self._camera_controls is None:
            return {"ok": False, "detail": "相機控制尚未就緒"}
        try:
            return {"ok": True, **self._camera_controls.snapshot()}
        except Exception:
            return {"ok": False, "detail": "無法讀取相機設定，請確認相機連線"}

    def servo_test_snapshot(self):
        snapshot = self._servo_test_snapshot() if self._servo_test_snapshot else {'available': False}
        tracking = self.status.snapshot()['tracking']
        return {**snapshot, 'tracking_active': tracking['armed'] or tracking['mode'] == 'arming'}

    def servo_test_command(self, action, values):
        with self._control_lock:
            if self._servo_test_callback is None:
                raise ValueError('雲台測試尚未連接')
            if action == 'start':
                tracking = self.status.snapshot()['tracking']
                if tracking['armed'] or tracking['mode'] == 'arming':
                    raise ValueError('請先停止自動跟拍')
                recording = self.recording.snapshot() if self.recording else {}
                if (recording.get('state', 'idle') != 'idle'
                        or recording.get('manual_requested') or recording.get('tracking_requested')):
                    raise ValueError('請先結束錄影並等待收尾')
                if self._camera_recovery_pending():
                    raise ValueError('請等待相機重新連接完成')
            if action in {'start', 'stop'}:
                self._servo_recovery_epoch += 1
            result = self._servo_test_callback(action, values)
            return {'ok': True, 'status': self.servo_test_snapshot(), **(result or {})}

    def camera_command(self, action: str, payload: dict) -> tuple[bool, str]:
        with self._control_lock:
            if self._camera_controls is None:
                return False, "相機控制尚未就緒"
            tracking = self.status.snapshot()["tracking"]
            if action != "stop" and (tracking["armed"] or tracking["mode"] == "arming"):
                return False, "請先 STOP 追蹤，再調整相機"
            try:
                self._camera_controls.command(action, payload)
            except ValueError:
                return False, "設定超出相機支援範圍，或指令無效"
            except Exception:
                return False, "相機操作未完成，請確認連線與鏡頭狀態"
            return True, "已送出調整並停止鏡頭" if action in {"zoom_in", "zoom_out", "focus_near", "focus_far"} else "已完成"

    def recording_command(self, action):
        with self._control_lock:
            return self._recording_command(action)

    def set_recording_mode(self, continuous):
        with self._control_lock:
            if self.recording is None:
                return False, '錄影尚未就緒'
            return self.recording.set_continuous(continuous)

    def _recording_command(self, action):
        if action == "start" and self._camera_recovery_pending():
            return False, "請等待相機重新連接完成"
        if self.recording is None:
            return False, "錄影尚未就緒"
        if action == "start" and self.status.snapshot()["components"]["camera"]["state"] != "ready":
            return False, "請等待相機連線"
        try:
            return self.recording.command(action)
        except Exception:
            return False, "無法開始錄影，請確認儲存空間"

    def diagnostics(self) -> dict:
        if self._diagnostics_callback is None:
            return {"available": False}
        return self._diagnostics_callback()

    def set_inference_rate(self, value: float) -> tuple[bool, str]:
        with self._control_lock:
            if self._inference_rate_callback is None:
                return False, "辨識服務尚未就緒"
            try:
                self._inference_rate_callback(value)
            except ValueError as exc:
                return False, str(exc)
            return True, "辨識頻率已生效"

    def _camera_recovery_pending(self) -> bool:
        return self.diagnostics().get("recovery", {}).get("state") in {"queued", "reconnecting"}

    def recover_servo(self) -> None:
        """Serialize the UART health check/reopen with ARM, STOP and port handoff."""
        with self._control_lock:
            if self._servo_recovery_callback is not None and self._servo_recovery_callback():
                self._servo_recovery_epoch += 1

    def reconnect_camera(self) -> tuple[bool, str]:
        with self._control_lock:
            if self._camera_reconnect_callback is None:
                return False, "此頁面沒有連接實機"
            tracking = self.status.snapshot()["tracking"]
            if tracking["armed"] or tracking["mode"] == "arming":
                return False, "請先停止追蹤，再重新連接相機"
            recording = self.recording.snapshot() if self.recording else {}
            if (recording.get("state", "idle") != "idle"
                    or recording.get("manual_requested")
                    or recording.get("tracking_requested")):
                return False, "請先結束錄影，並等待影片處理完成"
            return self._camera_reconnect_callback()
