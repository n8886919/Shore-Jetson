import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
from shore_tracker.manual_recording import ManualRecording
from shore_tracker.controller import AppController
from shore_tracker.status import StatusStore


class Clip:
    def __init__(self, uri, codec, path):
        self.path = path
        self.aborted = False
    def start(self): self.path.write_bytes(b'encoded-test-video')
    def poll_error(self): return False
    def finish(self): return True
    def abort(self): self.aborted = True


def recorder(tmp_path, builder=Clip):
    return ManualRecording(tmp_path, lambda: object(), 'h265', 180, 0, builder=builder)


def wait_for(rec, states):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if rec.snapshot()['state'] in states: return
        time.sleep(.01)
    pytest.fail(str(rec.snapshot()))


def test_recording_start_stop_idempotent_and_only_finalized_file_available(tmp_path):
    rec = recorder(tmp_path)
    rec.recover()
    assert rec.command('start')[0]
    first = rec.snapshot()['id']
    assert rec.command('start')[0]
    assert rec.snapshot()['id'] == first
    with pytest.raises(ValueError): rec.file(first)
    rec.command('stop')
    rec.command('stop')
    wait_for(rec, {'idle'})
    item = rec.recordings()[0]
    assert item['state'] == 'ready' and item['rotation_degrees'] == 180
    assert item['sha256'] == hashlib.sha256(rec.file(first).read_bytes()).hexdigest()
    rec.acknowledge(first, 'uploaded')
    assert 'Google Drive' in rec.snapshot()['detail']
    with pytest.raises(ValueError): rec.file('../escape')
    rec.close()
    assert not rec.command('start')[0]


def test_finalization_failure_preserves_partial_and_never_publishes(tmp_path):
    class FailedClip(Clip):
        def finish(self): return False
    rec = recorder(tmp_path, FailedClip)
    rec.command('start')
    wait_for(rec, {'recording'})
    rec.command('stop')
    wait_for(rec, {'failed'})
    item = rec.recordings()[0]
    assert item['state'] == 'failed'
    assert (tmp_path / item['id'] / 'clip.partial.mkv').exists()
    with pytest.raises(ValueError): rec.file(item['id'])


def test_restart_marks_interrupted_clip_failed(tmp_path):
    directory = tmp_path / ('a' * 32)
    directory.mkdir()
    (directory / 'recording.json').write_text(json.dumps({'id': directory.name, 'state': 'recording'}))
    rec = recorder(tmp_path)
    rec.recover()
    assert rec.recordings()[0]['state'] == 'failed'


def test_manual_recording_does_not_disarm_or_stop_tracking(tmp_path):
    rec = recorder(tmp_path)
    status = StatusStore()
    for name in ['camera', 'inference', 'recording', 'servo']: status.update_component(name, 'ready')
    status.arm()
    before = status.snapshot()['tracking']
    stops = []
    controller = AppController(status, recording=rec, stop_callback=lambda: stops.append(True))
    assert controller.recording_command('start')[0]
    assert controller.recording_command('stop')[0]
    rec.close()
    assert status.snapshot()['tracking'] == before
    assert stops == []


def test_start_failure_redacts_secret(tmp_path):
    def fail(*_args): raise RuntimeError('rtsp://private:password@camera')
    rec = recorder(tmp_path, fail)
    rec.command('start')
    wait_for(rec, {'failed'})
    assert 'password' not in json.dumps(rec.snapshot())
    assert rec.recordings()[0]['state'] == 'failed'


def test_http_recording_flow_does_not_restart_preview(tmp_path):
    import http.client
    from shore_tracker.preview import PreviewStore
    from shore_tracker.web.server import make_server
    status = StatusStore()
    status.update_component('camera', 'ready')
    rec = recorder(tmp_path)
    preview = PreviewStore()
    controller = AppController(status, recording=rec)
    server = make_server(('127.0.0.1', 0), status, controller, preview)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    def request(method, path, body=None, **headers):
        connection.request(method, path, json.dumps(body) if body else None, {'Content-Type': 'application/json', **headers})
        response = connection.getresponse()
        return response.status, response.read()
    try:
        assert request('POST', '/api/recording', {'action': 'start'}, Origin='https://other.example')[0] == 403
        assert request('POST', '/api/recording', {'action': []})[0] == 400
        assert request('POST', '/api/recording', {'action': 'start'})[0] == 200
        assert request('POST', '/api/recording', {'action': 'stop'})[0] == 200
        wait_for(rec, {'idle'})
        code, body = request('GET', '/api/recordings')
        item = json.loads(body)['recordings'][0]
        assert request('GET', f'/api/recordings/{item["id"]}/file') == (200, b'encoded-test-video')
        assert request('POST', '/api/recordings/ack', {'id': item['id'], 'state': 'uploaded'})[0] == 200
        assert server.preview_store is preview
        assert status.snapshot()['components']['camera']['state'] == 'ready'
    finally:
        rec.close()
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_unqualified_automatic_trial_deletes_only_its_new_video(tmp_path):
    old = tmp_path / 'previous-success.mkv'
    old.write_bytes(b'keep historical recording')
    rec=recorder(tmp_path)
    rec.command('start',trigger='tracking')
    wait_for(rec,{'recording'})
    rec.command('stop');wait_for(rec,{'idle'})
    item=rec.recordings()[0]
    assert item['state']=='discarded'
    assert not list((tmp_path/item['id']).glob('*.mkv'))
    assert old.read_bytes()==b'keep historical recording'
    with pytest.raises(ValueError):rec.file(item['id'])


@pytest.mark.parametrize('manual', [False,True])
def test_qualified_or_manual_overlap_is_preserved(tmp_path,manual):
    rec=recorder(tmp_path)
    rec.command('start',trigger='tracking')
    wait_for(rec,{'recording'})
    if manual:rec.note_trigger('manual')
    else:rec.note_trigger('tracking',2,qualified=True)
    rec.command('stop');wait_for(rec,{'idle'})
    item=rec.recordings()[0]
    assert item['state']=='ready'
    assert rec.file(item['id']).is_file()
    assert item['qualified'] is not manual
