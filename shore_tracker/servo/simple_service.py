from __future__ import annotations

from shore_tracker.config import PWM_MANUAL_TILT_LIMITS_DEG, ServoConfig
from shore_tracker.status import StatusStore

from .simple import SimpleServoClient, serial_client
from .pelco_client import PelcoServoClient, pelco_serial_client


class ServoService:
    def __init__(
        self,
        client: SimpleServoClient | PelcoServoClient,
        status: StatusStore,
    ) -> None:
        self.client = client
        self.status = status
        self.client.on_error = self._transport_failed

    def is_connected(self) -> bool:
        if not self.client.snapshot().ready:
            return False
        check = getattr(self.client.writer, "is_connected", None)
        return check() if check is not None else True

    def start(self) -> None:
        self.client.start()
        if self.client.protocol == "pelco_d":
            self.status.update_servo_command(protocol="pelco_d", pan=None, tilt=None)
            self.status.update_component("servo", "ready", "RS485 串口已開啟（Pelco-D 9600）；無位置／限位／碰撞回授")
            return
        detail = ("Servo UART 已開啟（9600，GXServo 180°）；無 ACK／實際角度回授"
                  if self.client.protocol == "m0_pulse" else
                  "舊版 Shore-Spotter UART 已開啟；控制板模式與實際角度未回讀")
        self.status.update_component(
            "servo", "ready", detail
        )

    def _transport_failed(self, reason: str) -> None:
        if self.client.protocol == "pelco_d":
            self.status.update_servo_command(protocol="pelco_d", pan=None, tilt=None)
            detail = "RS485 控制更新逾時" if reason == "target_expired" else "RS485 寫入失敗"
            self.status.update_component("servo", "error", detail + "；已嘗試 STOP，請重啟服務")
            return
        detail = ("UART 目標更新逾時，已停送"
                  if reason == "target_expired" else "UART 寫入失敗")
        detail += ("；等待自動恢復，恢復後請重新開始追蹤" if self.client.protocol == "m0_pulse"
                   else "；請重啟服務")
        self.status.update_component("servo", "error", detail)

    def request_arm(self) -> tuple[bool, str]:
        try:
            if self.client.protocol == "pelco_d":
                self.status.confirm_armed("manual ARM")
                run = self.status.inference_run()
                if run is None:
                    return False, "ARM 已取消"
                self.client.arm(run_id=run[0])
                if self.status.inference_run() != run:
                    self.client.stop_motion()
                    return False, "ARM 已取消"
            else:
                self.client.arm()
        except Exception:
            self.status.update_component("servo", "error", "UART 傳送無法啟用")
            return False, "UART 傳送無法啟用"
        self.status.confirm_armed("manual ARM")
        return True, "armed"

    def set_target_degrees(self, degrees: float) -> None:
        if self.client.protocol == "pelco_d":
            raise ValueError("Pelco-D requires direction/speed, not angle targets")
        self.client.set_target_degrees(degrees)

    def set_axes(self, degrees: float, tilt_degrees: float | None) -> None:
        if self.client.protocol == "pelco_d":
            raise ValueError("Pelco-D requires direction/speed, not angle targets")
        self.client.set_target_degrees(degrees, tilt_degrees=tilt_degrees)

    def set_velocity(self, pan: int, tilt: int, run_id: int) -> None:
        if self.client.protocol != "pelco_d":
            raise ValueError("Velocity commands require pelco_d")
        if self.client.set_velocity(pan, tilt, run_id=run_id):
            self.status.update_servo_command(protocol="pelco_d", pan=pan, tilt=tilt, run_id=run_id)

    def disable_tilt(self) -> None:
        self.client.disable_tilt()
        if self.client.protocol == "pelco_d":
            self.status.update_servo_command(protocol="pelco_d", pan=0, tilt=0)

    def pause_motion(self) -> None:
        if self.client.protocol == 'm0_pulse':
            self.client.pause_motion()
        else:
            self.stop_motion()

    def stop_motion(self, _reason: str = "") -> None:
        self.client.stop_motion()
        if self.client.protocol == "pelco_d":
            self.status.update_servo_command(protocol="pelco_d", pan=0, tilt=0)

    def close(self, *, timeout: float = 1.0) -> bool:
        stopped = self.client.close(timeout=timeout)
        self.status.update_component(
            "servo", "offline" if stopped else "error", "service stopped"
        )
        return stopped


def build_servo_service(
    config: ServoConfig,
    status: StatusStore,
) -> ServoService:
    if not config.enabled:
        raise ValueError("Servo is disabled")
    if config.pan_direction == 0:
        raise ValueError("Servo pan direction is not calibrated")
    if config.transport != "serial":
        raise ValueError("Direct controller requires UART; set servo.transport to serial")
    if config.protocol == "pelco_d":
        return ServoService(pelco_serial_client(config), status)
    tilt_min, tilt_max = config.tilt_min_deg, config.tilt_max_deg
    if config.protocol == 'm0_pulse':
        # One writer accepts both the automatic planner and leased manual tilt.
        manual_min, manual_max = PWM_MANUAL_TILT_LIMITS_DEG
        tilt_min, tilt_max = min(tilt_min, manual_min), max(tilt_max, manual_max)
    common = dict(
        angle_min_deg=config.angle_min_deg,
        angle_max_deg=config.angle_max_deg,
        send_hz=config.send_hz,
        protocol=config.protocol,
        tilt_min_deg=tilt_min,
        tilt_max_deg=tilt_max,
        pan_channel=config.pan_channel,
        tilt_channel=config.tilt_channel,
    )
    client = serial_client(
        device=config.serial_device,
        baud=config.serial_baud,
        **common,
    )
    return ServoService(client, status)
