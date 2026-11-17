"""Completed clips for automatic and manual triggers, independent of preview."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from shore_tracker.camera.pipeline import RtspUri, _load_gstreamer

RECORDING_ID = re.compile(r"^[0-9a-f]{32}$")


def atomic_json(path, value):
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


class GstClip:
    """Encoded video copy to MKV; EOS must complete before publishing the file."""
    def __init__(self, uri, codec, path):
        bindings = _load_gstreamer()
        self.gst = gst = bindings.Gst
        if codec not in {'h264', 'h265'}:
            raise ValueError('unsupported codec')
        self.pipeline = gst.parse_launch(
            f'rtspsrc name=source protocols=tcp latency=250 ! '
            f'application/x-rtp,media=video,encoding-name={codec.upper()} ! '
            f'rtp{codec}depay ! {codec}parse config-interval=-1 ! '
            'matroskamux ! filesink name=output sync=false'
        )
        self.pipeline.get_by_name('source').set_property('location', uri._reveal())
        self.pipeline.get_by_name('output').set_property('location', str(path))
        self.bus = self.pipeline.get_bus()

    def start(self):
        if self.pipeline.set_state(self.gst.State.PLAYING) == self.gst.StateChangeReturn.FAILURE:
            raise RuntimeError('recording start failed')

    def poll_error(self):
        message = self.bus.timed_pop_filtered(0, self.gst.MessageType.ERROR | self.gst.MessageType.EOS)
        return message is not None

    def finish(self):
        try:
            if not self.pipeline.send_event(self.gst.Event.new_eos()):
                return False
            message = self.bus.timed_pop_filtered(
                15 * self.gst.SECOND, self.gst.MessageType.EOS | self.gst.MessageType.ERROR)
            return message is not None and message.type == self.gst.MessageType.EOS
        finally:
            self.pipeline.set_state(self.gst.State.NULL)

    def abort(self):
        self.pipeline.set_state(self.gst.State.NULL)


class ManualRecording:
    def __init__(self, root, uri_reader, codec, rotation_degrees, free_stop_gib, *, builder=GstClip):
        self.root = Path(root)
        self.uri_reader = uri_reader
        self.codec = codec
        self.rotation = rotation_degrees
        self.free_stop_gib = free_stop_gib
        self.builder = builder
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._state = 'idle'
        self._id = None
        self._started = None
        self._detail = '尚未開始錄影'
        self._closing = False
        self._metadata = None

    def recover(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in self.root.glob('*/recording.json'):
            try:
                data = json.loads(path.read_text())
                if data['state'] in {'starting', 'recording', 'finalizing'}:
                    data.update(state='failed', detail='服務中斷；保留未完成檔案')
                    atomic_json(path, data)
            except (OSError, ValueError, KeyError):
                continue

    def snapshot(self):
        with self._lock:
            detail = self._detail
            if self._id and self._state == 'idle':
                try:
                    data = json.loads((self.root / self._id / 'recording.json').read_text())
                    detail = {'pending': '影片已完成，等待傳送到 SurfTrack', 'uploading': '影片傳送中',
                              'uploaded': '影片已上傳私人 Google Drive', 'failed': '上傳未完成，保留原檔等待重試'}.get(data['upload_state'], detail)
                except (OSError, ValueError, KeyError): pass
            return {'state': self._state, 'id': self._id, 'detail': detail,
                    'elapsed_seconds': round(time.monotonic() - self._started) if self._started else 0}

    def note_trigger(self, trigger, target_track_id=None, *, qualified=False):
        with self._lock:
            if self._metadata is None or self._state not in {'starting', 'recording'}:
                return
            if qualified:
                self._metadata['qualified'] = True
            if trigger not in self._metadata['triggers']:
                self._metadata['triggers'].append(trigger)
            if target_track_id is not None and target_track_id not in self._metadata['target_track_ids']:
                self._metadata['target_track_ids'].append(target_track_id)

    def command(self, action, *, trigger='manual', target_track_id=None):
        with self._lock:
            if action == 'stop':
                if self._state in {'starting', 'recording'}:
                    self._state = 'finalizing'
                    self._detail = '正在完成影片'
                    self._stop.set()
                return True, self._detail
            if action != 'start':
                return False, '錄影指令無效'
            if self._closing:
                return False, '服務正在結束'
            if self._state == 'finalizing':
                return False, '正在完成上一段影片'
            if self._thread is not None and self._thread.is_alive():
                self.note_trigger(trigger, target_track_id)
                return True, self._detail  # duplicate start must never toggle off
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if shutil.disk_usage(self.root).free / 1024**3 <= self.free_stop_gib:
                return False, '磁碟空間不足'
            self._id = uuid.uuid4().hex
            directory = self.root / self._id
            directory.mkdir(mode=0o700)
            self._state = 'starting'
            self._started = time.monotonic()
            self._detail = '正在連接錄影串流'
            self._stop.clear()
            data = {'id': self._id, 'state': 'starting', 'codec': self.codec,
                    'rotation_degrees': self.rotation, 'upload_state': 'pending',
                    'started_at': datetime.now(timezone.utc).isoformat(),
                    'triggers': [trigger], 'qualified': False, 'target_track_ids': [target_track_id] if target_track_id is not None else []}
            self._metadata = data
            atomic_json(directory / 'recording.json', data)
            self._thread = threading.Thread(target=self._run, args=(directory, data), daemon=True)
            self._thread.start()
            return True, self._detail

    def _run(self, directory, data):
        clip = None
        partial = directory / 'clip.partial.mkv'
        failed = False
        try:
            clip = self.builder(self.uri_reader(), self.codec, partial)
            clip.start()
            last_size = 0
            last_progress = time.monotonic()
            while not self._stop.wait(0.5):
                if clip.poll_error():
                    raise RuntimeError('recording stream ended')
                size = partial.stat().st_size if partial.exists() else 0
                if size > last_size:
                    last_size, last_progress = size, time.monotonic()
                    with self._lock:
                        if self._state == 'starting':
                            self._state = 'recording'
                            self._detail = '錄影中'
                            data['state'] = 'recording'
                            atomic_json(directory / 'recording.json', data)
                if time.monotonic() - last_progress > 15:
                    raise RuntimeError('recording stalled')
                if shutil.disk_usage(self.root).free / 1024**3 <= self.free_stop_gib:
                    self.command('stop')
            if not clip.finish() or not partial.is_file() or partial.stat().st_size == 0:
                raise RuntimeError('recording finalization failed')
            clip = None
            with partial.open('rb') as handle:
                digest = hashlib.file_digest(handle, 'sha256').hexdigest()
            partial.replace(directory / 'clip.mkv')
            data.update(state='ready', size_bytes=(directory / 'clip.mkv').stat().st_size, sha256=digest)
        except Exception:
            failed = True
            data.update(state='failed', detail='錄影未完整結束；保留檔案，未送往上傳')
        finally:
            with self._lock:
                self._state = 'finalizing'
            if clip is not None:
                try: clip.abort()
                except Exception: pass
            # Only this new, automatic, never-qualified event may be deleted.
            # Manual or continuous overlap always preserves the clip.
            if not {'manual', 'continuous'}.intersection(data.get('triggers', [])) and not data.get('qualified'):
                try:
                    partial.unlink(missing_ok=True)
                    (directory / 'clip.mkv').unlink(missing_ok=True)
                    data.update(state='discarded', size_bytes=0, upload_state='not_applicable',
                                detail='未發展成起乘／衝浪，試拍影片已刪除')
                    data.pop('sha256', None)
                except OSError:
                    failed = True
                    data.update(state='failed', detail='試拍影片刪除未完成，請檢查儲存空間')
            data['ended_at'] = datetime.now(timezone.utc).isoformat()
            try: atomic_json(directory / 'recording.json', data)
            finally:
                with self._lock:
                    self._state = 'failed' if failed else 'idle'
                    self._started = None
                    self._detail = data.get('detail', '影片已完成，等待傳送到 SurfTrack')

    def close(self):
        with self._lock:
            self._closing = True
            self.command('stop')
            thread = self._thread
        if thread: thread.join(timeout=20)

    def recordings(self):
        items = []
        for path in sorted(self.root.glob('*/recording.json'), reverse=True):
            if not RECORDING_ID.fullmatch(path.parent.name): continue
            try:
                data = json.loads(path.read_text())
                if data['state'] in {'ready', 'failed', 'discarded'}: items.append(data)
            except (OSError, ValueError, KeyError): continue
        return items

    def file(self, recording_id):
        if not RECORDING_ID.fullmatch(recording_id): raise ValueError('invalid id')
        directory = self.root / recording_id
        data = json.loads((directory / 'recording.json').read_text())
        if data['state'] != 'ready': raise ValueError('not finalized')
        path = directory / 'clip.mkv'
        if path.is_symlink() or directory.is_symlink(): raise ValueError('invalid path')
        return path

    def acknowledge(self, recording_id, state):
        if state not in {'uploaded', 'failed', 'uploading'}: raise ValueError('invalid upload state')
        with self._lock:
            self.file(recording_id)
            path = self.root / recording_id / 'recording.json'
            data = json.loads(path.read_text())
            data['upload_state'] = state
            atomic_json(path, data)
