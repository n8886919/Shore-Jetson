from types import SimpleNamespace
from shore_tracker.inference.confirmation import DetectionConfirmation


def test_requires_consecutive_same_id_and_resets_on_miss_and_new_run():
    gate=DetectionConfirmation(3)
    a,b=SimpleNamespace(track_id=1),SimpleNamespace(track_id=2)
    assert gate.update((a,)) == ()
    assert gate.update((a,b)) == ()
    assert gate.update((a,b)) == (a,)
    assert gate.update((b,)) == (b,)
    assert gate.update((a,)) == ()
    gate.reset()
    assert gate.update((a,)) == ()
