from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class DiskPressure(StrEnum):
    OK = "ok"
    WARNING = "warning"
    STOP = "stop"


@dataclass(frozen=True)
class DiskStatus:
    pressure: DiskPressure
    free_bytes: int
    total_bytes: int
    used_bytes: int | None = None

    @property
    def free_gib(self) -> float:
        return self.free_bytes / (1024**3)


class DiskGuard:
    def __init__(self, warning_gib: int, stop_gib: int) -> None:
        if stop_gib >= warning_gib:
            raise ValueError("stop threshold must be lower than warning threshold")
        self.warning_bytes = warning_gib * 1024**3
        self.stop_bytes = stop_gib * 1024**3

    def check(self, path: str | Path) -> DiskStatus:
        usage = shutil.disk_usage(Path(path))
        if usage.free <= self.stop_bytes:
            pressure = DiskPressure.STOP
        elif usage.free <= self.warning_bytes:
            pressure = DiskPressure.WARNING
        else:
            pressure = DiskPressure.OK
        return DiskStatus(pressure, usage.free, usage.total, usage.used)
