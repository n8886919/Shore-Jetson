# Inference, lock and FOV

The detector produces normalized full-frame surfer boxes. A second model emits
three independent probabilities in this order: `chasing_wave`, `takeoff`, and
`surfing`. An IoU tracker assigns the IDs used by the state machine.
After score filtering and non-maximum suppression, at most 10 candidates per
frame reach the tracker and action classifier, in descending detector confidence.
This bounds the action batch; the detector still processes the entire image.

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

## Throughput

`inference.max_fps = 0` is the default: inference immediately takes the newest
available frame after completing a pass. There is no frequency setting in the
kiosk. Optional legacy diagnostic caps of 1–10 Hz remain supported by the API.
Slow inference never queues old frames. Reported inference FPS measures completed
results rather than configured limits or detector-only throughput.

## Start/stop and live display

Engines load at boot; AI recognition defaults off on every service start. The
AI switch can run recognition while tracking remains DISARMED. Starting tracking
automatically enables AI. AI can be disabled during tracking and recording:
PWM tracking stays armed in manual mode, pending automatic targets and old boxes are
cleared, and the PWM servos accept leased keyboard jogging. Enabling AI cancels
jogging and waits for fresh observations before automatic following resumes.
Recording no longer forces or locks AI; manual and continuous recording keep their
existing triggers, while event recording expires when AI decisions become stale.
STOP, AI switches, run changes and the existing freshness deadline invalidate late
results. The historical non-PWM paths still DISARM when AI is disabled.
Disabled AI does not trigger an inference-stale STOP; camera and UART
faults retain their fail-safe behavior. Keyboard limits are documented in
[the servo control contract](servo-controller.md#pwm-方向鍵手動控制).
`/api/display-frame` immediately samples the newest JPEG using its sequence
cursor, with no presentation buffer. Its metadata carries the latest valid
recognition for the current run; boxes may therefore describe a slightly older
image. This deliberately favors live video over exact image/box synchronization.
The browser commits the new image and available boxes together. STOP and stale
results clear overlays. Recording and Servo safety guards remain independent.

## Locked rider position on Nano

In decoded PWM mode the native preview frames supply up to the configured preview rate. A green-channel template search tracks one locked rider between detector frames. The matcher rejects weak contrast, ambiguous peaks, old samples and camera-generation changes. Optical positions can guide box association and pan/tilt between detections. Only detector/action results confirm a ride or extend its deadline. Other input modes use the detector-only path.
