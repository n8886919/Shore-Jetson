from shore_tracker.recording_triggers import RecordingTriggers
from shore_tracker.inference import FollowDecision, FollowState, FollowDecisionReason


class Recorder:
    def __init__(self):
        self.state = 'idle'
        self.calls = []
        self.notes = []
        self.id = 0
    def snapshot(self): return {'state': self.state, 'id': self.id, 'detail': ''}
    def command(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == 'start': self.id += 1
        self.state = 'recording' if action == 'start' else 'finalizing'
        return True, 'ok'
    def note_trigger(self, *args, **kwargs): self.notes.append(args)
    def close(self): self.command('stop')


def decision(state=FollowState.SURFING, target_id=5):
    return FollowDecision(state, target_id, None, FollowDecisionReason.TRACKING, state in {FollowState.TAKEOFF,FollowState.SURFING,FollowState.FINISHED})


def setup():
    recorder = Recorder()
    now = [10.0]
    armed = [True]
    controller = RecordingTriggers(recorder, armed_reader=lambda: armed[0], clock=lambda: now[0])
    return recorder, controller, now, armed


def test_automatic_follow_starts_once_and_finish_finalizes():
    recorder, controller, now, _ = setup()
    controller.check_once()
    assert recorder.calls == []
    controller.follow(decision(), now[0])
    controller.check_once()
    controller.check_once()
    assert recorder.calls == [('start', {'trigger': 'tracking', 'target_track_id': 5})]
    # Temporary loss still retains the target ID and continues recording.
    assert recorder.state == 'recording'
    controller.follow(decision(FollowState.FINISHED), now[0])
    controller.check_once()
    assert recorder.calls[-1][0] == 'stop'


def test_manual_trigger_extends_auto_clip_and_auto_extends_manual_clip():
    recorder, controller, now, _ = setup()
    controller.follow(decision(), now[0]); controller.check_once()
    controller.command('start'); controller.check_once()
    controller.follow(decision(FollowState.FINISHED), now[0]); controller.check_once()
    assert len(recorder.calls) == 1
    assert recorder.state == 'recording'
    controller.follow(decision(), now[0]); controller.check_once()
    controller.command('stop'); controller.check_once()
    assert len(recorder.calls) == 1
    assert not controller.snapshot()['manual_requested']
    assert controller.snapshot()['tracking_requested']
    controller.follow(decision(FollowState.FINISHED), now[0]); controller.check_once()
    assert recorder.calls[-1][0] == 'stop'
    assert ('manual',) in recorder.notes
    assert ('tracking', 5) in recorder.notes


def test_pending_new_target_starts_after_previous_finalization():
    recorder, controller, now, _ = setup()
    controller.follow(decision(), now[0]); controller.check_once()
    controller.follow(decision(FollowState.FINISHED), now[0]); controller.check_once()
    controller.follow(decision(target_id=8), now[0]); controller.check_once()
    assert recorder.state == 'finalizing'
    assert len(recorder.calls) == 2
    recorder.state = 'idle'
    controller.check_once()
    assert recorder.calls[-1] == ('start', {'trigger': 'tracking', 'target_track_id': 8})


def test_disarm_and_stale_inference_release_auto_trigger():
    recorder, controller, now, armed = setup()
    controller.follow(decision(), now[0]); controller.check_once()
    armed[0] = False
    controller.check_once()
    assert recorder.calls[-1][0] == 'stop'
    recorder.state = 'idle'; armed[0] = True
    controller.follow(decision(), now[0]); controller.check_once()
    now[0] += 1.6
    controller.check_once()
    assert recorder.calls[-1][0] == 'stop'


def test_stop_tracking_preserves_additional_manual_recording():
    recorder, controller, now, armed = setup()
    controller.command('start'); controller.follow(decision(),now[0]); controller.check_once()
    armed[0] = False; controller.cancel_tracking(); controller.check_once()
    assert recorder.state == 'recording'
    controller.command('stop'); controller.check_once()
    assert recorder.state == 'finalizing'


def test_failed_recording_retries_with_backoff_and_no_new_user_action():
    recorder, controller, now, armed = setup()
    controller.follow(decision(), now[0]); controller.check_once()
    recorder.state = 'failed'
    controller.check_once()
    now[0] += 1
    controller.follow(decision(), now[0]); controller.check_once()
    assert len(recorder.calls) == 1
    now[0] += 4
    controller.follow(decision(), now[0]); controller.check_once()
    assert len(recorder.calls) == 2


def test_continuous_waits_for_arm_and_records_through_search_loss_and_finish():
    recorder,c,now,armed=setup();armed[0]=False
    assert c.set_continuous(True)[0]
    c.check_once();assert recorder.calls == []
    assert c.snapshot()['continuous_enabled'] and not c.snapshot()['continuous_requested']
    armed[0]=True;c.check_once();c.check_once()
    assert recorder.calls == [('start',{'trigger':'continuous','target_track_id':None})]
    for state in (FollowState.SEARCHING,FollowState.CHASING,FollowState.FINISHED):
        c.follow(decision(state),now[0]);c.check_once()
    now[0]+=10;c.check_once();assert len(recorder.calls)==1
    armed[0]=False;c.cancel_tracking();c.check_once()
    assert recorder.calls[-1][0]=='stop' and not c.snapshot()['continuous_requested']


def test_disabling_continuous_returns_to_automatic_without_cutting_active_chase():
    recorder,c,now,armed=setup()
    c.set_continuous(True);c.check_once()
    c.follow(decision(FollowState.CHASING),now[0]);c.set_continuous(False);c.check_once()
    assert len(recorder.calls)==1 and c.snapshot()['tracking_requested']
    c.follow(decision(FollowState.FINISHED),now[0]);c.check_once()
    assert recorder.calls[-1][0]=='stop'
    assert ('continuous',) in recorder.notes


def test_continuous_preference_survives_restart_without_starting_disarmed(tmp_path):
    path=tmp_path/'recording-settings.json'
    r=Recorder();c=RecordingTriggers(r,armed_reader=lambda:False,settings_path=path)
    assert c.set_continuous(True)[0]
    r2=Recorder();r2.recover=lambda:None
    restored=RecordingTriggers(r2,armed_reader=lambda:False,settings_path=path)
    try:
        restored.recover();restored.check_once()
        assert restored.snapshot()['continuous_enabled']
        assert r2.calls == []
    finally: restored.close()


def test_failed_preference_write_does_not_change_mode(tmp_path,monkeypatch):
    from pathlib import Path
    r=Recorder();c=RecordingTriggers(r,armed_reader=lambda:True,settings_path=tmp_path/'settings.json')
    def fail(*args): raise OSError('read only')
    monkeypatch.setattr(Path,'replace',fail)
    assert not c.set_continuous(True)[0]
    assert not c.snapshot()['continuous_enabled'] and r.calls == []


def test_continuous_rejects_non_boolean_settings():
    recorder,c,now,armed=setup()
    for value in (1,0,None,'true'):
        assert not c.set_continuous(value)[0]
    assert recorder.calls == [] and not c.snapshot()['continuous_enabled']
