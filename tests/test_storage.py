from collections import namedtuple

import pytest

from shore_tracker.storage import DiskGuard, DiskPressure


Usage = namedtuple("Usage", "total used free")


@pytest.mark.parametrize(
    ("free_gib", "expected"),
    [
        (121, DiskPressure.OK),
        (120, DiskPressure.WARNING),
        (101, DiskPressure.WARNING),
        (100, DiskPressure.STOP),
        (1, DiskPressure.STOP),
    ],
)
def test_disk_guard_thresholds(monkeypatch, tmp_path, free_gib, expected) -> None:
    monkeypatch.setattr(
        "shore_tracker.storage.shutil.disk_usage",
        lambda _path: Usage(500 * 1024**3, (499 - free_gib) * 1024**3, free_gib * 1024**3),
    )
    status = DiskGuard(120, 100).check(tmp_path)
    assert status.pressure is expected
    assert status.used_bytes == (499 - free_gib) * 1024**3


def test_disk_guard_rejects_inverted_thresholds() -> None:
    with pytest.raises(ValueError):
        DiskGuard(100, 100)
