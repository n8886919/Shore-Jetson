"""Pelco-D encoding and bounded manual control for the main settings panel."""
from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable

from .port_guard import require_control_port


DIRECTIONS = {"left": 0x04, "right": 0x02, "up": 0x08, "down": 0x10}
LEASE_SECONDS = 0.4
MAX_HOLD_SECONDS = 2.0


def encode_motion(address: int, direction: str = "stop", speed: int = 8) -> bytes:
    if direction not in (*DIRECTIONS, "stop"):
        raise ValueError("未知方向")
    if type(speed) is not int or not 1 <= speed <= 63:
        raise ValueError("速度必須為 1–63")
    pan = speed if direction == "right" else -speed if direction == "left" else 0
    tilt = speed if direction == "down" else -speed if direction == "up" else 0
    return encode_velocity(address, pan, tilt)


def encode_velocity(address: int, pan: int = 0, tilt: int = 0) -> bytes:
    """Signed protocol levels: positive right/down; zero clears direction bits."""
    if type(address) is not int or not 1 <= address <= 255:
        raise ValueError("地址必須為 1–255")
    if any(type(value) is not int or not -63 <= value <= 63 for value in (pan, tilt)):
        raise ValueError("速度級別必須為 -63–63")
    command = (0x02 if pan > 0 else 0x04 if pan < 0 else 0)
    command |= (0x10 if tilt > 0 else 0x08 if tilt < 0 else 0)
    payload = bytes((address, 0, command, abs(pan), abs(tilt)))
    return b"\xff" + payload + bytes((sum(payload) & 0xFF,))


class PelcoManualController:
    def __init__(self, *, device: str | None = None, address: int = 1,
                 simulate: bool = False, serial_factory: Callable | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        encode_motion(address)
        if not simulate and not device:
            raise ValueError("手動控制需要已確認的串口")
        self.device, self.address, self.simulate = device, address, simulate
        self.serial_factory, self.clock = serial_factory, clock
        self._lock = threading.RLock()
        self._port = None
        self._connected = False
        self._session = None
        self._sequence = 0
        self._epoch = 0
        self._direction = "stop"
        self._deadline = self._hold_started = None
        self._error = None
        self._last_frame = ""
        self._tx_frames = 0
        self._notice = "尚未連接"

    def snapshot(self) -> dict:
        with self._lock:
            return {"simulate": self.simulate, "device": self.device,
                    "address": self.address, "baud": 9600,
                    "connected": self._connected, "armed": self._session is not None,
                    "epoch": self._epoch,
                    "direction": self._direction, "error": self._error,
                    "last_frame": self._last_frame, "tx_frames": self._tx_frames,
                    "notice": self._notice}

    def connect(self) -> None:
        with self._lock:
            if self._connected:
                return
            if not self.simulate:
                require_control_port(self.device)
                factory = self.serial_factory
                if factory is None:
                    import serial
                    factory = serial.Serial
                self._port = factory(port=self.device, baudrate=9600, bytesize=8,
                                     parity="N", stopbits=1, timeout=0,
                                     write_timeout=0.1, exclusive=True,
                                     xonxoff=False, rtscts=False, dsrdtr=False)
            self._connected, self._error = True, None
            self._notice = "串口已開啟，尚未啟用手動控制"

    def arm(self, *, epoch: int) -> str:
        with self._lock:
            if type(epoch) is not int or epoch != self._epoch:
                raise ValueError("啟用請求已過期，請重新操作")
            if not self._connected or self._error:
                raise ValueError("請先連接正常的串口")
            if self._session is not None:
                raise ValueError("手動控制已啟用；請先停用再接管")
            self._session = secrets.token_urlsafe(24)
            self._sequence = 0
            self._notice = "手動控制已啟用，等待方向操作"
            return self._session

    def _write(self, frame: bytes) -> None:
        try:
            if not self.simulate and self._port.write(frame) != len(frame):
                raise OSError("串口寫入不完整")
        except Exception as exc:
            self._session = self._deadline = self._hold_started = None
            self._direction = "unknown"
            # A partial motion write may have reached the receiver. Try STOP once.
            try:
                stop = encode_motion(self.address)
                if self._port.write(stop) != len(stop):
                    raise OSError("停止指令寫入不完整")
                suffix = "已嘗試送出停止，請現場確認"
            except Exception:
                suffix = "停止指令送出失敗，請切斷雲台電源"
            self._error = f"串口錯誤：{exc}；{suffix}"
            raise OSError(self._error) from exc
        self._last_frame = frame.hex(" ").upper()
        self._tx_frames += 1

    def move(self, *, session: str, sequence: int, direction: str, speed: int) -> None:
        frame = encode_motion(self.address, direction, speed)
        if direction == "stop":
            raise ValueError("停止請使用 stop 動作")
        with self._lock:
            # Check elapsed time here too: a delayed HTTP request cannot revive a lease.
            self.expire()
            if not self._session or session != self._session:
                raise ValueError("手動控制已停用，請重新啟用")
            if type(sequence) is not int or sequence <= self._sequence:
                raise ValueError("過期的控制指令")
            self._sequence = sequence
            now = self.clock()
            if self._hold_started is None:
                self._hold_started = now
            self._write(frame)
            self._direction = direction
            self._deadline = min(now + LEASE_SECONDS, self._hold_started + MAX_HOLD_SECONDS)
            self._notice = "方向指令已送出；實際動作待現場確認"

    def stop(self, *, session: str | None = None, sequence: int | None = None,
             disarm: bool = False) -> None:
        with self._lock:
            if session == self._session and type(sequence) is int:
                self._sequence = max(self._sequence, sequence)
            else:
                # An emergency stop from another tab also revokes the active owner.
                disarm = True
            self._direction = "stop"
            self._deadline = self._hold_started = None
            if disarm:
                self._session = None
                self._epoch += 1
            if self._connected:
                self._write(encode_motion(self.address))
            self._notice = "停止指令已送出" if self._connected else "尚未連接"

    def expire(self) -> None:
        with self._lock:
            if self._deadline is not None and self.clock() >= self._deadline:
                self.stop(disarm=True)
                self._notice = "操作逾時或已達 2 秒上限，已送停止並停用手動控制"

    def disconnect(self) -> None:
        with self._lock:
            try:
                if self._connected:
                    self.stop(disarm=True)
            finally:
                if self._port is not None:
                    self._port.close()
                    self._port = None
                self._connected = False
                self._session = None
                self._notice = "串口已關閉"
