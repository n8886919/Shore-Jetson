"""M0 pulse commands for this project's GXServo 42 kg, 180-degree servo.

500..2500 us is the controller command range. Commanded degrees are not
measured shaft position; mechanical endpoints still need commissioning.
"""
from __future__ import annotations

import math


def encode_position(degrees: float, *, channel: int = 0) -> bytes:
    if type(channel) is not int or channel not in (0, 1):
        raise ValueError("Servo channel must be 0 or 1")
    if not math.isfinite(degrees) or not 0 <= degrees <= 180:
        raise ValueError("GXServo angle must be finite and in [0, 180]")
    # Integer half-up quantization, with 90 degrees exactly at 1500 us.
    millidegrees = math.floor(degrees * 1000 + 0.5)
    pulse = 500 + (millidegrees * 2000 + 90000) // 180000
    return f"#{channel:02d}P{pulse:04d}!".encode("ascii")
