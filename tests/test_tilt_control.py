"""Two-axis control on fake UART only; these tests never connect to hardware."""
from dataclasses import replace

import pytest

from shore_tracker.config import AppConfig, ConfigError, ServoConfig, TrackingConfig, validate_config
from shore_tracker.control import PanPlanner, PanPlannerConfig
from shore_tracker.coordinator import TrackingCoordinator
from shore_tracker.inference import (ActionPrediction, DetectionPrediction, FollowDecision,
    FollowDecisionReason, FollowState, NormalizedBox, TrackObservation)
from shore_tracker.servo.pulse_controller import encode_position
from shore_tracker.servo.simple import SimpleServoClient
from shore_tracker.status import StatusStore
from tools.servo_motion_test import run


def decision(y):
    item = TrackObservation(DetectionPrediction(1, NormalizedBox(.45, y-.05, .1, .1), .99),
                            ActionPrediction(1, .99, .99, .99))
    return FollowDecision(FollowState.SURFING, 1, item, FollowDecisionReason.TRACKING)


def planner():
    return PanPlanner(PanPlannerConfig(angle_min_deg=85, angle_max_deg=95,
        search_min_deg=85, search_max_deg=95, home_deg=90, max_slew_deg_s=5,
        search_speed_deg_s=2, max_acceleration_deg_s2=10, pan_gain_deg=45,
        deadband=.04, direction=-1, axis='tilt'))


@pytest.mark.parametrize('y,sign', [(.2, 1), (.8, -1), (.5, 0), (.53, 0)])
def test_tilt_uses_vertical_center_not_horizontal(y, sign):
    p=planner();d=decision(y)
    p.step(now=0, current_angle_deg=90, decision=d, armed=True)
    angle=p.step(now=.1, current_angle_deg=90, decision=d, armed=True)
    assert angle == pytest.approx(90+sign*.1)


@pytest.mark.parametrize('y,bound', [(.2,95), (.8,85)])
def test_tilt_speed_acceleration_bounds_and_search_hold(y, bound):
    p=planner();angle=90.;velocity=0.
    for i in range(201):
        next_angle=p.step(now=i*.02,current_angle_deg=angle,decision=decision(y),armed=True)
        next_velocity=(next_angle-angle)/.02
        assert 85 <= next_angle <= 95
        assert abs(next_velocity) <= 5+1e-8
        if next_angle != bound: assert abs(next_velocity-velocity) <= .2+1e-8
        angle,velocity=next_angle,next_velocity
    assert angle == bound
    searching=FollowDecision(FollowState.SEARCHING,None,None,FollowDecisionReason.SEARCHING)
    assert p.step(now=4.1,current_angle_deg=angle,decision=searching,armed=True) == angle


def test_atomic_axes_stop_disable_and_expiration(monkeypatch):
    writes=[];now=[0.]
    class Writer:
        def open(self): pass
        def close(self): pass
        def write(self, payload): writes.append(payload)
    client=SimpleServoClient(Writer(),angle_min_deg=0,angle_max_deg=180,
                             protocol='m0_pulse',clock=lambda:now[0])
    monkeypatch.setattr(client,'_run',lambda:None)
    client.start()
    try:
        client.set_target_degrees(90,tilt_degrees=91);client._send_target()
        client.arm();client._send_target();assert writes == []
        client.set_target_degrees(90,tilt_degrees=91);client._send_target()
        assert writes == [b'#00P1500!#01P1511!']
        client.disable_tilt();client._send_target();assert writes[-1] == b'#00P1500!'
        client.set_target_degrees(90,tilt_degrees=92);client.stop_motion()
        count=len(writes);client.arm();client._send_target();assert len(writes) == count
        client.set_target_degrees(90,tilt_degrees=92);now[0]=.25;client._send_target()
        assert len(writes) == count and not client.snapshot().armed
        assert client.snapshot().last_error == 'target_expired'
    finally: client.close()


@pytest.mark.parametrize('angle',[84.9,95.1,float('nan')])
def test_transport_rejects_unsafe_tilt(angle):
    client=SimpleServoClient(None,angle_min_deg=0,angle_max_deg=180,protocol='m0_pulse')
    with pytest.raises(ValueError,match='Tilt target'): client.set_target_degrees(90,tilt_degrees=angle)


def test_coordinator_enable_disable_readback_and_stale_stop():
    status=StatusStore()
    for c in ('camera','inference','servo'): status.update_component(c,'ready')
    status.arm();commands=[];disabled=[]
    c=TrackingCoordinator(TrackingConfig(),replace(ServoConfig(),pan_direction=-1,tilt_direction=-1),
        status,lambda angle:None,command_axes=lambda pan,tilt:commands.append((pan,tilt)),
        disable_tilt=lambda:disabled.append(True))
    c.process_frame([],now=0);c._decision=decision(.2)
    c.control_step(now=0);assert commands[-1] == (90,None)
    tuning=dict(pan_gain_deg=45,pan_deadband=.125,pan_direction=-1,zoom_maximum=1)
    c.update_tuning(**tuning,tilt_enabled=True)
    assert status.snapshot()['tuning']['tilt_enabled']
    c.control_step(now=.1);c.control_step(now=.2)
    assert commands[-1][1] > 90
    last=c.current_tilt_deg
    c.update_tuning(**tuning,tilt_enabled=False)
    assert disabled == [True]
    c.control_step(now=.3);assert commands[-1][1] is None and c.current_tilt_deg == last
    # Old clients may omit tilt_enabled, preserving the current flag.
    c.update_tuning(**tuning,tilt_enabled=True);c.update_tuning(**tuning)
    assert status.snapshot()['tuning']['tilt_enabled']
    c.control_step(now=.4);assert commands[-1][1] == last
    count=len(commands);c.control_step(now=2)
    assert not status.snapshot()['tracking']['armed'] and len(commands) == count


def test_uncalibrated_or_missing_transport_never_enables_tilt():
    c=TrackingCoordinator(TrackingConfig(),ServoConfig(),StatusStore(),lambda angle:None)
    assert not c.status.snapshot()['tuning']['tilt_available']
    with pytest.raises(ValueError,match='尚未校準'):
        c.update_tuning(pan_gain_deg=45,pan_deadband=.125,pan_direction=1,zoom_maximum=1,tilt_enabled=True)


@pytest.mark.parametrize('changes', [dict(tilt_enabled=True),dict(tilt_min_deg=96),
    dict(tilt_home_deg=100),dict(tilt_direction=2),dict(tilt_max_deg=float('nan'))])
def test_invalid_tilt_configuration(changes):
    with pytest.raises(ConfigError): validate_config(replace(AppConfig(),servo=replace(ServoConfig(),**changes)))


def test_channel_encoding_rejects_unknown_channels():
    assert encode_position(90,channel=1) == b'#01P1500!'
    for channel in (True,-1,2,'1'):
        with pytest.raises(ValueError): encode_position(90,channel=channel)


def test_out2_calibration_test_never_sends_out1():
    writes=[];now=[0.]
    class Port:
        in_waiting=0
        def read(self,count): return b''
        def write(self,payload): writes.append(payload);return len(payload)
    def sleep(seconds): now[0] += max(seconds,1e-9)
    result=run(Port(),channels=(1,),clock=lambda:now[0],sleep=sleep)
    assert result['channels'] == [1] and result['commands'] == 57
    assert all(p.startswith(b'#01P') for p in writes)
    assert writes[0] == writes[-1] == b'#01P1500!'


def test_swapped_wiring_routes_pan_and_tilt_to_configured_channels(monkeypatch):
    writes=[]
    class Writer:
        def open(self): pass
        def close(self): pass
        def write(self,payload): writes.append(payload)
    client=SimpleServoClient(Writer(),angle_min_deg=0,angle_max_deg=180,protocol='m0_pulse',
                             pan_channel=1,tilt_channel=0)
    monkeypatch.setattr(client,'_run',lambda:None);client.start()
    try:
        client.arm();client.set_target_degrees(80,tilt_degrees=91);client._send_target()
        assert writes[-1] == b'#01P1389!#00P1511!'
        client.disable_tilt();client._send_target()
        assert writes[-1] == b'#01P1389!'
    finally: client.close()


@pytest.mark.parametrize('channels',[(0,0),(1,1),(-1,1),(0,2),(True,0)])
def test_duplicate_or_unknown_axis_channels_are_rejected(channels):
    pan,tilt=channels
    with pytest.raises(ConfigError,match='distinct'):
        validate_config(replace(AppConfig(),servo=replace(ServoConfig(),pan_channel=pan,tilt_channel=tilt)))
    with pytest.raises(ValueError,match='distinct'):
        SimpleServoClient(None,angle_min_deg=0,angle_max_deg=180,pan_channel=pan,tilt_channel=tilt)
