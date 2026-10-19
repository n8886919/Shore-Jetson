from __future__ import annotations

from shore_tracker.config import ServoConfig
from shore_tracker.status import StatusStore

from .simple import SimpleServoClient, serial_client


class ServoService:
    def __init__(
        self,
        client: SimpleServoClient,
        status: StatusStore,
    ) -> None:
        self.client = client
        self.status = status
        self.client.on_error = self._transport_failed

    def start(self) -> None:
        self.client.start()
        detail = ("M0 UART 已開啟（9600，GXServo 180°）；無 ACK／實際角度回授"
                  if self.client.protocol == "m0_pulse" else
                  "舊版 Shore-Spotter UART 已開啟；控制板模式與實際角度未回讀")
        self.status.update_component(
            "servo", "ready", detail
        )

    def _transport_failed(self, reason: str) -> None:
        detail = ("UART 目標更新逾時，已停送；請重啟服務"
                  if reason == "target_expired" else "UART 寫入失敗，請重啟服務")
        self.status.update_component("servo", "error", detail)

    def request_arm(self) -> tuple[bool, str]:
        try:
            self.client.arm()
        except Exception:
            self.status.update_component("servo", "error", "UART 傳送無法啟用")
            return False, "UART 傳送無法啟用"
        self.status.confirm_armed("manual ARM")
        return True, "armed"

    def set_target_degrees(self, degrees: float) -> None:
        self.client.set_target_degrees(degrees)

    def stop_motion(self, _reason: str = "") -> None:
        self.client.stop_motion()

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
        raise ValueError("Shore-Spotter requires UART; set servo.transport to serial")
    common = dict(
        angle_min_deg=config.angle_min_deg,
        angle_max_deg=config.angle_max_deg,
        send_hz=config.send_hz,
        protocol=config.protocol,
    )
    client = serial_client(
        device=config.serial_device,
        baud=config.serial_baud,
        **common,
    )
    return ServoService(client, status)
