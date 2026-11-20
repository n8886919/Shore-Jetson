"""Leased PWM jogging, serialized by the tracking coordinator's control lock."""
from __future__ import annotations

import math
import secrets

from shore_tracker.config import PWM_MANUAL_TILT_LIMITS_DEG


class KeyboardJog:
    LEASE_SECONDS = .5

    def __init__(self, config, status, output, pause, clock):
        self.config, self.status = config, status
        self.output, self.pause, self.clock = output, pause, clock
        self.available = (config.protocol == 'm0_pulse' and config.enabled
            and (config.pan_channel, config.tilt_channel) == (0, 1)
            and config.pan_direction in {-1, 1} and config.tilt_direction in {-1, 1}
            and output is not None and pause is not None)
        self.revision = 0
        self.session = None
        self.sequence = 0
        self.direction = None
        self.velocity = 0.
        self.last_at = None
        self.deadline = 0.
        self.generation = None
        self.speed_deg_s = min(5., config.max_slew_deg_s)

    def snapshot(self):
        state = self.status.snapshot()
        return {'available': self.available,
                'enabled': self.available and state['tracking']['armed']
                    and not state['inference']['enabled']
                    and all(state['components'][k]['state'] == 'ready' for k in ('camera', 'servo')),
                'epoch': f"{state['control_epoch']}:{self.revision}",
                'moving': self.session is not None,
                'speed_deg_s': self.speed_deg_s,
                'max_speed_deg_s': min(5., self.config.max_slew_deg_s)}

    def stop(self):
        self.revision += 1
        self.session = self.direction = self.last_at = None
        self.velocity = 0.
        if self.pause is not None:
            self.pause()

    def command(self, action, values):
        self.expire()
        state = self.snapshot()
        if action == 'speed':
            speed = values['speed_deg_s']
            if (not self.available or type(speed) not in {int, float}
                    or not math.isfinite(speed) or not .1 <= speed <= state['max_speed_deg_s']):
                raise ValueError('手動控制速度須介於 0.1 與允許的上限（至多 5°/s）')
            self.speed_deg_s = float(speed)
            return {}
        if action == 'stop':
            if values['epoch'] == state['epoch']:
                self.stop()  # Fences a start still in flight, even before its reply.
            return {}
        if not state['enabled']:
            raise ValueError('請先開始追蹤並關閉 AI 辨識，再使用方向鍵')
        direction = values.get('direction')
        if direction not in {'up', 'down', 'left', 'right'}:
            raise ValueError('方向無效')
        if action == 'start':
            if values['epoch'] != state['epoch'] or self.session is not None:
                raise ValueError('方向鍵請求已過期或已由另一個畫面控制')
            self.session = secrets.token_urlsafe(24)
            self.sequence = 0
            self.generation = self.status.snapshot()['control_epoch']
            self.last_at = self.clock()
        elif action == 'move':
            if (self.session is None or values['session'] != self.session
                    or type(values['sequence']) is not int or values['sequence'] <= self.sequence):
                raise ValueError('方向鍵連線已停止，請放開後重新按下')
            self.sequence = values['sequence']
        else:
            raise ValueError('未知方向鍵指令')
        if self.direction != direction:
            self.velocity = 0.
        self.direction = direction
        self.deadline = self.clock() + self.LEASE_SECONDS
        return {'session': self.session}

    def expire(self):
        if self.session is not None and (self.clock() >= self.deadline
                or self.status.snapshot()['control_epoch'] != self.generation
                or not self.snapshot()['enabled']):
            self.stop()

    def step(self, pan, tilt):
        self.expire()
        if self.session is None:
            return pan, tilt
        now = self.clock()
        dt = min(.1, max(0., now - self.last_at))
        self.last_at = now
        cfg = self.config
        speed = self.speed_deg_s
        acceleration = min(10., cfg.max_acceleration_deg_s2)
        horizontal = self.direction in {'left', 'right'}
        current = pan if horizontal else tilt
        low, high = ((cfg.angle_min_deg, cfg.angle_max_deg) if horizontal
                     else PWM_MANUAL_TILT_LIMITS_DEG)
        if not horizontal and not low <= current <= high:
            self.stop()
            return pan, tilt
        sign = (-1 if self.direction in {'left', 'up'} else 1) * (
            cfg.pan_direction if horizontal else cfg.tilt_direction)
        distance = max(0., high-current if sign > 0 else current-low)
        # Reserve this tick's travel plus braking distance. This discrete bound
        # avoids clipping abruptly at a limit while the requested speed is high.
        braking = math.sqrt((acceleration * dt)**2 + 2 * acceleration * distance) - acceleration * dt
        desired = min(speed, braking)
        self.velocity += max(-acceleration*dt, min(acceleration*dt, desired-self.velocity))
        target = max(low, min(high, current + sign * self.velocity * dt))
        if horizontal:
            pan = target
            self.output(pan, None)  # Horizontal jogging never sends a tilt target.
        else:
            tilt = target
            self.output(pan, tilt)
        return pan, tilt
