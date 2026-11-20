import http.client
import json
import os
import threading
from urllib.parse import urlencode

import pytest

from shore_tracker.controller import AppController
from shore_tracker.preview import PreviewStore
from shore_tracker.status import StatusStore
from shore_tracker.web.file_browser import CardFiles, byte_range
from shore_tracker.web.server import make_server


@pytest.fixture
def card(tmp_path, monkeypatch):
    root = tmp_path / 'card'
    root.mkdir()
    monkeypatch.setattr('shore_tracker.web.file_browser.os.path.ismount', lambda path: path == root)
    return CardFiles(root)


def test_missing_mount_does_not_expose_underlying_directory(tmp_path):
    root = tmp_path / 'unmounted'
    root.mkdir()
    (root / 'on-emmc.txt').write_text('must not serve')
    with pytest.raises(OSError, match='not mounted'):
        CardFiles(root).listing('')
    with pytest.raises(OSError, match='not mounted'):
        with CardFiles(root).open_path('on-emmc.txt'):
            pytest.fail('opened an unmounted directory')


def test_directory_pagination_unicode_and_no_links(card):
    (card.root / '影片').mkdir()
    (card.root / '外部連結').symlink_to('/etc')
    for index in range(205):
        (card.root / f'{index:03}.mkv').write_bytes(b'video')
    first, last = card.listing(''), card.listing('', 200)
    assert first['total'] == 206
    assert len(first['entries']) == 200 and len(last['entries']) == 6
    assert first['entries'][0]['name'] == '影片'
    assert not any(row['name'] == '外部連結' for row in first['entries'])
    assert first['storage']['total'] > 0


@pytest.mark.parametrize('name', ['../secret', '/etc/passwd', 'a/../../secret', '.', 'a//b', 'a\0b'])
def test_invalid_paths_rejected(card, name):
    with pytest.raises(ValueError):
        with card.open_path(name):
            pytest.fail('opened invalid path')


def test_symlink_files_directories_and_fifo_never_served(card, tmp_path):
    outside = tmp_path / 'secret'
    outside.write_bytes(b'private')
    (card.root / 'file-link').symlink_to(outside)
    (card.root / 'dir-link').symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(card.root / 'pipe')
    for name in ['file-link', 'dir-link/secret', 'pipe']:
        with pytest.raises((OSError, ValueError)):
            with card.open_path(name):
                pytest.fail('opened linked or special file')


@pytest.mark.parametrize('header,size,expected', [
    (None, 10, (0, 9, False)), ('bytes=2-5', 10, (2, 5, True)),
    ('bytes=8-', 10, (8, 9, True)), ('bytes=-3', 10, (7, 9, True)),
    ('bytes=0-999', 10, (0, 9, True)), (None, 0, (0, -1, False)),
])
def test_byte_ranges(header, size, expected):
    assert byte_range(header, size) == expected


@pytest.mark.parametrize('header', ['bytes=10-', 'bytes=4-2', 'bytes=-0', 'bytes=-', 'bytes=0-1,3-4', 'nonsense'])
def test_invalid_ranges(header):
    with pytest.raises(ValueError):
        byte_range(header, 10)


def test_http_browsing_download_ranges_head_and_no_actuation(card):
    folder = card.root / 'recordings'
    folder.mkdir()
    (folder / '海浪.mkv').write_bytes(b'0123456789')
    (folder / 'note.html').write_bytes(b'<script>alert(1)</script>')
    (folder / 'escape').symlink_to('/etc/passwd')
    status = StatusStore()
    controller = AppController(status)
    server = make_server(('127.0.0.1', 0), status, controller, PreviewStore())
    server.card_files = card
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    def request(method, path, headers=None):
        connection.request(method, path, headers=headers or {})
        reply = connection.getresponse()
        return reply.status, dict(reply.getheaders()), reply.read()
    try:
        code, headers, body = request('GET', '/files')
        assert code == 200 and '記憶卡檔案' in body.decode()
        assert "script-src 'self'" in headers['Content-Security-Policy']
        for asset in ['/files.css', '/files.js']:
            assert request('GET', asset)[0] == 200
        code, _, body = request('GET', '/api/files?path=recordings')
        assert code == 200
        data = json.loads(body)
        assert [entry['name'] for entry in data['entries']] == ['note.html', '海浪.mkv']
        url = '/api/files/content?' + urlencode({'path': 'recordings/海浪.mkv'})
        code, headers, body = request('GET', url, {'Range': 'bytes=2-5'})
        assert code == 206 and body == b'2345' and headers['Content-Range'] == 'bytes 2-5/10'
        assert headers['Content-Type'] == 'video/x-matroska'
        code, headers, body = request('HEAD', url)
        assert code == 200 and body == b'' and headers['Content-Length'] == '10'
        code, headers, body = request('GET', url + '&download=1')
        assert code == 200 and body == b'0123456789'
        assert headers['Content-Disposition'].startswith('attachment;')
        code, headers, _ = request('GET', '/api/files/content?path=recordings/note.html')
        assert code == 200 and headers['Content-Type'] == 'application/octet-stream'
        code, _, _ = request('GET', url, {'Range': 'bytes=10-'})
        assert code == 416
        assert request('GET', '/api/files/content?path=recordings/escape')[0] == 403
        assert request('GET', '/api/files?path=../')[0] == 400
        assert request('GET', '/api/files?offset=bad')[0] == 400
        assert request('GET', '/api/files?path=missing')[0] == 404
        assert not status.snapshot()['tracking']['armed']
        assert request('GET', '/api/files', {'Host': 'evil.example'})[0] == 421
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
