"""Owner-requested bounded PWM sequence; positions are commands, never feedback."""
from __future__ import annotations

import math
import secrets
import threading
import time


class PwmServoTest:
    LEASE_SECONDS = 1.5
    MAX_SECONDS = 60.0
    HOLD_SECONDS = .35

    def __init__(self, *, service_factory, pan_direction, tilt_direction,
                 clock=time.monotonic):
        self.factory = service_factory
        self.pan_direction, self.tilt_direction = pan_direction, tilt_direction
        self.clock = clock
        self._lock = threading.RLock()
        self._service = None
        self._session = None
        self._epoch = 0
        self._sequence = 0
        self._phase = 'idle'
        self._notice = '水平 M0、垂直 M1；按下後兩軸先置中'
        self._error = None
        self._pan = self._tilt = None

    def snapshot(self):
        with self._lock:
            return {'running': self._session is not None, 'connected': self._service is not None,
                    'epoch': self._epoch, 'phase': self._phase, 'notice': self._notice,
                    'error': self._error, 'pan_requested_deg': self._pan,
                    'tilt_requested_deg': self._tilt}

    def start(self, *, epoch):
        with self._lock:
            if type(epoch) is not int or epoch != self._epoch:
                raise ValueError('測試請求已過期，請重新操作')
            if self._service is not None:
                raise ValueError('請等待前一次測試釋放串口')
            if self.pan_direction not in {-1, 1} or self.tilt_direction not in {-1, 1}:
                raise ValueError('請先確認兩軸方向')
            self._service = self.factory()
            self._service.start()
            self._service.client.arm()  # The tracking state remains DISARMED.
            self._session = secrets.token_urlsafe(24)
            self._sequence = 0
            self._error = None
            self._pan = self._tilt = 90.0
            right = 90 + 20 * self.pan_direction
            self._steps = [
                ('center', '兩舵機置中：水平 90°、垂直 90°', 90., 90., 1.),
                ('left', '水平向左轉 20°', 90 - 20 * self.pan_direction, 90., self._duration(20)),
                ('right', '水平向右轉 40°', right, 90., self._duration(40)),
                ('up', '抬頭 40°', right, 90 - 40 * self.tilt_direction, self._duration(40)),
                ('down', '低頭 40°，垂直回中', right, 90., self._duration(40)),
            ]
            now = self.clock()
            self._started = self._phase_started = now
            self._deadline = now + self.LEASE_SECONDS
            self._index = 0
            self._from = (90., 90.)
            self._phase, self._notice = self._steps[0][:2]
            return self._session

    @staticmethod
    def _duration(degrees):
        # Cosine easing: commanded speed <=5 deg/s, acceleration <=10 deg/s².
        return max(degrees * math.pi / 10, math.pi * math.sqrt(degrees / 20))

    def renew(self, *, session, sequence):
        with self._lock:
            self._expire()
            if self._session is None and self._phase == 'complete':
                return  # Report completion to the last in-flight heartbeat; never renew it.
            if self._session is None or session != self._session:
                raise ValueError('測試已停止，請重新操作')
            if type(sequence) is not int or sequence <= self._sequence:
                raise ValueError('測試指令已過期')
            self._sequence = sequence
            self._deadline = self.clock() + self.LEASE_SECONDS

    def _expire(self):
        if self._session is not None and (self.clock() >= self._deadline
                or self.clock() - self._started >= self.MAX_SECONDS):
            self.stop('測試連線逾時，已停止送出目標')

    def tick(self):
        """Called by the runtime watchdog; a GET never drives the sequence."""
        with self._lock:
            was_running = self._session is not None
            self._expire()
            if self._session is None:
                return was_running
            try:
                if not self._service.is_connected():
                    raise OSError('舵機串口中斷')
                phase, notice, pan, tilt, duration = self._steps[self._index]
                elapsed = self.clock() - self._phase_started
                fraction = min(1., max(0., elapsed / duration))
                blend = (1 - math.cos(math.pi * fraction)) / 2
                self._pan = self._from[0] + (pan - self._from[0]) * blend
                self._tilt = self._from[1] + (tilt - self._from[1]) * blend
                self._service.set_axes(self._pan, self._tilt)
                if elapsed >= duration + self.HOLD_SECONDS:
                    self._index += 1
                    if self._index == len(self._steps):
                        self.stop('測試指令完成：水平在中心右側 20°，垂直回中', phase='complete')
                        return True
                    self._from = (pan, tilt)
                    self._phase_started = self.clock()
                    self._phase, self._notice = self._steps[self._index][:2]
            except Exception:
                self._error = '舵機串口異常，測試已停止'
                self.stop(self._error, phase='error')
                return True
            return False

    def stop(self, notice='測試已停止；停止送目標不等於斷電', *, phase='stopped'):
        with self._lock:
            self._epoch += 1  # Also fences a delayed start after leaving the page.
            self._session = None
            self._phase, self._notice = phase, notice
            if self._service is not None:
                self._service.stop_motion()

    def close(self):
        with self._lock:
            if self._session is not None:
                self.stop()
            if self._service is None:
                return True
            if not self._service.close():
                return False
            self._service = None
            return True
