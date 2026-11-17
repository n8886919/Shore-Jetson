import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from shore_tracker.web import desktop


def test_only_the_dedicated_parent_browser_is_selected(tmp_path, monkeypatch):
    profile = '--user-data-dir=' + str(tmp_path / '.local/share/shore-tracker/browser')
    for pid, argv in [(11, ['/usr/bin/chromium-browser', '--user-data-dir=/other']),
                      (22, ['/usr/bin/chromium-browser', profile, '--type=renderer']),
                      (33, ['/usr/bin/chromium-browser', profile])]:
        proc = tmp_path / 'proc' / str(pid)
        proc.mkdir(parents=True)
        # Match the argv rewrite observed on Nano.
        (proc / 'cmdline').write_bytes((' '.join(argv) + '\0').encode())
    def paths(value):
        return tmp_path / 'proc' if value == '/proc' else Path(value)
    paths.home = lambda: tmp_path
    monkeypatch.setattr(desktop, 'Path', paths)
    assert desktop._browser_pid() == 33


@pytest.mark.parametrize('compact,expected', [(True, (512, 300)), (False, (1024, 600))])
def test_native_window_is_resized_without_hardware_calls(monkeypatch, compact, expected):
    monkeypatch.setattr(desktop, '_desktop_environment', lambda: {})
    monkeypatch.setattr(desktop, '_browser_pid', lambda: 33)
    monkeypatch.setattr(desktop.time, 'sleep', lambda _: None)
    state = Mock()
    monkeypatch.setattr(desktop, '_fullscreen', state)
    commands = []
    def command(env, *args):
        commands.append(args)
        if args[1] == 'search': return '123'
        if args[1] == 'getdisplaygeometry': return '1024 600'
        if args[1] == 'getwindowgeometry': return f'WIDTH={expected[0]}\nHEIGHT={expected[1]}'
        return ''
    monkeypatch.setattr(desktop, '_command', command)
    assert desktop.set_kiosk_compact(compact) == {'compact': compact, 'width': expected[0], 'height': expected[1]}
    state.assert_called_once_with({}, 123, not compact)
    assert any(args[1] == 'windowsize' for args in commands) == compact


def test_ambiguous_browser_windows_are_not_changed(monkeypatch):
    monkeypatch.setattr(desktop, '_desktop_environment', lambda: {})
    monkeypatch.setattr(desktop, '_browser_pid', lambda: 33)
    monkeypatch.setattr(desktop, '_command', lambda *args: '123\n456')
    state = Mock()
    monkeypatch.setattr(desktop, '_fullscreen', state)
    with pytest.raises(RuntimeError): desktop.set_kiosk_compact(True)
    state.assert_not_called()


@pytest.mark.parametrize('value', [0, 1, 'true', None])
def test_compact_requires_a_boolean(value):
    with pytest.raises(ValueError): desktop.set_kiosk_compact(value)


def test_window_api_validates_origin_and_payload(monkeypatch):
    import http.client
    import threading
    from shore_tracker.controller import AppController
    from shore_tracker.preview import PreviewStore
    from shore_tracker.status import StatusStore
    from shore_tracker.web import server as web
    calls = []
    def resize(compact):
        calls.append(compact)
        return {'compact': compact, 'width': 512, 'height': 300}
    monkeypatch.setattr(web, 'set_kiosk_compact', resize)
    status = StatusStore()
    server = web.make_server(('127.0.0.1', 0), status, AppController(status), PreviewStore())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host = f'127.0.0.1:{server.server_port}'
    def request(payload, origin=None):
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
        headers = {'Content-Type': 'application/json', 'Origin': origin or 'http://' + host}
        conn.request('POST', '/api/kiosk/window', json.dumps(payload), headers)
        response = conn.getresponse()
        body = json.loads(response.read())
        result = response.status, body
        conn.close()
        return result
    try:
        assert request({'compact': True}, 'https://untrusted.invalid')[0] == 403
        for payload in [{'compact': 1}, {'compact': False, 'pid': 999}, {}, []]:
            assert request(payload)[0] == 400
        assert calls == []
        assert request({'compact': True})[0] == 200
        assert calls == [True]
        assert not status.snapshot()['tracking']['armed']
    finally:
        server.shutdown()
        server.server_close()
