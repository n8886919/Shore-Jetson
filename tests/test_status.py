from shore_tracker.status import StatusStore


def test_boot_is_disarmed_and_not_armable() -> None:
    status = StatusStore()
    snapshot = status.snapshot()
    assert snapshot["tracking"]["armed"] is False
    assert snapshot["arm_allowed"] is False


def test_camera_and_inference_frame_ages_are_independent() -> None:
    status = StatusStore()
    status.update_metrics(camera_frame_age_ms=125)
    status.update_metrics(inference_frame_age_ms=40)

    metrics = status.snapshot()["metrics"]
    assert metrics["camera_frame_age_ms"] == 125
    assert metrics["inference_frame_age_ms"] == 40
    assert "last_frame_age_ms" not in metrics


def test_all_required_components_must_be_ready() -> None:
    status = StatusStore()
    for component in ("camera", "inference", "servo", "recording"):
        status.update_component(component, "ready")
    assert status.arm() == (True, "armed")
    assert status.snapshot()["tracking"]["armed"] is True


def test_fault_disarms_without_rearming_on_recovery() -> None:
    status = StatusStore()
    for component in ("camera", "inference", "servo", "recording"):
        status.update_component(component, "ready")
    status.arm()
    status.update_component("servo", "offline", "watchdog expired")
    assert status.snapshot()["tracking"]["armed"] is False
    status.update_component("servo", "ready")
    assert status.snapshot()["tracking"]["armed"] is False


def test_fault_notifies_hardware_stop_once_after_arm() -> None:
    status = StatusStore()
    reasons = []
    status.add_disarm_listener(reasons.append)
    for component in ("camera", "inference", "servo", "recording"):
        status.update_component(component, "ready")
    status.arm()

    status.update_component("camera", "error", "stream failed")
    status.update_component("recording", "paused", "stream failed")

    assert reasons == ["camera: error"]
    assert status.snapshot()["tracking"]["armed"] is False


def test_any_non_ready_critical_transition_disarms() -> None:
    for degraded_state in ("waiting", "starting"):
        status = StatusStore()
        reasons = []
        status.add_disarm_listener(reasons.append)
        for component in ("camera", "inference", "servo", "recording"):
            status.update_component(component, "ready")
        status.arm()

        status.update_component("camera", degraded_state)

        assert status.snapshot()["tracking"]["armed"] is False
        assert reasons == [f"camera: {degraded_state}"]


def test_disarm_listener_failure_cannot_undo_local_disarm() -> None:
    status = StatusStore()
    for component in ("camera", "inference", "servo", "recording"):
        status.update_component(component, "ready")
    status.add_disarm_listener(lambda _reason: (_ for _ in ()).throw(OSError()))
    status.arm()

    status.disarm("test")

    assert status.snapshot()["tracking"]["armed"] is False


def test_real_arm_request_waits_for_confirmation() -> None:
    status = StatusStore()
    for component in ("camera", "inference", "servo", "recording"):
        status.update_component(component, "ready")
    assert status.request_arm() == (True, "arming")
    assert status.snapshot()["tracking"]["armed"] is False
    assert status.request_arm()[0] is False
    status.confirm_armed()
    assert status.snapshot()["tracking"]["armed"] is True
    assert status.request_arm()[0] is False


def test_all_detection_boxes_and_thresholded_states_are_exposed_without_arm(monkeypatch):
    from shore_tracker.inference import ActionPrediction,DetectionPrediction,NormalizedBox,TrackObservation
    now=[10.0]
    monkeypatch.setattr('shore_tracker.status.time.monotonic',lambda:now[0])
    status=StatusStore()
    status.update_component('camera','ready');status.update_component('inference','ready')
    def person(i,confidence,chase,takeoff,surf):
        return TrackObservation(DetectionPrediction(i,NormalizedBox(.1,.2,.2,.3),confidence),ActionPrediction(i,chase,takeoff,surf))
    status.update_observations([person(1,.9,.7,.1,.1),person(2,.8,.1,.65,.7),person(3,.7,.1,.1,.1),person(4,.49,.9,.9,.9)])
    data=status.snapshot()['recognition']
    assert [item['track_id'] for item in data['detections']]==[1,2,3]
    assert [item['states'] for item in data['detections']]==[['chasing_wave'],['takeoff','surfing'],[]]
    assert data['detections'][0]['box']=={'x':.1,'y':.2,'width':.2,'height':.3}
    assert not status.snapshot()['tracking']['armed']
    status.configure_recognition(detection=.85,chasing_wave=.8)
    items=status.snapshot()['recognition']['detections']
    assert len(items)==1 and items[0]['states']==[]
    now[0]=11.5
    assert status.snapshot()['recognition']['detections']==[]
    assert not status.snapshot()['recognition']['available']
    assert status.snapshot()['recognition']['valid_for_ms']==0
