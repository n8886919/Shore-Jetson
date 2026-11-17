# Architecture

The owner reverted to **two 180° PWM servos through CH340G USB-TTL and M0/M1**
and reconfirmed this on 2026-11-14. The RS485 head has been put away.
The runtime retains both angle/PWM and Pelco-D paths; the manual direction panel is Pelco-D-only.
Source/host checks are separate from deployment and physical acceptance.

```text
Camera -- dedicated Ethernet --> Jetson --> local 1024x600 kiosk
                                   |-- full-frame recognition and tracking
                                   |-- ten-minute MKV, no audio
                                   `-- angle planner / m0_pulse --> USB CH340G
                                                                  `-- M0/M1 --> PWM servos
```

The PWM writer uses 9600 8N1 ASCII pulse commands, with requested angles rather
than position feedback. General profiles still select disabled Pelco-D/Moxa;
the named M0 profile also retains old device and motion settings. Neither is a
verified current deployment profile. See [the current contract](servo-controller.md)
for configuration gaps, PWM STOP semantics and calibration boundaries.

## Historical RS485 path (stored head)

The following Pelco-D behavior describes retained source, not current PWM hardware.
Manual control in the main settings drawer uses 9600 8N1, address 001, separate head power and Moxa R+/R−
in RS-485 2-wire mode. It starts DISARMED, sends STOP on release, bounds one hold
to 2 seconds and expires missing control refreshes after 0.4 seconds. The head's
own watchdog and feedback are unverified. The 2026-11-06 test's 42 GET probes returned zero RX;
position, collision and limit data were unavailable. No camera was
attached during that head-only test. See [the historical contract](servo-controller.md#歷史-rs485-雲台已收起).

The standalone test server/assets are removed. The dashboard provides read-only
`GET /api/pan-tilt` and token-protected, same-origin JSON `POST /api/pan-tilt`.
AppController serializes these commands against tracking ARM/STOP. Connecting
manual control first closes the automatic service; failed closure blocks takeover.
Disconnecting closes manual control before restoring the configured automatic
service DISARMED. No two writers share a port. The runtime owns the 25 ms manual
watchdog and stops/closes manual control on shutdown. Manual connection requires
Pelco-D configuration but is available for calibration with automatic output disabled.
GET, page load and opening settings never create a manual connection or move the head.

### Tracking runtime and RS485 output

The main runtime begins DISARMED. ARM waits for a new frame and follow decision;
opening the exclusive serial port or arming sends no motion/home command. STOP,
camera loss, stale inference, write failure, disk stop threshold or normal process
shutdown disarms and sends a real Pelco STOP. Restart never re-arms it.

With `protocol="pelco_d"`, `TrackingCoordinator` selects `VelocityPlanner` for
both axes. The planner maps full-frame image error to signed integer speed levels
without assuming or integrating a shaft position. Each axis stops in its deadband;
missing target, trial, search and finished decisions stop instead of returning home.
Tilt is optional and requires its own calibrated direction. Disabling tilt clears
both cached axes and sends STOP before fresh pan-only control can resume.

At the historical profiles' configured 20 Hz, the coordinator produces the latest command and a dedicated
`PelcoServoClient` writer refreshes it. A 250 ms stale producer, reversed clock or
partial/failed write triggers a STOP attempt and latches the client until restart.
Image freshness remains a separate default 1.5 s deadline. STOP and writes share
one lock: an in-flight write finishes before STOP, and old run IDs cannot restart
motion after re-ARM. No callbacks acquire application locks under the writer lock.

Status exposes requested protocol speed levels, never measured motion. Angle
metrics stay null; diagnostics omit legacy home/range fields. Address, level caps,
formula and calibration are in [the historical contract](servo-controller.md#自動跟拍主程式).
General profiles select Pelco-D but remain disabled with uncalibrated directions.
The `m0_pulse` angle path is used by the current PWM servos; `shore_spotter`
remains a compatibility path.

These protections run on the host. The head's watchdog, receive path, position,
limits and collision data are unverified; disconnected/powerless hosts cannot
promise STOP delivery. No automatic homing or electronic travel limit is claimed.

## Shared tracking and recording

Stable chasing starts an unqualified trial with no pan target. Only a surfer
previously seen chasing can later win the first-takeoff/ride lock. People first
seen already taking off/surfing are not acquired. Takeoff/surfing qualification
is latched for clip retention, while both scores falling below threshold for
the configured grace period ends the ride (even if chasing becomes high).
The chosen tracker cannot be replaced mid-ride. After completion or prolonged
loss, the state returns to searching and can choose a new surfer.

Pan is proportional to horizontal full-frame error. PWM uses bounded requested
angle trajectories; Pelco-D uses protocol speed levels without angular bounds. Detection always consumes the original
wide frame. Digital kiosk zoom is slow-in/fast-out: subject speed, image-edge
proximity and inference cadence can all force scale 1.0, the widest FOV.

`TrackingCoordinator.on_decision` feeds `RecordingTriggers` after target selection.
A separate worker starts the event recorder during the chasing trial and
finishes on abandoned trial, ride completion, loss timeout, DISARM or stale
inference. An automatic clip that never qualified is deleted only after the
recorder has stopped; its discard metadata is retained and not uploaded. Continuous mode adds a trigger for the entire confirmed ARM session, independent of target decisions.
The UI replaces manual start/stop with a persisted continuous-mode switch (default off).
Changing it while DISARMED never starts recording; STOP/fault DISARM releases its request.
Continuous clips are retained without a qualifying surfer. Mode changes share the existing event clip,
so disabling continuous never cuts an active automatic event. The legacy manual API remains available
for compatibility, with the same independent trigger behavior. Finalizing an old clip does not discard
pending requests. Recording IO is not performed in the inference callback.

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

## PWM angle planner

Inference only publishes the latest decision. The retained angle control loop integrates
angular velocity with configured speed and acceleration limits (`ServoConfig`
defaults are 5 degrees/s and 10 degrees/s²; the old M0 TOML differs). These are host trajectory bounds,
not physical feedback. Gain uses a fixed 0.20 second reference; inference FPS
does not multiply controller strength. The writer retains only the freshest
target and never queues a history of positions. No sibling firmware or Station
watchdog is part of the direct M0 path.

A missing locked rider immediately holds the last requested angle. The single five-second `tracking.ride_exit_seconds` setting controls both ID retention and ride exit. ID age starts at its last matched detection; ride age starts at its last valid takeoff/surfing observation. Expiry is checked before accepting a late recovery. Ride exit owns release and recording finalization; trial and cooldown remain two seconds. An inference stall disarms through the existing freshness limit; STOP clears cached decisions before re-ARM. Search/finished decisions can return PWM pan toward configured `home_deg` with bounded acceleration and braking. Its default 90° is not a verified physical centre; re-ARM does not read shaft position. PWM STOP clears pending commands and stops sending, but cannot cancel the servo's last received position. RS485 reference-direction return remains unimplemented.

The supervisor also checks the enabled `m0_pulse` UART. A missing port at startup,
USB removal/replacement (including a new device at the same path), or a latched
write/producer fault causes DISARM and closes the old writer before a new client
is opened. The check uses the same controller lock as ARM/STOP and port handoff.
An unreleased writer blocks replacement. Reopening clears all old targets and
never re-arms, homes or restarts the camera. Disabled and Pelco-D paths are not
automatically reopened by this PWM recovery check. This describes current source,
not deployment or physical acceptance of recovery.

## Detection overlay

The status API publishes every current detection above the configured detector threshold, with normalized full-frame boxes and all action names above their individual tracking thresholds. This display is independent of target acquisition and ARM. Detections without a qualifying action retain a box and tracker ID without inventing a state. The SVG overlay accounts for image letterboxing and digital zoom; it expires with observation freshness. Preview delivery and raw recordings remain independent of these browser annotations.

## Native screen and LAN Web

The native Qt Widgets desktop reads one bounded 960×540 BGRx frame in private
RAM-backed `/dev/shm` storage. Each decoder tee branch has its own bounded queue;
recording and decoded inference retain their existing paths. Native rendering
uses CPU copies and Qt painting; this is not a zero-copy GPU display claim.
The Python backend owns state/control; closing the UI does not stop it.

`camera.native_display=true` enables the local frame branch. With
`web.allow_lan=true` and `web.host="0.0.0.0"`, private-network clients may access
the dashboard by IP. Host checks match the accepted socket's local IP, and POSTs
require the same origin. Native frame paths and desktop window actions remain
loopback-only. No router forwarding is configured.

`web.h264_preview=true` adds an on-demand, bounded appsrc → NVENC H.264 → MPEG-TS
branch from the same native frames. A loopback go2rtc relay supplies WebRTC via
the dashboard's SDP endpoint; only media uses port 18555. Closing the last viewer
releases the encoder after the relay disconnects. The browser falls back to JPEG
when WebRTC fails. See [streaming](streaming.md).
