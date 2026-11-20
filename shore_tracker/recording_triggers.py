"""One clip recorder shared by following, continuous tracking and legacy manual requests."""
from __future__ import annotations

import threading
import time
import json
from pathlib import Path

from shore_tracker.inference import FollowState


class RecordingTriggers:
    def __init__(self, recorder, *, armed_reader, stale_seconds=1.5, clock=time.monotonic,
                 settings_path=None, activity_callback=None):
        self.recorder = recorder
        self.armed_reader = armed_reader
        self.stale_seconds = stale_seconds
        self.clock = clock
        self.activity_callback = activity_callback
        self._lock = threading.RLock()
        self._manual = False
        self._continuous = False
        self._settings_path = Path(settings_path) if settings_path is not None else None
        self._automatic = False
        self._qualified = False
        self._target_id = None
        self._decision_at = None
        self._retry_at = 0.0
        self._error = ''
        self._failed_id = None
        self._closing = False
        self._stop = threading.Event()
        self._thread = None

    def recover(self):
        self.recorder.recover()
        if self._settings_path is not None:
            try:
                settings = json.loads(self._settings_path.read_text())
                if isinstance(settings, dict) and type(settings.get('continuous')) is bool:
                    self._continuous = settings['continuous']
            except (OSError, ValueError):
                pass  # A new or unreadable preference defaults to event recording.
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='recording-triggers', daemon=True)
        self._thread.start()

    def set_continuous(self, enabled):
        if type(enabled) is not bool:
            return False, '連續錄影設定必須為開啟或關閉'
        with self._lock:
            if self._closing:
                return False, '服務正在結束'
            if self._settings_path is not None:
                try:
                    self._settings_path.parent.mkdir(parents=True, exist_ok=True)
                    temp = self._settings_path.with_suffix('.tmp')
                    temp.write_text(json.dumps({'continuous': enabled})+'\n')
                    temp.chmod(0o600)
                    temp.replace(self._settings_path)
                except OSError:
                    return False, '無法儲存錄影設定'
            self._continuous = enabled
            if enabled and self.armed_reader():
                self.recorder.note_trigger('continuous')
            self._retry_at = 0.0
            self._error = ''
            self._publish_activity()
        return True, '連續錄影已開啟' if enabled else '已改為自動跟拍錄影'

    def command(self, action):
        if action not in {'start', 'stop'}:
            return False, '錄影指令無效'
        with self._lock:
            if self._closing:
                return False, '服務正在結束'
            self._manual = action == 'start'
            if self._manual:
                self.recorder.note_trigger('manual')
            self._retry_at = 0.0
            self._error = ''
            self._publish_activity()
        return True, '手動錄影已開啟' if action == 'start' else '手動錄影已結束；自動跟拍依追蹤狀態繼續'

    def follow(self, decision, now):
        # Keep recording through the tracker's short loss/inactive grace. The
        # finish/search transition releases the automatic trigger.
        active = (decision.state in {FollowState.CHASING, FollowState.TAKEOFF, FollowState.SURFING}
                  and self.armed_reader())
        with self._lock:
            if self._closing: return
            was_active = self._automatic
            if active and not self._automatic:
                self._qualified = False
            self._qualified = self._qualified or decision.recording_valid
            self._automatic = active
            self._target_id = decision.target_track_id if active else None
            if self._qualified and (active or was_active):
                self.recorder.note_trigger('tracking', decision.target_track_id, qualified=True)
            self._decision_at = now
            self._publish_activity()

    def cancel_tracking(self, _reason=''):
        with self._lock:
            self._target_id = None
            self._automatic = False
            self._decision_at = None
            self._publish_activity()

    def stop_all(self):
        with self._lock:
            self._manual = False
            self._automatic = False
            self._target_id = None
        self.recorder.command('stop')

    def _publish_activity(self):
        # Called under the trigger lock, before starting a recorder. The status
        # lock makes forced enable and concurrent UI disable mutually exclusive.
        if self.activity_callback is not None:
            busy = self.recorder.snapshot()['state'] in {'starting', 'recording', 'finalizing'}
            required = self._manual or self._automatic or (self._continuous and bool(self.armed_reader())) or busy
            self.activity_callback(bool(required))

    def snapshot(self):
        with self._lock:
            result = self.recorder.snapshot()
            result.update(manual_requested=self._manual,
                          continuous_enabled=self._continuous,
                          continuous_requested=self._continuous and bool(self.armed_reader()),
                          tracking_requested=self._automatic,
                          tracking_qualified=self._qualified,
                          tracking_target_id=self._target_id)
            if self._error: result['detail'] = self._error
            return result

    def check_once(self):
        with self._lock:
            if self._closing: return
            now = self.clock()
            if (not self.armed_reader() or self._decision_at is None
                    or now - self._decision_at > self.stale_seconds):
                self._target_id = None
                self._automatic = False
            continuous = self._continuous and self.armed_reader()
            wanted = self._manual or self._automatic or continuous
            snapshot = self.recorder.snapshot()
            state = snapshot['state']
            self._publish_activity()
            if state == 'failed' and snapshot.get('id') != self._failed_id:
                self._failed_id = snapshot.get('id')
                self._retry_at = now + 5.0
            if not wanted:
                if state in {'starting', 'recording'}: self.recorder.command('stop')
                self._error = ''
                self._retry_at = 0.0
                return
            if state == 'finalizing': return  # pending triggers resume after EOS
            if state in {'idle', 'failed'}:
                if now < self._retry_at: return
                trigger = 'continuous' if continuous else 'tracking' if self._automatic else 'manual'
                ok, detail = self.recorder.command('start', trigger=trigger, target_track_id=self._target_id)
                self._error = '' if ok else detail
                if not ok:
                    self._retry_at = now + 5.0
                    return
                self._retry_at = 0.0
            if self._manual: self.recorder.note_trigger('manual')
            if continuous: self.recorder.note_trigger('continuous')
            if self._automatic: self.recorder.note_trigger('tracking', self._target_id, qualified=self._qualified)

    def _run(self):
        while not self._stop.wait(.2):
            try:
                self.check_once()
            except Exception:
                with self._lock:
                    self._error = '錄影觸發未完成，將重試；請檢查相機與儲存空間'
                    self._retry_at = self.clock() + 5.0

    def close(self):
        with self._lock:
            self._closing = True
            self._manual = False
            self._automatic = False
            self._target_id = None
            self._stop.set()
        if self._thread: self._thread.join(timeout=2)
        self.recorder.close()

    def recordings(self): return self.recorder.recordings()
    def file(self, recording_id): return self.recorder.file(recording_id)
    def acknowledge(self, recording_id, state): return self.recorder.acknowledge(recording_id, state)
