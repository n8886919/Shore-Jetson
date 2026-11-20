from __future__ import annotations

import json
import errno
import mimetypes
import os
import re
import secrets
import shutil
import socket
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address, ip_network
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen
from urllib.error import URLError

from shore_tracker.controller import AppController
from shore_tracker.preview import PreviewStore
from shore_tracker.status import StatusStore
from shore_tracker.web.desktop import set_kiosk_compact
from shore_tracker.web.file_browser import CardFiles, byte_range


MAX_REQUEST_BODY_BYTES = 4096
_LAN_NETWORKS = tuple(ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def _validated_loopback_address(
    address: tuple[str, int], *, allow_lan: bool = False,
) -> tuple[tuple[str, int], socket.AddressFamily]:
    try:
        host, port = address
    except (TypeError, ValueError) as exc:
        raise ValueError("dashboard bind address must be a host/port pair") from exc
    if not isinstance(host, str):
        raise ValueError("dashboard bind host must be a loopback address")

    # Do not leave the safety decision to name resolution.  Keep the documented
    # localhost convenience, but turn it into a numeric loopback before bind().
    if host.lower() == "localhost":
        host = "127.0.0.1"
    try:
        parsed = ip_address(host)
    except ValueError as exc:
        raise ValueError("dashboard bind host must be a loopback address") from exc
    if not parsed.is_loopback and not (allow_lan and host == "0.0.0.0"):
        raise ValueError("dashboard bind host must be a loopback address")

    family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
    return (str(parsed), port), family


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        status: StatusStore,
        controller: AppController,
        preview: PreviewStore,
        preview_stale_seconds: float,
        allow_lan: bool = False,
    ) -> None:
        address, self.address_family = _validated_loopback_address(address, allow_lan=allow_lan)
        self.allow_lan = allow_lan
        self.status_store = status
        self.controller = controller
        self.preview_store = preview
        self.preview_stale_seconds = preview_stale_seconds
        self.pan_tilt_token = secrets.token_urlsafe(32)
        self.card_files = CardFiles()
        super().__init__(address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Avoid accidental query-string/credential logging. Production logging is
        # attached at the application layer with explicit redaction.
        return

    def _valid_loopback_host(self) -> bool:
        value = self.headers.get("Host", "").strip().lower()
        if value.startswith("["):
            closing = value.find("]")
            if closing < 0:
                return False
            hostname = value[: closing + 1]
            suffix = value[closing + 1 :]
            if suffix and (not suffix.startswith(":") or not suffix[1:].isdigit()):
                return False
        else:
            hostname, separator, port = value.rpartition(":")
            if not separator or not port.isdigit():
                hostname = value
        if hostname in {"127.0.0.1", "localhost", "[::1]"}:
            return True
        return self.server.allow_lan and hostname == self.connection.getsockname()[0]

    def _reject_nonlocal_host(self) -> bool:
        peer = ip_address(self.client_address[0])
        private_peer = (
            peer.is_loopback
            or any(peer in network for network in _LAN_NETWORKS)
            or (self.server.allow_lan and peer in ip_network("100.64.0.0/10"))
        )
        if private_peer and self._valid_loopback_host():
            return False
        self._json(
            HTTPStatus.MISDIRECTED_REQUEST,
            {"ok": False, "detail": "dashboard Host 必須是 loopback"},
        )
        return True

    def _json(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        close_connection: bool = False,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        if close_connection:
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if close_connection:
            self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _consume_bounded_request_body(self) -> bool:
        # Chunked input and ambiguous Content-Length values are unnecessary for
        # these tiny local control requests and unsafe to leave unread on a
        # persistent connection.
        if self.headers.get("Transfer-Encoding") is not None:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "detail": "不支援 Transfer-Encoding"},
                close_connection=True,
            )
            return False

        values = self.headers.get_all("Content-Length", [])
        if len(values) > 1:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "detail": "Content-Length 無效"},
                close_connection=True,
            )
            return False

        raw_length = values[0].strip() if values else "0"
        if not raw_length or not raw_length.isascii() or not raw_length.isdigit():
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "detail": "Content-Length 無效"},
                close_connection=True,
            )
            return False

        normalized_length = raw_length.lstrip("0") or "0"
        maximum = str(32768 if urlsplit(self.path).path == "/api/stream/webrtc" else MAX_REQUEST_BODY_BYTES)
        if len(normalized_length) > len(maximum) or (
            len(normalized_length) == len(maximum)
            and normalized_length > maximum
        ):
            self._json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"ok": False},
                close_connection=True,
            )
            return False
        length = int(normalized_length, 10)
        self._request_body = self.rfile.read(length) if length else b""
        return True

    def _json_body(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self._request_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _static(self, name: str) -> None:
        if name not in {"index.html", "mobile.html", "app.css", "app.js", "kiosk.css", "kiosk.js", "streaming.js", "advanced.html", "advanced.css", "advanced.js", "pan-tilt.js", "servo-test.js", "files.html", "files.css", "files.js", "inference-toggle.js", "keyboard-jog.js"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = files("shore_tracker.web.static").joinpath(name).read_bytes()
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self'; media-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self._reject_nonlocal_host():
            return
        path = urlsplit(self.path).path
        if path in {"/", "/kiosk", "/kiosk/"}:
            self._static("index.html")
        elif path in {'/files', '/files/'}:
            self._static('files.html')
        elif path in {'/files.css', '/files.js'}:
            self._static(path[1:])
        elif path == '/api/files':
            self._card_listing()
        elif path == '/api/files/content':
            self._card_file()
        elif path in {"/mobile", "/mobile/"}:
            self._static("mobile.html")
        elif path in {"/control", "/control/", "/advanced", "/advanced/"}:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/kiosk?controls=1")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path in {"/advanced.css", "/advanced.js", "/kiosk.css", "/kiosk.js", "/streaming.js", "/pan-tilt.js", "/servo-test.js", "/inference-toggle.js", "/keyboard-jog.js"}:
            self._static(path[1:])
        elif path == "/api/stream":
            encoder = getattr(self.server, "webrtc_preview", None)
            self._json(HTTPStatus.OK, {"webrtc": encoder is not None,
                                      "encoder_active": bool(encoder and encoder.active)})
        elif path == "/api/preview.ts":
            encoder = getattr(self.server, 'webrtc_preview', None)
            if not ip_address(self.client_address[0]).is_loopback or encoder is None:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            encoder.stream(self)
        elif path == "/api/local-display":
            if not ip_address(self.client_address[0]).is_loopback:
                self._json(HTTPStatus.FORBIDDEN, {"ok": False})
                return
            display = getattr(self.server, 'local_display', None)
            self._json(HTTPStatus.OK, {"path": str(display.path) if display and display.path else None,
                "camera_interface": getattr(self.server, 'camera_interface', 'eth0'),
                "web_port": self.server.server_port, "lan_access": self.server.allow_lan})
        elif path == "/api/diagnostics":
            self._json(HTTPStatus.OK, self.server.controller.diagnostics())
        elif path == "/api/pan-tilt":
            self._json(HTTPStatus.OK, {**self.server.controller.pan_tilt_snapshot(),
                       "control_token": self.server.pan_tilt_token,
                       "offline_preview": self.server.status_store.snapshot().get("offline_preview", False)})
        elif path == '/api/keyboard':
            self._json(HTTPStatus.OK, {**self.server.controller.keyboard_snapshot(),
                       'control_token': self.server.pan_tilt_token})
        elif path == '/api/servo-test':
            self._json(HTTPStatus.OK, {**self.server.controller.servo_test_snapshot(),
                       'control_token': self.server.pan_tilt_token,
                       'offline_preview': self.server.status_store.snapshot().get('offline_preview', False)})
        elif path == "/app.css":
            self._static("app.css")
        elif path == "/app.js":
            self._static("app.js")
        elif path == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True})
        elif path == "/api/status":
            snapshot = self.server.status_store.snapshot()
            snapshot['metrics']['preview_fps'] = self.server.preview_store.frame_rate(
                stale_seconds=self.server.preview_stale_seconds)
            recorder = self.server.controller.recording
            snapshot['keyboard'] = self.server.controller.keyboard_snapshot()
            snapshot['manual_recording'] = recorder.snapshot() if recorder else {'state': 'unavailable'}
            self._json(HTTPStatus.OK, snapshot)
        elif path == "/api/preview.mjpeg":
            self._mjpeg()
        elif path == "/api/display-frame":
            self._display_frame()
        elif path == "/api/recordings":
            recorder = self.server.controller.recording
            self._json(HTTPStatus.OK, {'recordings': recorder.recordings() if recorder else []})
        elif re.fullmatch(r'/api/recordings/[0-9a-f]{32}/file', path):
            try:
                file = self.server.controller.recording.file(path.split('/')[3])
                handle = file.open('rb')
            except (OSError, ValueError, AttributeError):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            with handle:
                self.send_response(HTTPStatus.OK)
                self.send_header('Content-Type', 'video/x-matroska')
                self.send_header('Content-Length', str(file.stat().st_size))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                try: shutil.copyfileobj(handle, self.wfile, 1024 * 1024)
                except (BrokenPipeError, ConnectionResetError): pass
        elif path == "/api/camera":
            payload = self.server.controller.camera_snapshot()
            self._json(HTTPStatus.OK if payload["ok"] else HTTPStatus.SERVICE_UNAVAILABLE, payload)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:  # noqa: N802
        if self._reject_nonlocal_host():
            return
        if urlsplit(self.path).path == '/api/files/content':
            self._card_file(head_only=True)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _card_error(self, error: Exception) -> None:
        if isinstance(error, ValueError):
            status, detail = HTTPStatus.BAD_REQUEST, '檔案路徑或頁碼無效'
        elif isinstance(error, (FileNotFoundError, NotADirectoryError)):
            status, detail = HTTPStatus.NOT_FOUND, '找不到這個檔案或資料夾'
        elif isinstance(error, PermissionError) or getattr(error, 'errno', None) == errno.ELOOP:
            status, detail = HTTPStatus.FORBIDDEN, '無法讀取這個項目'
        else:
            status, detail = HTTPStatus.SERVICE_UNAVAILABLE, '無法讀取記憶卡，請確認卡片已掛載'
        self._json(status, {'ok': False, 'detail': detail})

    def _card_listing(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        try:
            result = self.server.card_files.listing(query.get('path', [''])[0],
                                                    int(query.get('offset', ['0'])[0]))
        except (OSError, ValueError) as error:
            self._card_error(error)
            return
        self._json(HTTPStatus.OK, result)

    def _card_file(self, *, head_only: bool = False) -> None:
        query = parse_qs(urlsplit(self.path).query)
        relative = query.get('path', [''])[0]
        headers_sent = False
        try:
            with self.server.card_files.open_path(relative) as (fd, info):
                try:
                    start, end, partial = byte_range(self.headers.get('Range'), info.st_size)
                except ValueError:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header('Content-Range', f'bytes */{info.st_size}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                name = relative.rsplit('/', 1)[-1]
                kind = mimetypes.guess_type(name)[0] or 'application/octet-stream'
                inline = kind.startswith('video/') and query.get('download', ['0'])[0] != '1'
                self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
                self.send_header('Content-Type', kind if inline else 'application/octet-stream')
                self.send_header('Content-Disposition', ('inline' if inline else 'attachment') +
                                 "; filename*=UTF-8''" + quote(name, safe=''))
                self.send_header('Content-Length', str(max(0, end - start + 1)))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('X-Frame-Options', 'DENY')
                if partial:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{info.st_size}')
                self.end_headers()
                headers_sent = True
                if head_only:
                    return
                os.lseek(fd, start, os.SEEK_SET)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = os.read(fd, min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
                if remaining > 0:
                    self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except (OSError, ValueError) as error:
            if headers_sent:
                self.close_connection = True
            else:
                self._card_error(error)

    def _display_frame(self):
        try:
            after = float(parse_qs(urlsplit(self.path).query).get('after', ['0'])[0])
            if not 0 <= after < float('inf'):
                raise ValueError()
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        frame = self.server.preview_store.wait_after(after, timeout=.4)
        if frame is None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header('Content-Length', '0')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return
        status = self.server.status_store.snapshot()
        recognition = status['recognition']
        recognized = status['inference_run_id'] is not None and recognition['available']
        metadata = {'cursor': frame.sequence, 'run_id': status['inference_run_id'],
                    'recognized': recognized,
                    'recognition': recognition if recognized else None,
                    'delay_ms': max(0, round((time.monotonic() - frame.captured_monotonic) * 1000)),
                    'pipeline_delay_ms': frame.pipeline_delay_ms}
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(frame.jpeg)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Frame-Meta', json.dumps(metadata, separators=(',', ':')))
        self.end_headers()
        try:
            self.wfile.write(frame.jpeg)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self._reject_nonlocal_host():
            return
        path = urlsplit(self.path).path
        if not self._consume_bounded_request_body():
            return
        origin = self.headers.get('Origin')
        if ((origin is not None and origin != 'http://' + self.headers.get('Host', ''))
                or (origin is None and not ip_address(self.client_address[0]).is_loopback)):
            self._json(HTTPStatus.FORBIDDEN, {'ok': False})
            return
        if path == "/api/stream/webrtc":
            if getattr(self.server, 'webrtc_preview', None) is None or self.headers.get_content_type() != 'application/sdp':
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
                return
            offer = self._request_body
            try:
                request = Request('http://127.0.0.1:1984/api/webrtc?src=preview', data=offer,
                                  headers={'Content-Type': 'application/sdp'})
                with urlopen(request, timeout=8) as response:
                    answer = response.read(32769)
                if len(answer) > 32768:
                    raise ValueError('SDP too large')
            except (OSError, ValueError, URLError):
                self.send_error(HTTPStatus.BAD_GATEWAY)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header('Content-Type', 'application/sdp')
            self.send_header('Content-Length', str(len(answer)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(answer)
            return
        if path == '/api/keyboard':
            if (self.headers.get_content_type() != 'application/json'
                    or not secrets.compare_digest(self.headers.get('X-Pan-Tilt-Token', ''), self.server.pan_tilt_token)):
                self._json(HTTPStatus.FORBIDDEN, {'ok': False})
                return
            payload = self._json_body()
            fields = {'start': {'epoch', 'direction'}, 'move': {'session', 'sequence', 'direction'},
                      'stop': {'epoch'}, 'speed': {'speed_deg_s'}}
            action = payload.get('action') if payload else None
            if (not isinstance(action, str) or action not in fields
                    or set(payload) - {'action'} != fields[action]):
                self._json(HTTPStatus.BAD_REQUEST, {'ok': False})
                return
            try:
                result = self.server.controller.keyboard_command(action,
                    {key: value for key, value in payload.items() if key != 'action'})
            except (TypeError, ValueError) as exc:
                self._json(HTTPStatus.CONFLICT, {'ok': False, 'detail': str(exc)})
                return
            except OSError:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {'ok': False, 'detail': '舵機控制失敗，請停止追蹤'})
                return
            self._json(HTTPStatus.OK, result)
            return
        if path == '/api/servo-test':
            origin = self.headers.get('Origin')
            if ((origin is not None and origin != 'http://' + self.headers.get('Host', ''))
                    or self.headers.get_content_type() != 'application/json'
                    or not secrets.compare_digest(self.headers.get('X-Pan-Tilt-Token', ''), self.server.pan_tilt_token)):
                self._json(HTTPStatus.FORBIDDEN, {'ok': False, 'detail': '請重新開啟設定頁'})
                return
            payload = self._json_body()
            fields = {'start': {'epoch'}, 'heartbeat': {'session', 'sequence'}, 'stop': set()}
            action = payload.get('action') if payload else None
            if (not isinstance(action, str) or action not in fields
                    or set(payload) - {'action'} != fields[action]):
                self._json(HTTPStatus.BAD_REQUEST, {'ok': False, 'detail': '測試指令格式無效'})
                return
            try:
                result = self.server.controller.servo_test_command(action,
                    {key: value for key, value in payload.items() if key != 'action'})
            except (TypeError, ValueError) as exc:
                self._json(HTTPStatus.CONFLICT, {'ok': False, 'detail': str(exc)})
                return
            except (OSError, ImportError):
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {'ok': False, 'detail': '舵機串口操作失敗，測試已中止'})
                return
            self._json(HTTPStatus.OK, result)
            return
        elif path == "/api/pan-tilt":
            origin = self.headers.get("Origin")
            if ((origin is not None and origin != "http://" + self.headers.get("Host", ""))
                    or self.headers.get_content_type() != "application/json"
                    or not secrets.compare_digest(self.headers.get("X-Pan-Tilt-Token", ""), self.server.pan_tilt_token)):
                self._json(HTTPStatus.FORBIDDEN, {"ok": False, "detail": "請重新開啟主設定頁"})
                return
            payload = self._json_body()
            fields = {"connect": set(), "disconnect": set(), "arm": {"epoch"},
                      "move": {"session", "sequence", "direction", "speed"},
                      "stop": {"session", "sequence"}, "disarm": {"session", "sequence"}}
            action = payload.get("action") if payload else None
            if (not isinstance(action, str) or action not in fields
                    or set(payload) - {"action"} != fields[action]):
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "detail": "雲台指令格式無效"})
                return
            try:
                result = self.server.controller.pan_tilt_command(action,
                    {key: value for key, value in payload.items() if key != "action"})
            except (TypeError, ValueError) as exc:
                self._json(HTTPStatus.CONFLICT, {"ok": False, "detail": str(exc)})
                return
            except (OSError, ImportError):
                self._json(HTTPStatus.SERVICE_UNAVAILABLE,
                           {"ok": False, "detail": "雲台串口操作失敗，請檢查連接與停止狀態"})
                return
            self._json(HTTPStatus.OK, result)
        elif path == "/api/kiosk/window":
            if not ip_address(self.client_address[0]).is_loopback:
                self._json(HTTPStatus.FORBIDDEN, {"ok": False})
                return
            origin = self.headers.get("Origin")
            if ((origin is not None and origin != "http://" + self.headers.get("Host", ""))
                    or self.headers.get_content_type() != "application/json"):
                self._json(HTTPStatus.FORBIDDEN, {"ok": False})
                return
            payload = self._json_body()
            if payload is None or set(payload) != {"compact"} or type(payload["compact"]) is not bool:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False})
                return
            try:
                result = set_kiosk_compact(payload["compact"])
            except RuntimeError as exc:
                self._json(HTTPStatus.CONFLICT, {"ok": False, "detail": str(exc)})
                return
            self._json(HTTPStatus.OK, {"ok": True, **result})
        elif path in {"/api/recording", "/api/recording/mode", "/api/recordings/ack"}:
            origin = self.headers.get('Origin')
            if (origin is not None and origin != 'http://' + self.headers.get('Host', '')) or self.headers.get_content_type() != 'application/json':
                self._json(HTTPStatus.FORBIDDEN, {'ok': False})
                return
            payload = self._json_body()
            if path == '/api/recording/mode':
                if payload is None or set(payload) != {'continuous'} or type(payload['continuous']) is not bool:
                    self._json(HTTPStatus.BAD_REQUEST, {'ok': False})
                    return
                ok, detail = self.server.controller.set_recording_mode(payload['continuous'])
                self._json(HTTPStatus.OK if ok else HTTPStatus.CONFLICT, {'ok': ok, 'detail': detail})
            elif path == '/api/recording':
                if payload is None or set(payload) != {'action'} or not isinstance(payload['action'], str) or payload['action'] not in {'start', 'stop'}:
                    self._json(HTTPStatus.BAD_REQUEST, {'ok': False})
                    return
                ok, detail = self.server.controller.recording_command(payload['action'])
                self._json(HTTPStatus.OK if ok else HTTPStatus.CONFLICT, {'ok': ok, 'detail': detail})
            else:
                try:
                    if payload is None or set(payload) != {'id', 'state'} or not all(isinstance(v, str) for v in payload.values()):
                        raise ValueError('invalid ack')
                    self.server.controller.recording.acknowledge(payload['id'], payload['state'])
                except (OSError, ValueError, AttributeError):
                    self._json(HTTPStatus.BAD_REQUEST, {'ok': False})
                    return
                self._json(HTTPStatus.OK, {'ok': True})
        elif path == "/api/camera/reconnect":
            origin = self.headers.get("Origin")
            if ((origin is not None and origin != "http://" + self.headers.get("Host", ""))
                    or self.headers.get_content_type() != "application/json"):
                self._json(HTTPStatus.FORBIDDEN, {"ok": False})
                return
            if self._json_body() != {}:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False})
                return
            ok, detail = self.server.controller.reconnect_camera()
            self._json(HTTPStatus.ACCEPTED if ok else HTTPStatus.CONFLICT,
                       {"ok": ok, "detail": detail})
        elif path == "/api/arm":
            ok, detail = self.server.controller.arm()
            self._json(
                HTTPStatus.OK if ok else HTTPStatus.CONFLICT,
                {"ok": ok, "detail": detail},
            )
        elif path == "/api/stop":
            ok, detail = self.server.controller.stop()
            self._json(HTTPStatus.OK, {"ok": ok, "detail": detail})
        elif path == "/api/camera":
            origin = self.headers.get("Origin")
            expected_origin = "http://" + self.headers.get("Host", "")
            if (origin is not None and origin != expected_origin) or self.headers.get_content_type() != "application/json":
                self._json(HTTPStatus.FORBIDDEN, {"ok": False, "detail": "相機控制只接受本機頁面的 JSON 請求"})
                return
            payload = self._json_body()
            if (payload is None or set(payload) != {"action", "values"}
                    or not isinstance(payload["action"], str)
                    or not isinstance(payload["values"], dict)):
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "detail": "相機指令格式無效"})
                return
            ok, detail = self.server.controller.camera_command(payload["action"], payload["values"])
            self._json(HTTPStatus.OK if ok else HTTPStatus.CONFLICT, {"ok": ok, "detail": detail})
        elif path == "/api/inference":
            payload = self._json_body()
            if (self.headers.get_content_type() != 'application/json' or payload is None
                    or set(payload) != {'enabled'} or type(payload['enabled']) is not bool):
                self._json(HTTPStatus.BAD_REQUEST, {'ok': False, 'detail': 'AI 辨識設定格式無效'})
                return
            ok, detail = self.server.controller.set_inference_enabled(payload['enabled'])
            self._json(HTTPStatus.OK if ok else HTTPStatus.CONFLICT,
                       {'ok': ok, 'detail': detail, 'inference': self.server.status_store.snapshot()['inference']})
        elif path == "/api/inference/rate":
            origin = self.headers.get("Origin")
            if ((origin is not None and origin != "http://" + self.headers.get("Host", ""))
                    or self.headers.get_content_type() != "application/json"):
                self._json(HTTPStatus.FORBIDDEN, {"ok": False})
                return
            payload = self._json_body()
            value = payload.get("max_fps") if payload is not None else None
            if (payload is None or set(payload) != {"max_fps"}
                    or type(value) not in {int, float} or not (value == 0 or 1 <= value <= 10)):
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "detail": "辨識頻率必須為 0（不限速）或介於 1–10 Hz"})
                return
            ok, detail = self.server.controller.set_inference_rate(value)
            self._json(HTTPStatus.OK if ok else HTTPStatus.CONFLICT,
                       {"ok": ok, "detail": detail, "max_fps": float(value) if ok else None})
        elif path == "/api/tuning":
            payload = self._json_body()
            required = {"pan_gain_deg", "pan_deadband", "pan_direction", "zoom_maximum"}
            if (payload is None or not required <= set(payload)
                    or set(payload) - required - {"tilt_enabled"}
                    or ("tilt_enabled" in payload and type(payload["tilt_enabled"]) is not bool)):
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "detail": "invalid tuning payload"},
                )
                return
            ok, detail = self.server.controller.tune(
                pan_gain_deg=payload["pan_gain_deg"],
                pan_deadband=payload["pan_deadband"],
                pan_direction=payload["pan_direction"],
                zoom_maximum=payload["zoom_maximum"],
                **({"tilt_enabled": payload["tilt_enabled"]} if "tilt_enabled" in payload else {}),
            )
            self._json(
                HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST,
                {"ok": ok, "detail": detail},
            )
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _mjpeg(self) -> None:
        modes = parse_qs(urlsplit(self.path).query, keep_blank_values=True).get("mode", ["web"])
        if len(modes) != 1 or modes[0] not in {"web", "local"}:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid preview mode")
            return
        interval = 0.2 if modes[0] == "web" else 0.0
        next_send = 0.0
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type", "multipart/x-mixed-replace; boundary=shoreframe"
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        sequence = 0
        try:
            while True:
                # Each client samples the latest frame; never queue old camera frames.
                delay = next_send - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                frame = self.server.preview_store.wait_after(sequence, 2.0)
                if frame is None:
                    continue
                sequence = frame.sequence
                if (
                    time.monotonic() - frame.captured_monotonic
                    > self.server.preview_stale_seconds
                ):
                    continue
                header = (
                    b"--shoreframe\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame.jpeg)}\r\n\r\n".encode()
                )
                self.wfile.write(header)
                self.wfile.write(frame.jpeg)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                next_send = time.monotonic() + interval
        except (BrokenPipeError, ConnectionResetError):
            return


def make_server(
    address: tuple[str, int],
    status: StatusStore,
    controller: AppController,
    preview: PreviewStore,
    *,
    preview_stale_seconds: float = 2.0,
    allow_lan: bool = False,
) -> DashboardServer:
    return DashboardServer(
        address,
        status,
        controller,
        preview,
        preview_stale_seconds,
        allow_lan,
    )
