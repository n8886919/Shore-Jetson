from xml.etree import ElementTree as ET

import pytest

from shore_tracker.camera.control import CameraControls
from shore_tracker.controller import AppController
from shore_tracker.status import StatusStore


SETTINGS = '''<ImagingSettings><Brightness>128</Brightness><Exposure><Mode>AUTO</Mode><ExposureTime>1000</ExposureTime></Exposure><Focus><AutoFocusMode>MANUAL</AutoFocusMode></Focus></ImagingSettings>'''
OPTIONS = '''<ImagingOptions><Brightness><Min>1</Min><Max>255</Max></Brightness><Exposure><Mode>AUTO</Mode><Mode>MANUAL</Mode><ExposureTime><Min>100</Min><Max>100000</Max></ExposureTime></Exposure></ImagingOptions>'''


class Camera(CameraControls):
    def __init__(self):
        super().__init__(lambda: '192.0.2.2', sleeper=lambda _seconds: None)
        self.calls = []
        self.fail_move = False
        self.fail_stops = 0

    def _discover(self):
        self._urls = {'p': 'ptz', 'i': 'imaging'}
        self._zoom = self._focus = True
        self._profile = 'MainStream'
        self._source = 'VideoSourceMain'

    def _soap(self, url, body):
        self.calls.append((url, body))
        name = body.tag.rsplit('}', 1)[-1]
        if name in {'Move', 'ContinuousMove'} and self.fail_move:
            raise TimeoutError('sensitive connection detail')
        if name == 'Stop' and self.fail_stops:
            self.fail_stops -= 1
            raise TimeoutError('temporary STOP failure')
        if name == 'GetImagingSettings': return ET.fromstring('<Response>' + SETTINGS + '</Response>')
        if name == 'GetOptions': return ET.fromstring('<Response>' + OPTIONS + '</Response>')
        return ET.Element('Response')


@pytest.mark.parametrize('action', ['zoom_in', 'zoom_out', 'focus_near', 'focus_far'])
def test_lens_steps_always_stop_without_pan_tilt_motion(action):
    camera = Camera()
    camera.command(action, {})
    first, last = camera.calls[0][1], camera.calls[-1][1]
    assert first.find('.//{*}PanTilt') is None
    assert last.tag.endswith('Stop')
    if action.startswith('zoom'):
        assert first.findtext('{*}Timeout') == 'PT1S'
        assert last.findtext('{*}PanTilt') == 'false'
        assert last.findtext('{*}Zoom') == 'true'
    else:
        speed = float(first.findtext('.//{*}Speed'))
        assert speed == (-1 if action == 'focus_near' else 1)


def test_move_timeout_still_attempts_stop_and_retries():
    camera = Camera()
    camera.fail_move = True
    camera.fail_stops = 2
    with pytest.raises(TimeoutError): camera.command('zoom_in', {})
    assert [node.tag.rsplit('}', 1)[-1] for _, node in camera.calls] == ['ContinuousMove', 'Stop', 'Stop', 'Stop']


@pytest.mark.parametrize('changes', [{'brightness': 0}, {'brightness': 256}, {'brightness': True}, {'brightness': float('nan')}, {'exposure_mode': 'BAD'}, {'password': 'hidden'}])
def test_invalid_settings_never_reach_set_imaging(changes):
    camera = Camera()
    with pytest.raises(ValueError): camera.command('settings', changes)
    assert not any(node.tag.endswith('SetImagingSettings') for _, node in camera.calls)


def test_partial_exposure_update_preserves_mode_and_does_not_persist():
    camera = Camera()
    camera.command('settings', {'exposure_time': 2000})
    node = camera.calls[-1][1]
    assert node.findtext('.//{*}Exposure/{*}Mode') == 'AUTO'
    assert node.findtext('.//{*}Exposure/{*}ExposureTime') == '2000'
    assert node.find('.//{*}Brightness') is None
    assert node.findtext('{*}ForcePersistence') == 'false'


def test_read_only_snapshot_has_no_camera_address_or_credentials():
    camera = Camera()
    snapshot = camera.snapshot()
    assert snapshot['zoom_factor'] is None
    assert snapshot['fields'][0]['value'] == 128
    assert not any(node.tag.endswith(('Move', 'Stop', 'SetImagingSettings')) for _, node in camera.calls)


def test_tracking_blocks_camera_changes_but_allows_lens_stop():
    status = StatusStore()
    for name in ('camera', 'inference', 'servo'): status.update_component(name, 'ready')
    status.arm()
    camera = Camera()
    controller = AppController(status, camera_controls=camera)
    assert controller.camera_command('zoom_in', {})[0] is False
    assert camera.calls == []
    assert controller.camera_command('stop', {})[0] is True


def test_camera_exception_details_are_not_exposed():
    camera = Camera()
    camera.fail_move = True
    controller = AppController(StatusStore(), camera_controls=camera)
    ok, detail = controller.camera_command('focus_far', {})
    assert not ok
    assert 'sensitive' not in detail
