"""Run the real Qt switch against a fake backend; never controls installed hardware."""
import json
import os
from pathlib import Path
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def test_native_inference_default_manual_and_recording_lock(tmp_path):
    binary = os.environ.get('SHORE_NATIVE_INFERENCE_TEST_BINARY')
    if not binary or not Path(binary).is_file():
        raise unittest.SkipTest('Qt inference UI harness not built on this host')
    flag = tmp_path / 'recording-state'
    flag.write_text('unlocked')
    enabled = [False]
    actions = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def reply(self, value):
            body = json.dumps(value).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass
        def do_GET(self):
            if self.path == '/api/servo-test': return self.reply({'available': False})
            if self.path == '/api/diagnostics': return self.reply({'recovery': {'state': 'ready'}})
            if self.path == '/api/local-display': return self.reply({})
            locked = flag.read_text() == 'locked'
            if locked: enabled[0] = True
            self.reply({'tracking': {'armed': False, 'mode': 'disarmed'}, 'tuning': {},
                'arm_allowed': True, 'components': {'inference': {'state': 'idle'}, 'servo': {'state': 'ready'}},
                'inference': {'enabled': enabled[0], 'locked': locked}})
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            actions.append((self.path, payload))
            assert self.path == '/api/inference'
            assert flag.read_text() == 'unlocked', 'native UI must refuse clicks during recording'
            enabled[0] = payload['enabled']
            self.reply({'ok': True})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        subprocess.run([binary, f'http://127.0.0.1:{server.server_port}', str(flag)],
            env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'}, check=True, timeout=12)
        assert actions == [('/api/inference', {'enabled': True}), ('/api/inference', {'enabled': False})]
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


if __name__ == '__main__':
    import tempfile
    with tempfile.TemporaryDirectory() as path:
        test_native_inference_default_manual_and_recording_lock(Path(path))
    print('Native AI toggle: default off, enable/disable readback and recording lock passed')
