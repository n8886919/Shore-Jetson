"""One volatile BGRx frame for the local desktop; never a media archive."""
from __future__ import annotations

import fcntl
import mmap
import os
from pathlib import Path
import struct
import tempfile
import threading
import time

# Little-endian, also read by native/main.cpp. Payload starts at byte 64.
HEADER = struct.Struct('<8sIIIIQd')
CAPACITY = 64 + 960 * 540 * 4


class NativeFrames:
    def __init__(self):
        self.path: Path | None = None
        self._file = None
        self._map = None
        self._sequence = 0
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self._file is not None:
                return
            # /dev/shm is RAM-backed on the supported Jetsons and Linux host.
            fd, name = tempfile.mkstemp(prefix=f'shore-display-{os.getuid()}-', dir='/dev/shm')
            os.fchmod(fd, 0o600)
            self.path = Path(name)
            self._file = os.fdopen(fd, 'r+b', buffering=0)
            self._file.truncate(CAPACITY)
            self._map = mmap.mmap(fd, CAPACITY)

    def publish(self, data, width, height, stride, **_metadata):
        if not (0 < width <= 960 and 0 < height <= 540
                and width * 4 <= stride <= 960 * 4 and len(data) >= stride * height):
            return False
        with self._lock:
            if self._file is None:
                return False
            try:
                fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False  # A slow display must never block the camera.
            try:
                self._sequence += 1
                self._map[64:64 + stride * height] = data[:stride * height]
                self._map[:HEADER.size] = HEADER.pack(
                    b'SHORE01\0', width, height, stride, 0,
                    self._sequence, time.monotonic())
            finally:
                fcntl.flock(self._file, fcntl.LOCK_UN)
        return True

    def close(self):
        with self._lock:
            if self._map is not None:
                self._map.close()
                self._map = None
            if self._file is not None:
                self._file.close()
                self._file = None
            if self.path is not None:
                self.path.unlink(missing_ok=True)
                self.path = None
