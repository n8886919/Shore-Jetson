#!/usr/bin/env python3
"""One explicitly armed, bounded Jetson-to-Servo movement smoke test."""

from __future__ import annotations

import argparse
import time
from dataclasses import replace
from pathlib import Path

from shore_tracker.config import load_config
from shore_tracker.servo.simple_service import build_servo_service
from shore_tracker.status import StatusStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True,
                        help="TOML with the verified non-console serial_device")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="required confirmation for the bounded physical movement",
    )
    parser.add_argument("--confirm-centered", action="store_true",
                        help="operator has independently verified the 90-degree centre")
    args = parser.parse_args()
    if not args.execute or not args.confirm_centered:
        parser.error("--execute and --confirm-centered are required for physical movement")

    config = replace(
        load_config(args.config).servo,
        enabled=True,
        angle_min_deg=85.0,
        angle_max_deg=95.0,
        home_deg=90.0,
    )
    print("prerequisite=service stopped, confirmed centre, operator at power cut-off; no position feedback", flush=True)
    service = build_servo_service(config, StatusStore())
    service.start()
    try:
        ok, message = service.request_arm()
        if not ok:
            raise RuntimeError(message)
        angle = 90.0
        for target in (95.0, 85.0, 90.0):
            print(f"commanded_target_degrees={target:.1f}", flush=True)
            while abs(target - angle) > 1e-8:
                angle += max(-0.25, min(0.25, target - angle))
                service.set_target_degrees(angle)
                time.sleep(0.05)
            deadline = time.monotonic() + .5
            while time.monotonic() < deadline:
                service.set_target_degrees(angle)
                time.sleep(.05)
    finally:
        service.stop_motion("bounded smoke test complete")
        service.close()
        print("uart=stopped; servo may finish the last target; power remains on", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
