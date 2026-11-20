"""Run actual Qt event routing with the production API and simulated output."""
import os
from pathlib import Path
import subprocess
import threading
import unittest

from test_keyboard_jog import setup
from shore_tracker.preview import PreviewStore
from shore_tracker.web.server import make_server


def test_native_held_arrows_release_focus_and_input_guards(tmp_path):
    binary = os.environ.get('SHORE_NATIVE_KEYBOARD_TEST_BINARY')
    if not binary or not Path(binary).is_file():
        raise unittest.SkipTest('Qt keyboard UI harness not built on this host')
    _, coordinator, controller, _, _, _ = setup()
    actions = []
    phase = tmp_path / 'speed-input-phase'
    phase.write_text('waiting')
    original = controller.keyboard_command
    def command(action, values):
        if action == 'speed':
            assert phase.read_text() == 'typing', 'speed must apply before Enter or focus loss'
        actions.append((action, values.copy()))
        result = original(action, values)
        if action == 'speed': phase.write_text('applied')
        return result
    controller.keyboard_command = command
    controller.arm(); controller.set_inference_enabled(False)
    server = make_server(('127.0.0.1', 0), controller.status, controller, PreviewStore())
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        subprocess.run([binary, f'http://127.0.0.1:{server.server_port}', str(phase)],
            env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'}, check=True, timeout=12)
        assert [v['direction'] for a,v in actions if a == 'start'] == ['right', 'up']
        assert len([a for a,_ in actions if a == 'move']) >= 15
        assert len([a for a,_ in actions if a == 'stop']) == 2
        assert [v['speed_deg_s'] for a,v in actions if a == 'speed'] == [2]
        assert controller.keyboard_snapshot()['speed_deg_s'] == 2
        assert not coordinator.keyboard_snapshot()['moving']
    finally:
        server.shutdown(); server.server_close(); thread.join(2)
