# Inference, lock and FOV

The detector produces normalized full-frame surfer boxes. A second model emits
three independent probabilities in this order: `chasing_wave`, `takeoff`, and
`surfing`. An IoU tracker assigns the IDs used by the state machine.

A chasing candidate must remain stable before acquisition. Once selected, the
same ID stays locked through takeoff and surfing; another person cannot steal
the lock. One `tracking.ride_exit_seconds` setting defaults to five seconds. The IoU tracker
retains an unmatched ID for that duration after its last detection, independent
of FPS; expired IDs are removed before matching. The ride uses the same duration
from the last valid takeoff/surfing observation. Missing frames and low action
scores do not reset the deadline; only the same rider recovering before expiry
does. While waiting, visible low-score riders remain pan targets; missing riders
hold the last commanded angle. Expiry enters `finished` and finalizes the clip;
after the existing two-second cooldown the lock clears and search resumes.

Retention does not add motion prediction or re-identification: a returning box
must still satisfy IoU matching. The old `lost_grace_seconds` setting is retired;
legacy TOML files may contain it but it is ignored.

Pan error is `box_center_x - 0.5`. The P controller applies gain, deadband,
direction, angle limits and maximum slew. The UI exposes only gain, deadband
and maximum digital zoom.

The detector always uses the uncropped wide image. Kiosk zoom behaves as
framing, not as sensor or optical zoom:

- fast target, target near an edge, or slow inference: immediately use 1.0x;
- target lost, finished, or searching: use 1.0x;
- stable slow target near centre: gradually crop toward the configured size.

The POC runs the two exported ONNX models with the JetPack 7.2.1 native
TensorRT Python API. A small CUDA driver binding copies tensors without PyCUDA,
Docker, DeepStream or a DeepStream custom parser. Engine plans are built on the
target Jetson and cached locally because they are hardware/runtime specific.
Missing TensorRT, failed engine construction or inference errors keep the
system DISARMED. A real-frame GPU benchmark remains a commissioning gate.

## Rate control

`inference.max_fps` defaults to 2 Hz; valid values are 1–10 Hz. The kiosk control
page applies changes through `/api/inference/rate`, independently of preview FPS.
The worker limits model start frequency with interruptible waits and reads the
latest frame after each wait. Slow inference adds no catch-up queue. Displayed
FPS is measured between completed frames, and appears after the second result.
