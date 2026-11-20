"""Run on a host with the prebuilt Qt harness; every HTTP action is simulated."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def test_native_progress_completion_recovery_and_late_cancel():
    binary = os.environ.get('SHORE_NATIVE_UI_TEST_BINARY')
    if not binary or not Path(binary).is_file():
        raise unittest.SkipTest('Qt UI harness not built on this host')
    start = time.monotonic()
    state = dict(running=False, connected=False, available=True, calibrated=True,
                 epoch=0, notice='水平 M0、垂直 M1；按下後兩軸先置中', phase='idle',
                 tracking_active=False, control_token='fake-token')
    actions, heartbeat, starts = [], [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, data, code=200):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The bounded Qt harness can exit with a GET in flight.

        def do_GET(self):
            if self.path == '/api/servo-test':
                return self.reply(state.copy())
            if self.path == '/api/diagnostics':
                return self.reply({'recovery': {'state': 'queued' if time.monotonic()-start < 1.2 else 'ready'}})
            self.reply({'tracking': {'armed': False, 'mode': 'disarmed'}, 'tuning': {},
                        'components': {'servo': {'state': 'ready'}}, 'arm_allowed': True})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert self.path == '/api/servo-test'
            assert self.headers['X-Pan-Tilt-Token'] == 'fake-token'
            action = body['action']
            actions.append((time.monotonic()-start, action))
            if action == 'start':
                starts.append(body)
                if len(starts) == 3:
                    return self.reply({'ok': False, 'detail': '請先結束錄影並等待收尾'}, 409)
                state.update(running=True, connected=True, notice='兩舵機置中', phase='center')
                result = {'ok': True, 'session': str(len(starts)), 'status': state.copy()}
                if len(starts) == 2:
                    time.sleep(.4)  # A dismiss/STOP overtakes the start response.
                return self.reply(result)
            if action == 'heartbeat':
                heartbeat.append(body)
                state.update(notice='抬頭 20°', phase='up')
                if len(starts) == 1 and len(heartbeat) >= 4:
                    state.update(running=False, connected=False, notice='測試指令完成', phase='complete', epoch=1)
            elif action == 'stop':
                state.update(running=False, connected=False, notice='測試已停止', phase='stopped', epoch=2)
            self.reply({'ok': True, 'status': state.copy()})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        subprocess.run([binary, f'http://127.0.0.1:{server.server_port}'],
                       env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'}, check=True, timeout=12)
        assert len(starts) == 4
        assert actions[0][0] >= .9 and actions[0][1] == 'start', 'Loading the UI never sends commands'
        assert any(action == 'stop' for _, action in actions)
        assert all(item['session'] in {'1', '4'} for item in heartbeat), 'Late/rejected session never gains a heartbeat'
        for session in {'1', '4'}:
            sequences = [item['sequence'] for item in heartbeat if item['session'] == session]
            assert sequences == list(range(1, len(sequences)+1))
        assert not state['running']
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
