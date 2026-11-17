#!/usr/bin/env python3
"""View the existing dashboard locally without hardware or an SSH connection."""
import argparse
from importlib.resources import files
import mimetypes
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shore_tracker.controller import AppController
from shore_tracker.preview import PreviewStore
from shore_tracker.status import StatusStore
from shore_tracker.web.server import DashboardHandler, make_server


class OfflineStatus(StatusStore):
    def snapshot(self):
        data = super().snapshot()
        data.update(offline_preview=True, arm_allowed=False,
                    arm_reason='離線預覽：數值為版面預設，非 Jetson 即時狀態')
        return data


class OfflineHandler(DashboardHandler):
    def _static(self, name):
        if name not in {'index.html', 'mobile.html', 'app.js'}:
            return super()._static(name)
        text = files('shore_tracker.web.static').joinpath(name).read_text()
        if name in {'index.html', 'mobile.html'}:
            text = text.replace('</title>', ' · 離線預覽</title>')
        else:
            text = text.replace("'DISARMED'", "'離線預覽'")
            text = text.replace('等待相機畫面', '離線預覽 · 未連接 Jetson 相機')
        body = text.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(name)[0]+'; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self._reject_nonlocal_host(): return
        path = urlsplit(self.path).path
        if path == '/api/preview.mjpeg':
            self._json(404, {'ok': False, 'detail': '離線預覽沒有即時影像'})
        elif path == '/api/camera':
            self._json(200, {'ok': False, 'detail': '離線預覽：未讀取實體相機設定'})
        else:
            super().do_GET()

    def do_POST(self):
        self._json(403, {'ok': False, 'detail': '離線預覽不執行控制指令'}, close_connection=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18092)
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    status = OfflineStatus()
    for component in status.COMPONENTS:
        status.update_component(component, 'offline', '離線預覽')
    status.update_tuning(pan_gain_deg=45, pan_deadband=.20, pan_direction=-1, zoom_maximum=1)
    server = make_server(('127.0.0.1', args.port), status, AppController(status), PreviewStore())
    server.RequestHandlerClass = OfflineHandler
    url = f'http://127.0.0.1:{server.server_port}'
    print(url, flush=True)
    if args.open:
        subprocess.Popen(['xdg-open', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
