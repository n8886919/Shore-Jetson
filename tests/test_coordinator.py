from dataclasses import replace
import pytest

from shore_tracker.config import ServoConfig, TrackingConfig, ZoomConfig
from shore_tracker.coordinator import TrackingCoordinator
from shore_tracker.inference import ActionPrediction, DetectionPrediction, NormalizedBox, TrackObservation
from shore_tracker.status import StatusStore


def process_and_control(coordinator, observations, *, now):
    decision = coordinator.process_frame(observations, now=now)
    coordinator.control_step(now=now)
    return decision


def observation(center: float, *, chasing=0.9, takeoff=0.0, surfing=0.0):
    return TrackObservation(
        DetectionPrediction(1, NormalizedBox(center - 0.05, 0.4, 0.1, 0.2), 0.95),
        ActionPrediction(1, chasing, takeoff, surfing),
    )


def ready_status() -> StatusStore:
    status = StatusStore()
    for name in ("camera", "inference", "servo"):
        status.update_component(name, "ready")
    status.arm()
    return status


def test_p_control_uses_local_commanded_angle_without_state_sync() -> None:
    status = ready_status()
    commands = []
    coordinator = TrackingCoordinator(
        replace(TrackingConfig(), candidate_hold_seconds=0),
        replace(ServoConfig(), pan_direction=1),
        status,
        commands.append,
        ZoomConfig(),
    )
    process_and_control(coordinator, [observation(0.8)], now=0.0)
    process_and_control(coordinator, [observation(0.8)], now=0.1)
    assert commands[-1] == 90.0
    process_and_control(coordinator, [observation(0.8)], now=0.2)
    assert commands[-1] == 90.0
    for now in (.3,.4,.5): process_and_control(coordinator, [observation(.8,takeoff=.9)],now=now)
    assert commands[-1] > 90.0


def test_fast_target_forces_wider_fov() -> None:
    status = ready_status()
    coordinator = TrackingCoordinator(
        replace(TrackingConfig(), candidate_hold_seconds=0),
        replace(ServoConfig(), pan_direction=1),
        status,
        lambda _angle: None,
        replace(ZoomConfig(), zoom_in_per_second=10.0),
    )
    process_and_control(coordinator, [observation(0.50)], now=0.0)
    process_and_control(coordinator, [observation(0.51)], now=0.1)
    process_and_control(coordinator, [observation(0.51)], now=0.2)
    for now in (.3,.4,.5,.6): process_and_control(coordinator, [observation(.51,takeoff=.9)],now=now)
    assert status.snapshot()["metrics"]["zoom_scale"] > 1.0
    process_and_control(coordinator, [observation(0.80,takeoff=.9)], now=0.7)
    assert status.snapshot()["metrics"]["zoom_scale"] == 1.0


def test_zero_faces_right_and_direction_is_fixed():
    status = ready_status()
    commands = []
    coordinator = TrackingCoordinator(
        replace(TrackingConfig(), candidate_hold_seconds=0),
        replace(ServoConfig(), pan_direction=-1), status, commands.append,
    )
    for now in (0, 0.1, 0.2):
        process_and_control(coordinator, [observation(0.8)], now=now)
    assert commands[-1] == 90
    for now in (.3,.4,.5): process_and_control(coordinator, [observation(.8,takeoff=.9)],now=now)
    assert commands[-1] < 90
    with pytest.raises(ValueError, match="固定"):
        coordinator.update_tuning(pan_gain_deg=45, pan_deadband=.125, pan_direction=1, zoom_maximum=2.5)


def test_action_scores_visible_before_lock_and_when_disarmed():
    status = ready_status()
    commands = []
    coordinator = TrackingCoordinator(TrackingConfig(), replace(ServoConfig(), pan_direction=-1), status, commands.append)
    sample = observation(.8, chasing=.2, takeoff=.6, surfing=.3)
    process_and_control(coordinator, [sample], now=1.0)
    snapshot = status.snapshot()
    assert snapshot['tracking']['target_track_id'] is None
    assert snapshot['recognition']['chasing_wave'] == .2
    assert snapshot['recognition']['source'] == 'candidate'
    assert commands[-1] == 90
    status.disarm('test')
    command_count = len(commands)
    process_and_control(coordinator, [sample], now=1.1)
    assert status.snapshot()['recognition']['takeoff'] == .6
    assert len(commands) == command_count


def test_no_detection_and_stale_image_do_not_report_zero_scores():
    status = ready_status()
    coordinator = TrackingCoordinator(TrackingConfig(), replace(ServoConfig(), pan_direction=-1), status, lambda angle: None)
    process_and_control(coordinator, [], now=0)
    recognition = status.snapshot()['recognition']
    assert recognition['available']
    assert recognition['detection_count'] == 0
    assert recognition['chasing_wave'] is None
    process_and_control(coordinator, [observation(.5)], now=.1)
    status.update_component('inference', 'stale')
    recognition = status.snapshot()['recognition']
    assert not recognition['available']
    assert recognition['detection_count'] is None
    assert recognition['chasing_wave'] is None


def test_locked_target_scores_take_priority_over_more_confident_other_person():
    status = ready_status()
    coordinator = TrackingCoordinator(replace(TrackingConfig(), candidate_hold_seconds=0), replace(ServoConfig(), pan_direction=-1), status, lambda angle: None)
    for now in (0,.1,.2): process_and_control(coordinator, [observation(.5)], now=now)
    for now in (.3,.4,.5): process_and_control(coordinator, [observation(.5,takeoff=.9)],now=now)
    other = TrackObservation(DetectionPrediction(2, NormalizedBox(.1,.1,.1,.2), .99), ActionPrediction(2,.1,.8,.1))
    process_and_control(coordinator, [other,observation(.5,takeoff=.9)], now=.6)
    recognition = status.snapshot()['recognition']
    assert recognition['detection_count'] == 2
    assert recognition['source'] == 'locked'
    assert recognition['track_id'] == 1
    assert recognition['chasing_wave'] == .9


@pytest.mark.parametrize('action,expected', [('takeoff','takeoff'),('surfing','surfing')])
def test_already_active_surfer_without_prior_chasing_is_ignored(action, expected):
    status = ready_status()
    commands = []
    coordinator = TrackingCoordinator(TrackingConfig(), replace(ServoConfig(), pan_direction=-1), status, commands.append)
    sample = observation(.8, chasing=.1, **{action: .9})
    process_and_control(coordinator, [sample], now=0)
    process_and_control(coordinator, [sample], now=.25)
    assert status.snapshot()['tracking']['target_track_id'] is None
    assert commands[-1] == 90
    process_and_control(coordinator, [sample], now=.5)
    assert status.snapshot()['tracking']['mode'] == 'searching'
    assert status.snapshot()['tracking']['target_track_id'] is None
    assert commands[-1] == 90
    process_and_control(coordinator, [], now=.75)
    assert commands[-1] == 90


def test_single_surfing_spike_does_not_trigger_following():
    status = ready_status()
    commands = []
    coordinator = TrackingCoordinator(TrackingConfig(), replace(ServoConfig(), pan_direction=-1), status, commands.append)
    for now, score in [(0,.9),(.25,.2),(.5,.9),(.75,.9)]:
        process_and_control(coordinator, [observation(.8,chasing=.1,surfing=score)],now=now)
        assert status.snapshot()['tracking']['target_track_id'] is None
        assert commands[-1] == 90


def test_locked_target_loss_holds_until_timeout_then_returns_home():
    status = ready_status()
    commands = []
    coordinator = TrackingCoordinator(
        replace(TrackingConfig(), candidate_hold_seconds=0),
        replace(ServoConfig(), pan_direction=-1), status, commands.append,
    )
    for now in (0, .1, .2):
        process_and_control(coordinator, [observation(.8)], now=now)
    for now in (.3, .4, .5):
        process_and_control(coordinator, [observation(.8, chasing=0, takeoff=.9)], now=now)
    locked_angle = commands[-1]
    assert locked_angle < 90
    missing = process_and_control(coordinator, [], now=.6)
    assert missing.reason.value == 'temporarily_lost'
    assert commands[-1] == locked_angle
    process_and_control(coordinator, [observation(.8, chasing=0, takeoff=.9)], now=.7)
    resumed_angle = commands[-1]
    assert resumed_angle < locked_angle
    process_and_control(coordinator, [], now=.8)
    held = process_and_control(coordinator, [], now=2.3)
    assert held.reason.value == 'temporarily_lost'
    assert commands[-1] == resumed_angle
    still_held = process_and_control(coordinator, [], now=5.69)
    assert still_held.reason.value == 'temporarily_lost'
    assert commands[-1] == resumed_angle
    ended = process_and_control(coordinator, [], now=5.71)
    assert ended.state.value == 'finished'
    assert resumed_angle < commands[-1] <= 90


def test_twenty_hz_motion_is_identical_at_five_and_ten_inference_fps():
    def run(frame_every_ticks):
        status = ready_status()
        commands = []
        coordinator = TrackingCoordinator(
            replace(TrackingConfig(), candidate_hold_seconds=0),
            replace(ServoConfig(), pan_direction=-1, pan_gain_deg=11), status, commands.append,
        )
        for now in (0, .05, .10): coordinator.process_frame([observation(.8)], now=now)
        for now in (.15, .20, .25): coordinator.process_frame([observation(.8,takeoff=.9)], now=now)
        assert commands == []  # Inference never writes motor angles.
        for tick in range(21):
            now = .25 + tick*.05
            if tick % frame_every_ticks == 0:
                coordinator.process_frame([observation(.8,takeoff=.9)], now=now)
            coordinator.control_step(now=now)
        return commands
    slow, fast = run(4), run(2)
    assert len(slow) == len(fast) == 21
    assert slow == pytest.approx(fast)
    assert all(b < a for a,b in zip(slow,slow[1:]))
    speeds = [(b-a)/.05 for a,b in zip(slow,slow[1:])]
    assert all(abs(v) <= 30 for v in speeds)
    assert all(abs(b-a) <= 60*.05+1e-8 for a,b in zip(speeds,speeds[1:]))


def test_control_tick_stops_on_inference_stall_and_needs_new_frame_after_stop():
    status = ready_status()
    commands = []
    coordinator = TrackingCoordinator(replace(TrackingConfig(),candidate_hold_seconds=0),
        replace(ServoConfig(),pan_direction=-1),status,commands.append)
    status.add_disarm_listener(coordinator.cancel_tracking)
    for now in (0,.1,.2):coordinator.process_frame([observation(.8)],now=now)
    for now in (.3,.4,.5):coordinator.process_frame([observation(.8,takeoff=.9)],now=now)
    coordinator.control_step(now=.5)
    coordinator.control_step(now=.55)
    assert commands[-1] < 90
    before = len(commands)
    coordinator.control_step(now=2.1)
    assert len(commands) == before
    assert not status.snapshot()['tracking']['armed']
    status.update_component('inference','ready')
    status.arm()
    coordinator.control_step(now=2.2)
    assert len(commands) == before


def test_real_control_thread_moves_between_frames_and_joins():
    import time
    status = ready_status()
    commands = []
    coordinator = TrackingCoordinator(replace(TrackingConfig(),candidate_hold_seconds=0),
        replace(ServoConfig(),pan_direction=-1),status,commands.append)
    base = time.monotonic()
    for _ in range(3):coordinator.process_frame([observation(.8)],now=base)
    for _ in range(3):coordinator.process_frame([observation(.8,takeoff=.9)],now=base)
    coordinator.start()
    try:
        deadline = time.monotonic()+.6
        while len(commands)<4 and time.monotonic()<deadline:time.sleep(.01)
        assert len(commands)>=4
        assert commands[-1] < commands[0]
    finally:
        assert coordinator.close()
    size = len(commands)
    time.sleep(.06)
    assert len(commands) == size


def test_fifty_hz_motion_stays_smooth_with_two_hz_inference():
    def run(frame_every_ticks):
        status=ready_status();commands=[]
        coordinator=TrackingCoordinator(replace(TrackingConfig(),candidate_hold_seconds=0),
            replace(ServoConfig(),pan_direction=1,send_hz=50,max_slew_deg_s=5,
                    max_acceleration_deg_s2=10),status,commands.append)
        for now in (0,.05,.10):coordinator.process_frame([observation(.8)],now=now)
        for now in (.15,.20,.25):coordinator.process_frame([observation(.8,takeoff=.9)],now=now)
        for tick in range(101):
            now=.25+tick*.02
            if tick%frame_every_ticks==0:
                coordinator.process_frame([observation(.8,takeoff=.9)],now=now)
            coordinator.control_step(now=now)
        return commands
    two_fps,ten_fps=run(25),run(5)
    assert two_fps == pytest.approx(ten_fps)
    assert len(two_fps)==101
    speeds=[(b-a)/.02 for a,b in zip(two_fps,two_fps[1:])]
    assert all(0<v<=5+1e-8 for v in speeds)
    assert all(abs(b-a)<=10*.02+1e-8 for a,b in zip(speeds,speeds[1:]))
    # Every intervening tick moves; there are no 500-ms holds awaiting inference.
    assert all(b>a for a,b in zip(two_fps,two_fps[1:]))


def test_stop_callback_does_not_wait_for_control_lock():
    import threading
    coordinator = TrackingCoordinator(TrackingConfig(),replace(ServoConfig(),pan_direction=-1),ready_status(),lambda angle: None)
    completed = threading.Event()
    def stop():
        coordinator.cancel_tracking()
        completed.set()
    with coordinator._lock:
        thread = threading.Thread(target=stop)
        thread.start()
        returned_without_control_lock = completed.wait(.3)
    thread.join(timeout=1)
    assert returned_without_control_lock


@pytest.mark.parametrize('deadband', [.05, .125, .4])
def test_tolerance_total_percent_accepts_ten_to_eighty(deadband):
    status=StatusStore()
    c=TrackingCoordinator(TrackingConfig(),replace(ServoConfig(),pan_direction=-1),status,lambda angle:None)
    c.update_tuning(pan_gain_deg=45,pan_deadband=deadband,pan_direction=-1,zoom_maximum=1)
    assert c.planner.config.deadband == deadband
    assert c.tilt_planner.config.deadband == deadband
    assert status.snapshot()['tuning']['pan_deadband'] == deadband


@pytest.mark.parametrize('deadband', [.049, .401, -1, float('nan')])
def test_tolerance_rejects_values_outside_total_percent_range(deadband):
    c=TrackingCoordinator(TrackingConfig(),replace(ServoConfig(),pan_direction=-1),StatusStore(),lambda angle:None)
    with pytest.raises(ValueError,match='10%–80%'):
        c.update_tuning(pan_gain_deg=45,pan_deadband=deadband,pan_direction=-1,zoom_maximum=1)
