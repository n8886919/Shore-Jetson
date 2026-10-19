# Architecture

```text
Camera -- dedicated Ethernet --> Jetson --> local 1024x600 kiosk
                                   |-- full-frame recognition and tracking
                                   |-- ten-minute MKV, no audio
                                   `-- 40-pin UART (M0 pulses) --> controller --> GXServo 180
```

The runtime begins DISARMED. A local mouse ARM starts search and pan control;
STOP, camera loss, stale inference, Servo write failure, disk stop threshold or
process shutdown disarms it. A systemd restart never re-arms it.

The Jetson sends M0 pulse commands at 9600 8N1 directly to the controller.
GXServo remains 0-180 degrees with centre 90. No Station is in this control path.
A 20 Hz host planner bounds speed/acceleration; one serial writer sends only the
latest target. ARM/STOP are local gates. Startup/re-ARM waits for a fresh target.
STOP clears the target and waits for an in-progress bounded write to finish.
Targets aged 250 ms are rejected, tracking disarms and errors latch until restart.
The controller has no established watchdog or ACK: the servo can complete its
last target after host STOP. Requested position is not shaft feedback.
See [hardware](hardware.md) and [source boundary](shore-spotter.md).

Stable chasing starts an unqualified trial with no pan target. Only a surfer
previously seen chasing can later win the first-takeoff/ride lock. People first
seen already taking off/surfing are not acquired. Takeoff/surfing qualification
is latched for clip retention, while both scores falling below threshold for
the configured grace period ends the ride (even if chasing becomes high).
The chosen tracker cannot be replaced mid-ride. After completion or prolonged
loss, the state returns to searching and can choose a new surfer.

Pan is proportional to horizontal full-frame error and remains bounded by the
configured search range and slew rate. Detection always consumes the original
wide frame. Digital kiosk zoom is slow-in/fast-out: subject speed, image-edge
proximity and inference cadence can all force scale 1.0, the widest FOV.

`TrackingCoordinator.on_decision` feeds `RecordingTriggers` after target selection.
A separate worker starts the event recorder during the chasing trial and
finishes on abandoned trial, ride completion, loss timeout, DISARM or stale
inference. An automatic clip that never qualified is deleted only after the
recorder has stopped; its discard metadata is retained and not uploaded. The manual
button is an additional independent request. Both requests share one event clip;
releasing either one never cuts a clip that the other still needs. Finalizing an
old clip does not discard a pending new request. Recording IO is not performed
in the inference callback.

Finalized event clips, trigger/target metadata and SHA-256 are exposed only on
the loopback API. The computer receiver uses an SSH tunnel and the shared
`surf_track.drive.upload` transport to upload them privately. The receiver
reconnects/retries while it is running, and does not delete uploaded originals. Opening
the web UI with `deploy/open-web` also starts the receiver if necessary.

The continuous 600-second original-stream MKV sequence remains a separate local
backup without automatic upload. Neither recording path includes audio.

The deployment enables a dedicated camera NetworkManager profile at boot,
loads a narrow nftables route guard, runs the app as an unprivileged account
with systemd filesystem hardening, and launches the local kiosk. These startup
files do not prove a decoded stream, inference speed or physical movement.

## Independent pan control

Inference only publishes the latest decision. A separate 20 Hz loop integrates
angular velocity with the configured 30 degrees/s speed and 60 degrees/s²
acceleration limits on requested angles. These are host trajectory bounds,
not physical feedback. Gain uses a fixed 0.20 second reference; inference FPS
does not multiply controller strength. The writer retains only the freshest
target and never queues a history of positions. The old Station speed settings
and watchdog are not part of the direct M0 path.

A missing locked rider immediately holds the last commanded angle. The single five-second `tracking.ride_exit_seconds` setting controls both ID retention and ride exit. ID age starts at its last matched detection; ride age starts at its last valid takeoff/surfing observation. Expiry is checked before accepting a late recovery. Ride exit owns release and recording finalization; trial and cooldown remain two seconds. An inference stall disarms through the existing freshness limit; STOP clears cached decisions before re-ARM. Return to 90 degrees uses bounded acceleration and braking. This is commanded trajectory smoothing, not measured shaft feedback or extra image inference.

## Detection overlay

The status API publishes every current detection above the configured detector threshold, with normalized full-frame boxes and all action names above their individual tracking thresholds. This display is independent of target acquisition and ARM. Detections without a qualifying action retain a box and tracker ID without inventing a state. The SVG overlay accounts for image letterboxing and digital zoom; it expires with observation freshness. Preview delivery and raw recordings remain independent of these browser annotations.
