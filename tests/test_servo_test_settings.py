import http.client
import json
import threading
from dataclasses import replace

import pytest

from shore_tracker.runtime import ShoreTrackerRuntime
from shore_tracker.servo.pwm_test import PwmServoTest
from shore_tracker.servo.simple import SimpleServoClient
from shore_tracker.servo.simple_service import ServoService
from shore_tracker.web.server import make_server
from test_runtime import Camera, Inference, config


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('SHORE_TRACKER_RTSP_URI','rtsp://test:secret@192.0.2.8/live')
    monkeypatch.setattr('shore_tracker.coordinator.TrackingCoordinator.start',lambda _:None)
    monkeypatch.setattr(ShoreTrackerRuntime,'_start_supervisor',lambda _:None)
    monkeypatch.setattr(ShoreTrackerRuntime,'_watch_pan_tilt',lambda _:None)
    clients, owners, events, now = [], [], [], [0.]

    class Writer:
        def __init__(self): self.writes = []
        def open(self):
            assert not owners
            owners.append(self); events.append('open')
        def write(self, payload): self.writes.append(payload)
        def close(self):
            if self in owners: owners.remove(self)
            events.append('close')
        def is_connected(self): return self in owners

    def factory(cfg, status):
        client=SimpleServoClient(Writer(),protocol='m0_pulse',angle_min_deg=cfg.angle_min_deg,
            angle_max_deg=cfg.angle_max_deg,tilt_min_deg=cfg.tilt_min_deg,
            tilt_max_deg=cfg.tilt_max_deg,pan_channel=cfg.pan_channel,tilt_channel=cfg.tilt_channel)
        monkeypatch.setattr(client,'_run',lambda:None)
        clients.append(client)
        return ServoService(client,status)

    cfg=config(tmp_path)
    cfg=replace(cfg,servo=replace(cfg.servo,tilt_direction=-1))
    runtime=ShoreTrackerRuntime(cfg,
        dashboard_builder=lambda address,*a,**k:make_server(('127.0.0.1',0),*a,**k),
        camera_builder=Camera,inference_worker_builder=Inference,servo_service_builder=factory,
        camera_route_validator=lambda *a:None,
        servo_test_builder=lambda **kwargs:PwmServoTest(**kwargs,clock=lambda:now[0]))
    runtime.start_services()
    runtime.clients,runtime.owners,runtime.events,runtime.test_now=clients,owners,events,now
    thread=threading.Thread(target=runtime.server.serve_forever,daemon=True);thread.start()
    yield runtime
    runtime.stop_services();runtime.server.shutdown();runtime.server.server_close();thread.join(2)


def request(runtime, method='GET', body=None, path='/api/servo-test', **headers):
    connection=http.client.HTTPConnection('127.0.0.1',runtime.server.server_port,timeout=2)
    headers={'Content-Type':'application/json','X-Pan-Tilt-Token':runtime.server.pan_tilt_token,**headers}
    connection.request(method,path,json.dumps(body) if body is not None else None,headers)
    response=connection.getresponse();data=response.read();connection.close()
    if response.getheader('Content-Type','').startswith('application/json'): data=json.loads(data)
    return response.status,data


def test_read_only_load_then_exclusive_test_handoff_and_global_stop(runtime):
    assert request(runtime,path='/servo-test.js')[0] == 200
    assert request(runtime)[1]['calibrated']
    assert runtime.events == ['open'] and runtime.clients[0].writer.writes == []
    code,result=request(runtime,'POST',{'action':'start','epoch':0})
    assert code == 200 and result['status']['running']
    assert runtime.events == ['open','close','open'] and len(runtime.owners) == 1
    assert not runtime.controller.arm()[0]
    runtime.controller.recover_servo()
    assert len(runtime.clients) == 2, 'automatic recovery must not take the test port'
    runtime._servo_test.tick();runtime.clients[-1]._send_target()
    assert runtime.clients[-1].writer.writes == [b'#00P1500!#01P1500!']
    assert runtime.controller.stop()[0]
    assert not runtime._servo_test.snapshot()['running']
    runtime.controller.recover_servo()
    assert len(runtime.owners) == 1 and len(runtime.clients) == 3
    assert not runtime.status.snapshot()['tracking']['armed']
    assert runtime.clients[-1].writer.writes == []


def test_half_tilt_sequence_uses_test_limits_and_restores_disarmed(runtime):
    original = runtime.config.servo
    code, result = request(runtime, 'POST', {'action': 'start', 'epoch': 0})
    assert code == 200
    client = runtime.clients[-1]
    assert (client.angle_min_deg, client.angle_max_deg) == (70, 110)
    assert (client.tilt_min_deg, client.tilt_max_deg) == (70, 110)
    for sequence in range(1, 2401):
        runtime.test_now[0] += .025
        runtime._servo_test.renew(session=result['session'], sequence=sequence)
        runtime._servo_test.tick()
        client._send_target()
        if not runtime._servo_test.snapshot()['running']:
            break
    assert runtime._servo_test.snapshot()['phase'] == 'complete'
    assert any(b'#01P1722!' in data for data in client.writer.writes)
    assert not any(b'#01P1944!' in data or b'#01P1056!' in data for data in client.writer.writes)
    assert any(b'#00P1722!' in data for data in client.writer.writes)
    assert any(b'#00P1278!' in data for data in client.writer.writes)
    runtime.controller.recover_servo()
    assert len(runtime.owners) == 1
    assert not runtime._servo_test.snapshot()['connected']
    assert not runtime.status.snapshot()['tracking']['armed']
    assert runtime.clients[-1].writer.writes == []
    assert runtime.config.servo == original


@pytest.mark.parametrize('headers,code',[({'Origin':'https://evil.example'},403),
    ({'X-Pan-Tilt-Token':'bad'},403),({'Content-Type':'text/plain'},403),({'Host':'evil.example'},421)])
def test_foreign_or_invalid_requests_cannot_start_test(runtime,headers,code):
    assert request(runtime,'POST',{'action':'start','epoch':0},**headers)[0] == code
    assert runtime.events == ['open']


@pytest.mark.parametrize('body',[{'action':'start'}, {'action':'stop','extra':1},
    {'action':'move'}, {'action':[]}, None])
def test_invalid_shapes_are_rejected_before_handoff(runtime,body):
    assert request(runtime,'POST',body)[0] == 400
    assert runtime.events == ['open']


def test_tracking_and_wrong_channel_mapping_block_test(runtime):
    assert runtime.controller.arm()[0]
    assert request(runtime,'POST',{'action':'start','epoch':0})[0] == 409
    runtime.controller.stop()
    runtime.config=replace(runtime.config,servo=replace(runtime.config.servo,pan_channel=1,tilt_channel=0))
    assert request(runtime,'POST',{'action':'start','epoch':runtime._servo_test.snapshot()['epoch']})[0] == 409
    assert runtime.events == ['open']


def test_late_start_and_unreleased_auto_port_never_take_over(runtime, monkeypatch):
    assert request(runtime,'POST',{'action':'stop'})[0] == 200
    assert request(runtime,'POST',{'action':'start','epoch':0})[0] == 409
    assert runtime.events == ['open']
    close=runtime._servo.close
    monkeypatch.setattr(runtime._servo,'close',lambda:False)
    epoch=runtime._servo_test.snapshot()['epoch']
    assert request(runtime,'POST',{'action':'start','epoch':epoch})[0] == 503
    assert runtime.events == ['open']
    monkeypatch.setattr(runtime._servo,'close',close)


def test_pwm_command_responds_once_on_keepalive_connection(runtime):
    connection = http.client.HTTPConnection('127.0.0.1', runtime.server.server_port, timeout=2)
    try:
        connection.request('POST', '/api/servo-test', json.dumps({'action': 'stop'}),
                           {'Content-Type': 'application/json', 'X-Pan-Tilt-Token': runtime.server.pan_tilt_token})
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())['ok']
        connection.request('GET', '/api/servo-test')
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())['available']
    finally:
        connection.close()
