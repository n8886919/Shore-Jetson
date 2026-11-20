from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Event
from urllib.parse import parse_qs, urlsplit

import pytest

from shore_tracker.camera import card_recording
from shore_tracker.camera.card_recording import CameraCardRecording


class Camera:
    """Exercise the actual HTTP and XML handling without a network connection."""

    def __init__(self):
        self.host = '192.0.2.55'
        self.calls = []
        self.enabled = False
        self.local = '1'
        self.remote = '0'
        self.ignore_write = False
        self.reject_login = False
        self.model = 'MCA85'
        self.hook = None

    def respond(self, endpoint, params):
        if endpoint == 'getinfo':
            return 200, f'<info model="{self.model}"/>'
        if endpoint == 'getuid':
            if self.reject_login:
                return 400, '<fault><reason>password error</reason></fault>'
            assert params == {'username': ['test user'], 'password': ['p+& secret']}
            return 200, '<uid>ab1200ff</uid>'
        assert params['uid'] == ['ab1200ff']
        if endpoint == 'setScheduleStorage':
            if 'enable' in params:
                assert set(params) == {'uid', 'enable'}
                if not self.ignore_write:
                    self.enabled = params['enable'] == ['1']
            return 200, (f'<RecordConfig><Common LocalEnable="{self.local}" RemoteEnable="{self.remote}"/>'
                         f'<ScheduleRecord LocalStore="{int(self.enabled)}" RemoteStore="0"/></RecordConfig>')
        if endpoint == 'getDevState':
            return 200, '<DevState sd1="1" system_time="2026-11-9-8-36-12"/>'
        if endpoint == 'get_record_query':
            assert params['day'] == ['9'] and params['month'] == ['11'] and params['record_mode'] == ['2']
            return 200, ('<RecordQueryInfo><items start_time="2026-11-09 07:30:11" filesize="123" '
                         'record_mode="2" filepath="/mnt/mmc0/schedule/test.mp4"/></RecordQueryInfo>')
        raise AssertionError(endpoint)

    def connection(self, host, timeout):
        assert host == self.host and timeout <= 2
        camera = self

        class Connection:
            def request(self, method, path, headers):
                assert method == 'GET' and headers['Cache-Control'] == 'no-store'
                url = urlsplit(path)
                assert not url.hostname and not url.scheme
                self.endpoint = url.path.split('/')[-1]
                self.params = parse_qs(url.query)
                camera.calls.append((self.endpoint, self.params))

            def getresponse(self):
                result = camera.hook(self.endpoint, self.params) if camera.hook else None
                status, payload = result if result is not None else camera.respond(self.endpoint, self.params)
                response = BytesIO(payload.encode())
                response.status = status
                return response

            def close(self):
                pass

        return Connection()


@pytest.fixture
def setup(monkeypatch):
    camera = Camera()
    monkeypatch.setenv('SHORE_CAMERA_ADMIN_USER', 'test user')
    monkeypatch.setenv('SHORE_CAMERA_ADMIN_PASSWORD', 'p+& secret')
    monkeypatch.setattr(card_recording.http.client, 'HTTPConnection', camera.connection)
    return camera, CameraCardRecording(lambda: camera.host)


def test_lifecycle_and_cached_status_do_not_start_camera(setup):
    cam, rec = setup
    rec.recover()
    rec.follow(object(), 1)
    rec.cancel_tracking()
    assert not rec.set_continuous(True)[0]
    assert rec.snapshot()['schedule_enabled'] is None
    assert rec.recordings() == []
    with pytest.raises(ValueError):
        rec.file('../other')
    with pytest.raises(ValueError):
        rec.acknowledge('x', 'uploaded')
    rec.close()
    assert not cam.calls


def test_schedule_writes_only_gate_and_always_checks_independent_readback(setup):
    cam, rec = setup
    assert rec.command('start')[0]
    assert cam.enabled and rec.snapshot()['state'] == 'schedule_enabled'
    assert rec.snapshot()['schedule_enabled'] and rec.snapshot()['writing_confirmed'] is None
    assert cam.calls[-2:] == [('setScheduleStorage', {'uid': ['ab1200ff'], 'enable': ['1']}),
                               ('setScheduleStorage', {'uid': ['ab1200ff']})]
    assert rec.command('start')[0]
    rec.close()
    assert not cam.enabled and rec.snapshot()['state'] == 'idle'
    assert [args['enable'] for _, args in cam.calls if 'enable' in args] == [['1'], ['0']]
    assert not rec.command('start')[0]


def test_no_credentials_no_connection_and_route_identity_before_password(setup, monkeypatch):
    cam, rec = setup
    monkeypatch.delenv('SHORE_CAMERA_ADMIN_PASSWORD')
    assert not rec.command('start')[0] and not cam.calls
    monkeypatch.setenv('SHORE_CAMERA_ADMIN_PASSWORD', 'X')
    cam.model = 'different'
    assert not rec.command('start')[0]
    assert [name for name, params in cam.calls] == ['getinfo']
    cam.calls.clear()
    rec = CameraCardRecording(lambda: 'user:pass@example.invalid')
    assert not rec.command('refresh')[0] and not cam.calls
    assert 'pass' not in repr(rec.snapshot())


def test_rejected_credentials_not_repeated_or_exposed(setup):
    cam, rec = setup
    cam.reject_login = True
    assert not rec.command('start')[0]
    assert not rec.command('refresh')[0]
    assert not rec.command('stop')[0]
    assert [name for name, _ in cam.calls] == ['getinfo', 'getuid']
    assert rec.snapshot()['schedule_enabled'] is None
    assert all(word not in repr(rec.snapshot()) for word in ('secret', 'ab1200ff', cam.host))


@pytest.mark.parametrize('local,remote', [('0','0'), ('1','1'), ('1','')])
def test_nonlocal_or_ambiguous_destination_never_written(setup,local,remote):
    cam, rec = setup
    cam.local, cam.remote = local, remote
    assert not rec.command('start')[0] and not rec.snapshot()['available']
    assert not any('enable' in p for _, p in cam.calls)


def test_returned_success_without_persisted_change_fails(setup):
    cam, rec = setup
    cam.ignore_write = True
    assert not rec.command('start')[0]
    assert rec.snapshot()['schedule_enabled'] is False
    assert rec.snapshot()['writing_confirmed'] is None


def test_ambiguous_write_still_stopped_during_shutdown(setup):
    cam, rec = setup

    def disconnect(endpoint, params):
        if endpoint == 'setScheduleStorage' and ('enable' in params or cam.enabled):
            cam.enabled = True
            raise TimeoutError('sensitive url with password')

    cam.hook = disconnect
    assert not rec.command('start')[0]
    assert rec.snapshot()['schedule_enabled'] is None
    cam.hook = None
    rec.close()
    assert not cam.enabled
    assert rec.snapshot()['schedule_enabled'] is False


def test_shutdown_never_stops_another_camera_after_route_change(setup):
    cam, rec = setup
    assert rec.command('start')[0]
    cam.host = '192.0.2.99'
    rec.close()
    assert cam.calls[-1][0] == 'getinfo' and cam.enabled
    assert rec.snapshot()['schedule_enabled'] is None


def test_independent_snapshot_during_start_and_serial_stop(setup):
    cam, rec = setup
    entered, release = Event(), Event()

    def hold_write(endpoint, params):
        if params.get('enable') == ['1']:
            entered.set()
            assert release.wait(2)

    cam.hook = hold_write
    with ThreadPoolExecutor(2) as workers:
        started = workers.submit(rec.command, 'start')
        assert entered.wait(2)
        stopped = workers.submit(rec.command, 'stop')
        assert rec.snapshot()['state'] == 'starting'
        release.set()
        assert started.result(2)[0] and stopped.result(2)[0]
    assert not cam.enabled and rec.snapshot()['state'] == 'idle'


def test_refresh_reports_only_historical_file_and_leaves_external_schedule_running(setup):
    cam, rec = setup
    cam.enabled = True
    assert rec.command('refresh')[0]
    status = rec.snapshot()
    assert status['last_card_clip'] == {'started_at_camera': '2026-11-09T07:30:11', 'size_bytes': 123}
    assert status['writing_confirmed'] is None and status['schedule_enabled']
    assert '/mnt' not in repr(status)
    rec.close()
    assert cam.enabled and not any('enable' in p for _,p in cam.calls)


def test_missing_record_index_keeps_separate_verified_schedule(setup):
    cam, rec = setup
    cam.hook = lambda endpoint, params: (400, '<fault/>') if endpoint == 'get_record_query' else None
    assert rec.command('refresh')[0]
    status = rec.snapshot()
    assert status['schedule_enabled'] is False and status['last_card_clip'] is None
    assert status['clip_query_detail'] and status['writing_confirmed'] is None


@pytest.mark.parametrize('response',[(302,'<uid>leak</uid>'), (200,'<' * 262145),
                                     (200,'<!DOCTYPE x><uid>bad</uid>'), (200,'bad XML')])
def test_does_not_accept_redirects_oversized_or_invalid_responses(setup,response):
    cam, rec = setup
    cam.hook = lambda endpoint, params: response if endpoint == 'getuid' else None
    assert not rec.command('start')[0]
    assert [name for name, _ in cam.calls] == ['getinfo', 'getuid']
    assert rec.snapshot()['schedule_enabled'] is None
