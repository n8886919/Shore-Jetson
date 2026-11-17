from importlib.resources import files

import pytest

from shore_tracker.controller import AppController
from shore_tracker.status import StatusStore
from shore_tracker.web.server import _validated_loopback_address


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.1", "example.test"])
def test_dashboard_rejects_non_loopback(host) -> None:
    with pytest.raises(ValueError):
        _validated_loopback_address((host, 8090))


def test_ui_is_simple_and_has_fov_tuning() -> None:
    html = files("shore_tracker.web.static").joinpath("index.html").read_text()
    js = files("shore_tracker.web.static").joinpath("app.js").read_text()
    assert "開始追蹤" in html
    control = files("shore_tracker.web.static").joinpath("advanced.html").read_text()
    assert "預覽放大" in control
    for page in (html, control):
        assert 'id="reconnectButton"' not in page
        assert 'id="reconnectReason"' not in page
    assert all(f'id="{name}"' not in html for name in ('inferenceFps','targetId','servoTargetAngle','connection','panGain','autoZoomEnabled'))
    assert '<h2>跟拍</h2>' not in html
    assert "Max zoom" not in html
    assert 'id="controlToggle"' in html
    assert 'id="controlsPanel"' in html
    assert 'href="/control' not in html
    assert "panDirection" not in html
    assert "/api/control-session" not in js
    assert "transformOrigin" in js


def test_web_live_information_is_last_in_settings() -> None:
    html = files("shore_tracker.web.static").joinpath("index.html").read_text()
    settings = html.split('id="controlsPanel"', 1)[1].split('</aside>', 1)[0]
    final_section = settings.rsplit('<section', 1)[1]
    footer = html.split('<footer class="status-bar">', 1)[1].split('</footer>', 1)[0]
    assert 'id="liveInformation"' in final_section
    assert '<h2>即時資訊</h2>' in final_section
    for name in ('liveInfo', 'inferenceLamp', 'phase', 'streamNotice'):
        assert f'id="{name}"' in final_section
        assert f'id="{name}"' not in footer
        assert html.count(f'id="{name}"') == 1
    assert 'id="armButton"' in footer and 'id="controlToggle"' in footer


def test_controller_applies_p_and_zoom_tuning() -> None:
    values = []
    controller = AppController(StatusStore(), tuning_callback=lambda **kwargs: values.append(kwargs))
    assert controller.tune(
        pan_gain_deg=40,
        pan_deadband=0.05,
        pan_direction=-1,
        zoom_maximum=2.0,
    )[0]
    assert values == [{
        "pan_gain_deg": 40,
        "pan_deadband": 0.05,
        "pan_direction": -1,
        "zoom_maximum": 2.0,
    }]


def test_camera_http_api_validates_origin_and_preserves_loopback_boundary():
    import http.client
    import json
    import threading
    from shore_tracker.preview import PreviewStore
    from shore_tracker.web.server import make_server

    class Camera:
        def __init__(self): self.actions = []
        def snapshot(self): return {"fields": [], "zoom": True, "focus": True, "zoom_factor": None}
        def command(self, action, payload): self.actions.append((action, payload))

    camera = Camera()
    status = StatusStore()
    controller = AppController(status, camera_controls=camera)
    server = make_server(('127.0.0.1', 0), status, controller, PreviewStore())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
    try:
        connection.request('GET', '/api/camera')
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())['zoom_factor'] is None
        assert camera.actions == []

        body = json.dumps({'action': 'zoom_in', 'values': {}})
        connection.request('POST', '/api/camera', body, {'Content-Type': 'application/json', 'Origin': 'https://other.example'})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        assert camera.actions == []

        connection.request('POST', '/api/camera', body, {'Content-Type': 'application/json'})
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        assert camera.actions == [('zoom_in', {})]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_status_separates_preview_rate_from_inference_rate():
    import json,threading,time
    from urllib.request import urlopen
    from shore_tracker.preview import PreviewStore
    from shore_tracker.web.server import make_server
    status=StatusStore()
    status.update_metrics(inference_fps=5)
    preview=PreviewStore()
    now=time.monotonic()
    for i in range(21):preview.publish(b'\xff\xd8frame\xff\xd9',captured_monotonic=now-1+i*.05)
    server=make_server(('127.0.0.1',0),status,AppController(status),preview)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with urlopen(f'http://127.0.0.1:{server.server_port}/api/status',timeout=2) as r:data=json.load(r)
        assert data['metrics']['preview_fps']==pytest.approx(20)
        assert data['metrics']['inference_fps']==5
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


def test_display_http_uses_latest_image_with_current_boxes_without_a_buffer():
    import http.client, json, threading, time
    from shore_tracker.inference import ActionPrediction, DetectionPrediction, NormalizedBox, TrackObservation
    from shore_tracker.preview import PreviewStore
    from shore_tracker.web.server import make_server
    status = StatusStore()
    for name in ('camera', 'inference', 'servo'): status.update_component(name, 'ready')
    assert status.arm()[0]
    run_id = status.inference_run()[0]
    status.update_observations([TrackObservation(
        DetectionPrediction(42, NormalizedBox(.1,.1,.2,.3), .9),
        ActionPrediction(42, .8, .1, .1))],inference_run_id=run_id)
    preview = PreviewStore()
    for i in range(20): preview.publish(b'\xff\xd8'+str(i).encode()+b'\xff\xd9')
    server=make_server(('127.0.0.1',0),status,AppController(status),preview)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
    try:
        start=time.monotonic()
        connection.request('GET','/api/display-frame')
        response=connection.getresponse()
        assert response.status == 200
        metadata=json.loads(response.getheader('X-Frame-Meta'))
        assert response.read() == b'\xff\xd819\xff\xd9'
        assert time.monotonic()-start < .3
        assert metadata['cursor'] == 20
        assert metadata['recognized'] is True
        assert metadata['recognition']['detections'][0]['track_id'] == 42
        sequence = metadata['recognition']['sequence']
        preview.publish(b'\xff\xd8next\xff\xd9')
        connection.request('GET','/api/display-frame')
        response=connection.getresponse()
        next_metadata=json.loads(response.getheader('X-Frame-Meta'))
        assert response.read() == b'\xff\xd8next\xff\xd9'
        assert next_metadata['cursor'] > metadata['cursor']
        assert next_metadata['recognition']['sequence'] == sequence
        status.update_observations((),inference_run_id=run_id)
        connection.request('GET','/api/display-frame')
        response=connection.getresponse()
        assert json.loads(response.getheader('X-Frame-Meta'))['recognition']['sequence'] == sequence + 1
        response.read()
        status.disarm('STOP')
        connection.request('GET','/api/display-frame')
        response=connection.getresponse()
        assert json.loads(response.getheader('X-Frame-Meta'))['recognition'] is None
        response.read()
        connection.request('GET','/api/display-frame?after=nan')
        response=connection.getresponse()
        assert response.status == 400
        response.read()
    finally:
        connection.close();server.shutdown();server.server_close();thread.join(timeout=2)


@pytest.mark.parametrize('recording', [
    {'state': 'recording'}, {'state': 'starting'}, {'state': 'finalizing'},
    {'state': 'idle', 'manual_requested': True},
    {'state': 'idle', 'tracking_requested': True},
])
def test_reconnect_blocks_active_or_queued_recording(recording):
    from types import SimpleNamespace
    calls = []
    controller = AppController(StatusStore(), recording=SimpleNamespace(snapshot=lambda: recording),
        camera_reconnect_callback=lambda: calls.append(True))
    assert not controller.reconnect_camera()[0]
    assert not calls


def test_reconnect_http_is_nonblocking_and_validates_origin_payload():
    import http.client
    import json
    import threading
    from shore_tracker.preview import PreviewStore
    from shore_tracker.web.server import make_server
    calls = []
    def reconnect():
        calls.append(True)
        return True, 'queued'
    status = StatusStore()
    controller = AppController(status, camera_reconnect_callback=reconnect,
                               diagnostics_callback=lambda: {'available': True})
    server = make_server(('127.0.0.1', 0), status, controller, PreviewStore())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
    try:
        for alias in ['/control', '/advanced']:
            connection.request('GET', alias)
            response = connection.getresponse()
            assert response.status == 303
            assert response.getheader('Location') == '/kiosk?controls=1'
            response.read()
        for path in ['/kiosk', '/mobile', '/kiosk.css', '/kiosk.js', '/advanced.css', '/advanced.js', '/api/diagnostics']:
            connection.request('GET', path)
            response = connection.getresponse()
            assert response.status == 200
            response.read()
        for body, origin, expected in [('{}', 'https://evil.example', 403), ('[]', None, 400), ('{"action":"reset"}', None, 400), ('{}', None, 202)]:
            headers = {'Content-Type': 'application/json'}
            if origin: headers['Origin'] = origin
            connection.request('POST', '/api/camera/reconnect', body, headers)
            response = connection.getresponse()
            assert response.status == expected
            response.read()
        assert len(calls) == 1
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_inference_rate_api_checks_origin_range_and_unknown_fields():
    import http.client,json,threading
    from shore_tracker.preview import PreviewStore
    from shore_tracker.web.server import make_server
    rates=[]
    server=make_server(('127.0.0.1',0),StatusStore(),AppController(StatusStore(),inference_rate_callback=rates.append),PreviewStore())
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
    try:
        for payload,origin,expected in [({'max_fps':2},'https://evil.example',403),({'max_fps':True},None,400),({'max_fps':0},None,200),({'max_fps':11},None,400),({'max_fps':2,'extra':1},None,400),({'max_fps':2},None,200)]:
            headers={'Content-Type':'application/json'}
            if origin:headers['Origin']=origin
            conn.request('POST','/api/inference/rate',json.dumps(payload),headers)
            response=conn.getresponse();assert response.status==expected;response.read()
        assert rates==[0,2]
    finally:conn.close();server.shutdown();server.server_close();thread.join(2)


def test_preview_clients_throttle_independently_and_use_latest_frame():
    import http.client
    import threading
    import time
    from shore_tracker.preview import PreviewStore
    from shore_tracker.web.server import make_server

    preview = PreviewStore()
    stopped = threading.Event()
    def produce():
        sequence = 0
        while not stopped.is_set():
            sequence += 1
            preview.publish(b'\xff\xd8' + sequence.to_bytes(4, 'big') + b'\xff\xd9')
            stopped.wait(.05)
    producer = threading.Thread(target=produce, daemon=True)
    server = make_server(('127.0.0.1', 0), StatusStore(), AppController(StatusStore()), preview)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    producer.start(); serving.start()
    def sample(mode):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
        try:
            connection.request('GET', '/api/preview.mjpeg' + mode)
            response = connection.getresponse()
            assert response.status == 200
            samples = []
            for _ in range(3):
                assert response.readline() == b'--shoreframe\r\n'
                headers = {}
                while (line := response.readline()) != b'\r\n':
                    key, value = line.decode().split(':', 1)
                    headers[key.lower()] = value.strip()
                jpeg = response.read(int(headers['content-length']))
                assert response.read(2) == b'\r\n'
                samples.append((time.monotonic(), int.from_bytes(jpeg[2:-2], 'big')))
            response.close()
            return samples
        finally:
            connection.close()
    try:
        web = sample('')  # Existing web/mobile URLs default to the 5 FPS cap.
        local = sample('?mode=local')
        assert web[-1][0] - web[0][0] >= .35
        assert web[-1][1] - web[0][1] >= 6  # skipped frames are not queued
        assert local[-1][0] - local[0][0] < .30
        assert preview.frame_rate() > 15  # client cap never slows the camera producer
        for query in ['?mode=bad', '?mode=', '?mode=local&mode=web']:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
            connection.request('GET', '/api/preview.mjpeg' + query)
            response = connection.getresponse()
            assert response.status == 400
            response.read(); connection.close()
    finally:
        stopped.set(); producer.join(timeout=2)
        server.shutdown(); server.server_close(); serving.join(timeout=2)


def test_tuning_api_accepts_optional_boolean_tilt_without_breaking_old_clients():
    import http.client
    import json
    import threading
    from shore_tracker.preview import PreviewStore
    from shore_tracker.web.server import make_server
    values=[]
    controller=AppController(StatusStore(),tuning_callback=lambda **kw:values.append(kw))
    server=make_server(('127.0.0.1',0),controller.status,controller,PreviewStore())
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=dict(pan_gain_deg=45,pan_deadband=.04,pan_direction=-1,zoom_maximum=1)
    def post(payload):
        c=http.client.HTTPConnection('127.0.0.1',server.server_port)
        c.request('POST','/api/tuning',json.dumps(payload),{'Content-Type':'application/json'})
        r=c.getresponse();data=r.read();c.close();return r.status,json.loads(data)
    try:
        assert post(base)[0] == 200 and 'tilt_enabled' not in values[-1]
        assert post(dict(base,tilt_enabled=True))[0] == 200 and values[-1]['tilt_enabled'] is True
        assert post(dict(base,tilt_enabled=False))[0] == 200 and values[-1]['tilt_enabled'] is False
        count=len(values)
        for bad in (0,1,'true',None,[]): assert post(dict(base,tilt_enabled=bad))[0] == 400
        assert post(dict(base,unknown=True))[0] == 400
        assert len(values) == count
    finally:
        server.shutdown();thread.join();server.server_close()


def test_continuous_recording_mode_api_is_strict_and_never_arms_or_records():
    import http.client,json,threading
    from shore_tracker.preview import PreviewStore
    from shore_tracker.web.server import make_server
    from shore_tracker.recording_triggers import RecordingTriggers
    class Recorder:
        def snapshot(self): return {'state':'idle'}
        def note_trigger(self,*args,**kwargs): raise AssertionError('must not record while disarmed')
    status=StatusStore();recording=RecordingTriggers(Recorder(),armed_reader=lambda:status.snapshot()['tracking']['armed'])
    server=make_server(('127.0.0.1',0),status,AppController(status,recording=recording),PreviewStore())
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def post(payload,origin=None):
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port)
        headers={'Content-Type':'application/json'}
        if origin:headers['Origin']=origin
        connection.request('POST','/api/recording/mode',json.dumps(payload),headers)
        r=connection.getresponse();r.read();connection.close();return r.status
    try:
        assert post({'continuous':True},'https://example.invalid')==403
        for v in [0,1,'true',None]:assert post({'continuous':v})==400
        assert post({'continuous':True,'extra':0})==400
        assert post({'continuous':True})==200 and recording.snapshot()['continuous_enabled']
        assert post({'continuous':False})==200 and not recording.snapshot()['continuous_enabled']
        assert not status.snapshot()['tracking']['armed']
    finally:server.shutdown();thread.join();server.server_close()
