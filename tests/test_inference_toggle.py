import http.client
import json
import threading
import time

import pytest

from shore_tracker.controller import AppController
from shore_tracker.inference.worker import InferenceWorker
from shore_tracker.preview import PreviewStore
from shore_tracker.recording_triggers import RecordingTriggers
from shore_tracker.status import StatusStore
from shore_tracker.web.server import make_server
from test_recording_triggers import Recorder, decision


def ready():
    status = StatusStore()
    for component in status.COMPONENTS:
        status.update_component(component, 'ready')
    return status


def test_default_off_manual_inference_never_arms_and_old_results_are_fenced():
    status = ready()
    assert status.snapshot()['inference'] == {'enabled': False, 'locked': False}
    assert status.inference_run() is None
    controller = AppController(status)
    assert controller.set_inference_enabled(True)[0]
    run = status.inference_run()
    assert not status.snapshot()['tracking']['armed']
    assert status.update_observations((), inference_run_id=run[0])
    assert status.snapshot()['recognition']['available']
    assert controller.set_inference_enabled(False)[0]
    assert status.inference_run() is None
    assert not status.snapshot()['recognition']['available']
    assert not status.update_observations((), inference_run_id=run[0])
    assert controller.set_inference_enabled(True)[0]
    assert status.inference_run()[0] != run[0]
    assert not status.update_observations((), inference_run_id=run[0])
    assert not StatusStore().snapshot()['inference']['enabled'], 'preference is not persisted'


@pytest.mark.parametrize('trigger', ['manual', 'event', 'continuous'])
def test_recording_does_not_force_or_lock_ai(trigger):
    status = ready()
    recorder = Recorder()
    armed = [False]
    recording = RecordingTriggers(recorder, armed_reader=lambda: armed[0])
    controller = AppController(status, recording=recording)
    if trigger == 'manual':
        assert controller.recording_command('start')[0]
    elif trigger == 'event':
        armed[0] = True
        recording.follow(decision(), recording.clock())
    else:
        assert recording.set_continuous(True)[0]
        armed[0] = True
    recording.check_once()
    assert recorder.state == 'recording'
    assert status.snapshot()['inference'] == {'enabled': False, 'locked': False}
    assert controller.set_inference_enabled(True)[0]
    assert controller.set_inference_enabled(False)[0]
    assert recorder.state == 'recording'
    armed[0] = False
    recording.cancel_tracking()
    recording.command('stop')
    recording.check_once()
    assert recorder.state == 'finalizing'
    assert controller.set_inference_enabled(False)[0]


def test_disabling_ai_while_tracking_keeps_armed_and_fences_old_results():
    status = ready()
    stops = []
    controller = AppController(status, stop_callback=lambda: stops.append('stop'))
    status.add_disarm_listener(controller.fail_safe_stop)
    assert controller.arm()[0]
    run = status.inference_run()
    assert controller.set_inference_enabled(False)[0]
    assert status.snapshot()['tracking']['armed']
    assert status.snapshot()['tracking']['mode'] == 'manual'
    assert not status.update_observations((), inference_run_id=run[0])
    status.update_component('inference', 'stale')
    assert status.snapshot()['tracking']['armed'], 'disabled inference cannot disarm keyboard control'
    assert stops == []
    assert controller.set_inference_enabled(True)[0]
    assert status.inference_run()[0] != run[0]
    status.update_component('inference', 'stale')
    assert not status.snapshot()['tracking']['armed'], 'enabled inference retains freshness STOP'


def test_worker_can_recognize_without_tracking_and_discards_inflight_disable():
    preview, status = PreviewStore(), ready()
    entered, release = threading.Event(), threading.Event()
    results, calls = [], []
    class Models:
        def load(self): pass
        def infer_jpeg(self, jpeg):
            calls.append(jpeg); entered.set(); assert release.wait(2); return ()
    class Sink:
        def process_frame(self, observations, **kwargs): results.append(observations)
    worker = InferenceWorker(preview=preview, status=status, coordinator=Sink(),
        models=Models(), stale_seconds=1.5, on_demand=True)
    worker.start()
    try:
        preview.publish(b'\xff\xd8off\xff\xd9')
        assert not entered.wait(.15)
        status.set_inference_enabled(True)
        preview.publish(b'\xff\xd8on\xff\xd9')
        assert entered.wait(1)
        assert not status.snapshot()['tracking']['armed']
        status.set_inference_enabled(False)
        release.set()
        time.sleep(.1)
        assert results == []
        assert calls == [b'\xff\xd8on\xff\xd9']
    finally:
        release.set(); assert worker.close(timeout=1)


def test_http_toggle_rejects_foreign_origin_bad_shapes_and_allows_tracking_disable():
    status = ready()
    server = make_server(('127.0.0.1', 0), status, AppController(status), PreviewStore())
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
    def post(payload, origin=None):
        headers = {'Content-Type': 'application/json'}
        if origin: headers['Origin'] = origin
        connection.request('POST', '/api/inference', json.dumps(payload), headers)
        response = connection.getresponse(); return response.status, json.loads(response.read())
    try:
        for payload in ({}, {'enabled': 1}, {'enabled': True, 'extra': 1}, []):
            assert post(payload)[0] == 400
        assert post({'enabled': True}, 'http://evil.example')[0] == 403
        assert status.inference_run() is None
        assert post({'enabled': True})[1]['inference'] == {'enabled': True, 'locked': False}
        assert server.controller.arm()[0]
        assert post({'enabled': False})[0] == 200
        assert status.snapshot()['tracking']['armed']
        connection.request('GET', '/inference-toggle.js')
        response = connection.getresponse(); assert response.status == 200; response.read()
    finally:
        connection.close(); server.shutdown(); server.server_close(); thread.join(2)
