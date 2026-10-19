from __future__ import annotations

import json
import mimetypes
import re
import shutil
import socket
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlsplit

from shore_tracker.controller import AppController
from shore_tracker.preview import PreviewStore
from shore_tracker.status import StatusStore


MAX_REQUEST_BODY_BYTES = 4096


def _validated_loopback_address(
    address: tuple[str, int],
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
    if not parsed.is_loopback:
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
    ) -> None:
        address, self.address_family = _validated_loopback_address(address)
        self.status_store = status
        self.controller = controller
        self.preview_store = preview
        self.preview_stale_seconds = preview_stale_seconds
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
        return hostname in {"127.0.0.1", "localhost", "[::1]"}

    def _reject_nonlocal_host(self) -> bool:
        if self._valid_loopback_host():
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
        maximum = str(MAX_REQUEST_BODY_BYTES)
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
        if name not in {"index.html", "mobile.html", "app.css", "app.js", "kiosk.css", "kiosk.js", "advanced.html", "advanced.css", "advanced.js"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = files("shore_tracker.web.static").joinpath(name).read_bytes()
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
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
        elif path in {"/mobile", "/mobile/"}:
            self._static("mobile.html")
        elif path in {"/control", "/control/", "/advanced", "/advanced/"}:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/kiosk?controls=1")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path in {"/advanced.css", "/advanced.js", "/kiosk.css", "/kiosk.js"}:
            self._static(path[1:])
        elif path == "/api/diagnostics":
            self._json(HTTPStatus.OK, self.server.controller.diagnostics())
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
            snapshot['manual_recording'] = recorder.snapshot() if recorder else {'state': 'unavailable'}
            self._json(HTTPStatus.OK, snapshot)
        elif path == "/api/preview.mjpeg":
            self._mjpeg()
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

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self._reject_nonlocal_host():
            return
        path = urlsplit(self.path).path
        if not self._consume_bounded_request_body():
            return
        if path in {"/api/recording", "/api/recordings/ack"}:
            origin = self.headers.get('Origin')
            if (origin is not None and origin != 'http://' + self.headers.get('Host', '')) or self.headers.get_content_type() != 'application/json':
                self._json(HTTPStatus.FORBIDDEN, {'ok': False})
                return
            payload = self._json_body()
            if path == '/api/recording':
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
        elif path == "/api/inference/rate":
            origin = self.headers.get("Origin")
            if ((origin is not None and origin != "http://" + self.headers.get("Host", ""))
                    or self.headers.get_content_type() != "application/json"):
                self._json(HTTPStatus.FORBIDDEN, {"ok": False})
                return
            payload = self._json_body()
            value = payload.get("max_fps") if payload is not None else None
            if (payload is None or set(payload) != {"max_fps"}
                    or type(value) not in {int, float} or not 1 <= value <= 10):
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "detail": "辨識頻率必須介於 1–10 Hz"})
                return
            ok, detail = self.server.controller.set_inference_rate(value)
            self._json(HTTPStatus.OK if ok else HTTPStatus.CONFLICT,
                       {"ok": ok, "detail": detail, "max_fps": float(value) if ok else None})
        elif path == "/api/tuning":
            payload = self._json_body()
            if payload is None or set(payload) != {
                "pan_gain_deg", "pan_deadband", "pan_direction", "zoom_maximum"
            }:
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
) -> DashboardServer:
    return DashboardServer(
        address,
        status,
        controller,
        preview,
        preview_stale_seconds,
    )
