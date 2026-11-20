"""Explicit Web control of the Anjvision camera's scheduled SD recording."""
from __future__ import annotations

import http.client
import ipaddress
import os
import re
import threading
from datetime import datetime
from urllib.parse import urlencode
from xml.etree import ElementTree as ET


class _CameraError(Exception):
    """Message is safe for the Web status page."""


class _LoginRejected(_CameraError):
    pass


class CameraCardRecording:
    destination = 'camera_sd'

    def __init__(self, host_reader):
        # Runtime's reader validates the route to the camera on its dedicated interface.
        self._host_reader = host_reader
        self._operation = threading.RLock()
        self._lock = threading.Lock()
        self._closing = False
        self._owned_device = None
        self._rejected = None
        self._status = dict(state='unknown', available=False, schedule_enabled=None,
                            detail='尚未查詢相機卡錄影排程', last_card_clip=None,
                            clip_query_detail='', writing_confirmed=None)

    def _publish(self, **fields):
        with self._lock:
            self._status.update(fields)

    def snapshot(self):
        with self._lock:
            result = dict(self._status)
            if result['last_card_clip'] is not None:
                result['last_card_clip'] = dict(result['last_card_clip'])
        return dict(result, destination=self.destination, continuous_enabled=False,
                    manual_requested=False, tracking_requested=False, elapsed_seconds=0, id=None)

    @staticmethod
    def _get(host, endpoint, params=None):
        # HTTPConnection talks only to this numeric IP. It does not consult proxy
        # configuration or follow redirects, including on the credentialed call.
        conn = http.client.HTTPConnection(host, timeout=2)
        path = '/cgi-bin/' + endpoint
        if params:
            path += '?' + urlencode(params)
        try:
            conn.request('GET', path, headers={'Connection': 'close', 'Cache-Control': 'no-store'})
            response = conn.getresponse()
            if endpoint == 'getuid' and response.status in {401, 403}:
                raise _LoginRejected('相機拒絕管理登入，請核對相機管理帳密')
            if response.status not in {200, 400}:
                raise _CameraError('相機管理介面未完成請求')
            payload = response.read(262145)
            if (len(payload) > 262144 or b'<!DOCTYPE' in payload.upper()
                    or b'<!ENTITY' in payload.upper()):
                raise _CameraError('相機管理介面回覆格式異常')
            root = ET.fromstring(payload)
            if root.tag == 'fault' or response.status != 200:
                reason = (root.findtext('reason') or '').strip().lower()
                if endpoint == 'getuid' and reason in {'password error', 'username error', 'user error'}:
                    raise _LoginRejected('相機拒絕管理登入，請核對相機管理帳密')
                raise _CameraError('相機拒絕此項管理操作')
            return root
        except (OSError, http.client.HTTPException, ET.ParseError, ValueError):
            raise _CameraError('無法讀取相機管理回覆，請確認相機連線') from None
        finally:
            conn.close()

    def _login(self):
        user = os.environ.get('SHORE_CAMERA_ADMIN_USER')
        password = os.environ.get('SHORE_CAMERA_ADMIN_PASSWORD')
        if not user or not password:
            raise _CameraError('尚未設定 Shore Jetson 使用的相機管理帳密')
        try:
            host = str(ipaddress.IPv4Address(self._host_reader()))
        except Exception:
            # A route/URI exception might itself contain credentials.
            raise _CameraError('無法確認相機專用網路連線') from None
        # Remember a rejection for this process without retaining another plaintext password.
        credentials = (host, user, hash(password))
        if self._rejected == credentials:
            raise _LoginRejected('相機先前拒絕此管理帳密，請更新帳密')
        info = self._get(host, 'getinfo')
        model = info.get('model') or info.get('oem')
        if info.tag.lower() != 'info' or model not in {'MCA85', 'MCA85_AF'}:
            raise _CameraError('相機型號與設定不符，請確認相機連線')
        # DHCP can change the address of the same camera. Never transfer
        # outstanding cleanup to a different camera that happens to use its IP.
        device = (info.get('mac_address') or host).lower()
        if self._owned_device is not None and device != self._owned_device:
            raise _CameraError('相機已更換，上一次控制的相機卡排程仍待核對')
        try:
            login = self._get(host, 'getuid', {'username': user, 'password': password})
        except _LoginRejected:
            self._rejected = credentials
            raise
        uid = (login.text or '').strip()
        if login.tag != 'uid' or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', uid):
            raise _CameraError('未取得相機管理工作階段')
        self._rejected = None
        return host, uid, device

    def _schedule(self, host, uid):
        root = self._get(host, 'setScheduleStorage', {'uid': uid})
        common, schedule = root.find('Common'), root.find('ScheduleRecord')
        if root.tag != 'RecordConfig' or common is None or schedule is None:
            raise _CameraError('相機未回傳可核對的卡片錄影排程')
        bits = (common.get('LocalEnable'), common.get('RemoteEnable'),
                schedule.get('LocalStore'), schedule.get('RemoteStore'))
        if any(value not in {'0', '1'} for value in bits):
            raise _CameraError('相機的錄影目的地或排程欄位不完整')
        local, remote, scheduled, scheduled_remote = bits
        if local != '1' or remote != '0' or scheduled_remote != '0':
            raise _CameraError('相機未設為只儲存至本機卡，請檢查相機錄影目的地')
        return scheduled == '1'

    def _recent_clip(self, host, uid):
        # Use the camera's own calendar day, independent of the Nano timezone.
        state = self._get(host, 'getDevState', {'uid': uid})
        try:
            now = datetime.strptime(state.attrib['system_time'], '%Y-%m-%d-%H-%M-%S')
        except (KeyError, ValueError):
            raise _CameraError('無法確認相機日期，未查到當日片段') from None
        records = self._get(host, 'get_record_query', {'uid': uid, 'year': now.year,
                            'month': now.month, 'day': now.day, 'record_mode': 2})
        if records.tag != 'RecordQueryInfo':
            raise _CameraError('相機未回傳當日錄影清單')
        recent = None
        for item in records.findall('items'):
            try:
                started = datetime.strptime(item.attrib['start_time'], '%Y-%m-%d %H:%M:%S')
                size = int(item.attrib['filesize'])
            except (KeyError, ValueError):
                continue
            if (item.get('record_mode') == '2' and started.date() == now.date() and size > 0
                    and re.match(r'^/?mnt/mmc\d+/schedule/', item.get('filepath', ''))):
                if recent is None or started > recent[0]:
                    recent = started, size
        if recent is not None:
            return {'started_at_camera': recent[0].isoformat(), 'size_bytes': recent[1]}
        return None

    def command(self, action):
        if action not in ('start', 'stop', 'refresh'):
            return False, '相機卡錄影指令無效'
        with self._operation:
            if self._closing and action != 'stop':
                return False, '服務正在結束'
            states = {'start': 'starting', 'stop': 'stopping', 'refresh': 'checking'}
            self._publish(state=states[action], available=False, schedule_enabled=None,
                          detail='正在核對相機卡錄影排程')
            try:
                host, uid, device = self._login()
                enabled = self._schedule(host, uid)
                if action != 'refresh' and enabled != (action == 'start'):
                    if action == 'start':
                        # A timeout after sending may still have enabled the schedule.
                        self._owned_device = device
                    try:
                        self._get(host, 'setScheduleStorage', {'uid': uid, 'enable': int(action == 'start')})
                    except _CameraError:
                        # Only an independent read can resolve an ambiguous write.
                        pass
                    enabled = self._schedule(host, uid)
                if not enabled:
                    self._owned_device = None
                detail = '相機卡定時錄影排程已開啟，寫卡狀態待確認' if enabled else '相機卡定時錄影排程已關閉'
                ok = action == 'refresh' or enabled == (action == 'start')
                if not ok:
                    detail = '相機讀回的排程與指令不符。' + detail
                self._publish(state='schedule_enabled' if enabled else 'idle', available=True,
                              schedule_enabled=enabled, detail=detail)
                if action == 'refresh':
                    try:
                        clip = self._recent_clip(host, uid)
                        self._publish(last_card_clip=clip, clip_query_detail=(
                            '相機卡當日最近的定時錄影片段' if clip else '當日尚未查到有資料的相機卡定時片段'))
                    except _CameraError as exc:
                        self._publish(last_card_clip=None, clip_query_detail=str(exc))
                return ok, detail
            except _CameraError as exc:
                self._publish(state='unavailable', available=False, schedule_enabled=None,
                              detail=str(exc), last_card_clip=None, clip_query_detail='')
                return False, str(exc)

    def recover(self):
        with self._operation:
            self._closing = False
        # The camera retains its own schedule; startup never writes to it.

    def follow(self, decision, now):
        pass  # A tracking decision is independent of the owner's Web recording control.

    def cancel_tracking(self, _reason=''):
        pass

    def set_continuous(self, enabled):
        return False, '相機卡錄影由開始與停止按鈕控制'

    def stop_all(self):
        with self._operation:
            if self._owned_device is not None:
                self.command('stop')

    def close(self):
        with self._operation:
            self._closing = True
            self.stop_all()

    def recordings(self):
        return []  # The legacy transfer API lists only completed Jetson clips.

    def file(self, recording_id):
        raise ValueError('相機卡影片未提供 Jetson 本機檔案')

    def acknowledge(self, recording_id, state):
        raise ValueError('相機卡影片未加入上傳佇列')
