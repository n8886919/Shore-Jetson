# Shore Jetson

Jetson proof of concept for automatic surf tracking and filming with an
ONVIF/RTSP camera and **two 180° PWM servos**.

All application titles, desktop launchers and service descriptions use **Shore Jetson**.
The Python package `shore_tracker`, CLI, service identifiers and installed paths retain
`shore-tracker` for compatibility with existing settings and recordings.

The owner reconfirmed on **2026-11-14** that the RS485 head has been put away.
Current hardware is **CH340G USB-TTL → M0/M1 dual-channel controller → two 180° PWM servos**.
The retained `m0_pulse` path sends ASCII pulse commands at 9600 8N1.
Current channel mapping, direction, neutral, mechanical limits and installed
configuration still require physical verification; dated tests do not establish acceptance
of the reassembled setup. General TOML examples still select disabled Pelco-D/Moxa,
and the named M0 profile also contains old, unverified settings. This documentation
does not make either ready for deployment. The source defaults now use horizontal
M0 and vertical M1; the installed device settings still need a separate update.
See the [current control contract](docs/servo-controller.md)
and [hardware](docs/hardware.md).

The owner now specifies horizontal **M0** and vertical **M1**. The settings drawer's
**雲台測試** button requests both axes at 90°, then relative left 5°, right 10°,
up 10° and down 10°, with live step text and a stop button. It ends at centre-right
5° and vertical centre. Test and tracking writers are exclusive; dismissal,
connection loss and a heartbeat timeout cancel the test. Completion is a command
result, not measured position. See [the PWM test contract](docs/servo-controller.md#pwm-雲台測試按鈕).

The native desktop keeps close, minimise and settings buttons together in the
top-right column. Web settings stay in the bottom status bar. Remote Web fills
the browser viewport and preserves the video's aspect
ratio, including older `?layout=nano` links; it has no fixed 1024×600 limit.

The primary goal is automatic surf filming. Manual recording is an additional trigger.
The current policy is defined by the user's 2026-09-05 clarification:

1. Boot DISARMED with the kiosk/private web UI on the current 1024x600 landscape display (`?layout=nano`). Click 開始追蹤 to enable the flow.
2. First observe stable **chasing**: start a trial clip, hold the current filming direction,
   and remember the surfers seen chasing. Do not acquire people who appear only
   already taking off or surfing.
3. Lock the first qualifying takeoff from that chasing pool and begin following
   that tracker. A directly observed chase-to-surfing transition is also valid.
4. If chasing ends without a qualifying takeoff/ride, stop the trial, request the
   filming reference direction and retain the completed trial for review.
5. Once qualified, follow through takeoff/surfing. When both end or the target is
   lost beyond the grace period, finalize and queue the clip for the Nano Drive backup. Returning to chasing also ends the ride.
6. The manual start/end button is additional: it can start a recording without
   ARM or preserve/extend a trial. Overlapping triggers share one clip; a clip
   with any manual trigger is kept for labeling even if the automatic trial fails.

One setting, `tracking.ride_exit_seconds = 5.0`, controls both missing-ID
retention and qualified ride exit. An ID expires five seconds after its last
matched detection. A qualified ride ends five seconds after its last valid
takeoff/surfing observation; missing and low-score frames share that deadline.
Recovery of the same rider before the deadline continues the same clip. While
waiting, a visible rider is followed even with low action scores; a missing rider
holds its last requested PWM angle (the historical Pelco-D path sends STOP). Matching remains IoU-based, so ID retention does
not guarantee recovery after large displacement or occlusion. Trial abandonment
and post-ride cooldown still use two seconds. The old `lost_grace_seconds` TOML
key is accepted only for compatibility and ignored.

After the qualified ride grace expires, the intended policy returns to the filming reference direction.
The PWM angle planner can request the configured `home_deg`, but its default 90°
is not a measured position or a verified filming reference for the current assembly.
Connecting the camera, opening the UI or restarting the service does not record video.
Local recording starts only through the manual, automatic-following or ARM-session
continuous trigger, whose state is exposed in the UI.
A finalized chase-only trial is archived under the capture date’s `fail/` folder. Confirmed rides, manual clips and continuous recordings go directly under the date. The independent backup worker releases local video only after verifying the cloud MP4 and its midpoint JPEG.

PWM pan uses image-error proportional control with bounded requested angles, speed
and acceleration. Inference always sees the original wide
frame. Digital preview zoom can be enabled through Max zoom. The current installation
uses a persistent maximum of 1x so the preview does not automatically zoom.

PWM axis-to-image direction, reference position and mechanical limits require
calibration on the current assembly. Old channel/direction and 90° centre records
are historical evidence, not current measurements.

## Development

Start with [AI 開發與日常流程](docs/development.md) for the lightweight workflow,
task examples and code/test map. [AGENTS.md](AGENTS.md) contains the shared agent
instructions; [host setup](docs/project-setup.md) covers environment preparation.

This project is maintained independently. Development and deployment require no
sibling repository or external controller firmware. Controller specifications
and vendor references live in [the local contract](docs/servo-controller.md).
The supplied [camera and historical RS-485 head manuals](docs/hardware-manuals.md)
are archived with a page index; PWM controller images remain in the
[dated M0 reference](docs/legacy-m0-controller.md).

The gear drawer's direction/speed manual panel is Pelco-D-only and cannot control
the current PWM setup. Its [RS485 operation guide](docs/pelco-d-test.md) is historical.
The standalone test page/server has been removed. Preview the integrated panel with
`python3 deploy/preview-web.py`; offline preview never opens hardware.

```bash
python3 -m pytest -q
node tests/test_dashboard_controls.js
node tests/test_kiosk_controls.js
node tests/test_control_page.js
node tests/test_pelco_controls.js
python3 -m shore_tracker --config config/example.toml doctor
python3 deploy/preview-web.py
```

The offline preview is loopback-only at `http://127.0.0.1:18092`. `doctor` checks
static prerequisites and can report missing Jetson hardware/models on a host.
The real runtime's `serve` command starts camera/recording components; use it only
within an authorized device task following [commissioning](docs/commissioning.md).
The desktop now uses the native C++/Qt App; its footer shows the current LAN
`IP` without a scheme or `:80`; other ports display as `IP:port`. The Nano profile opts into private-LAN Web access,
with hardware H.265/WebRTC and JPEG fallback. Generic profiles remain loopback-only.
See [native desktop and streaming](docs/streaming.md) for setup and measured scope.

- `/` and `/kiosk`: the current physical display and primary UI target are
  **1024x600 landscape**, selected with `?layout=nano`. Without that parameter,
  the 1280x800 compatibility layout remains. See [UI specifications](docs/ui.md).
  In Nano layout, opening controls scales the complete 960x540 video to 768x432
  and reveals a 256px menu. The black right area and 50px gear move together as
  part of that menu in 0.1 seconds. The 768px footer stays fixed and keeps Start/
  Stop reachable. Top-right 50px buttons resize the native window to half size
  to reveal the Jetson desktop, restore fullscreen, or close the browser after
  confirmation. The dock has a Shore Jetson launcher. These display actions send no hardware commands.
  FPS rounds to an integer and turns red at zero.
- Web/mobile preview connections default to a 5 FPS delivery cap. Each client
  samples the latest JPEG independently, without slowing camera production or
  inference. `/kiosk?display=local` requests camera-rate delivery for the physical
  Jetson display; `deploy/start-kiosk` selects this mode automatically.
- `/control` and `/advanced` redirect to `/kiosk?controls=1`; there is no separate
  kiosk control page. `/mobile` retains its existing layout and links into the
  shared control drawer.
- Main statuses use text and coloured backgrounds without dots. Serial state
  lives in diagnostics: an open port does not prove that the PWM controller received
  commands or that a servo reached its requested angle. Tracker angle values are
  requests or planner estimates, not position feedback.

Inference runs as fast as the available frames and hardware allow; the kiosk has
no rate slider. `inference.max_fps = 0` disables intentional pacing (the default).
An optional 1–10 Hz cap remains available for diagnostic clients. Each pass takes
the newest frame and skipped frames never form a queue. The live image is never
held back for inference; the latest valid boxes are drawn on the newest image.

During tracking, the AI switch can be turned off for keyboard pan/tilt control:
hold an arrow key to keep moving, release to stop new targets, and turn AI back on
to resume automatic following. This uses the configured PWM axis limits and a
0.5-second input lease; recording does not lock the AI switch. See the
[control contract](docs/servo-controller.md#pwm-方向鍵手動控制).

Tracking changes from 開始追蹤 to 取消啟動 or 停止追蹤 according to reported state.
Status loss permits STOP but never a new ARM request. Lens adjustments require
tracking to be stopped. PWM angle values are requested values, not measured position.

The control page retains read-only `/api/diagnostics` and queued camera recovery.
The backup button is labelled **重新連接相機**. Its progress follows live recovery
state and disappears when video is ready; no page refresh is needed. Servo faults
are labelled **舵機未連線**. With the owner's current dual 180° PWM servos
(`m0_pulse`, CH340G USB-TTL → M0/M1), the supervisor automatically retries a
missing or failed UART after closing its previous owner. Recovery stays DISARMED,
discards old targets and requires a fresh manual ARM; it does not restart video.
Manual reconnection is rejected while tracking or event recording is active,
queued or finalizing. Recovery remains DISARMED. Incomplete fragments are
retained without claiming successful finalization. Unknown resource ownership
blocks replacement. No camera passwords or stream URLs are exposed.

## PWM servo control

Jetson USB → CH340G USB-TTL → M0/M1 controller → two 180° PWM servos.
The controller generates PWM from `#00Pxxxx!` / `#01Pxxxx!` ASCII commands,
without a newline. The source maps 0–180° to 500–2500 µs; those are encoding
bounds, not verified mechanical endpoints. Power must match the installed servos;
the historical RS485 head's separate 12 V supply is not a PWM power specification.

The writer starts DISARMED. Opening the port or ARM alone sends no cached/home
target, but fresh tracking decisions can request the configured home from an
unknown physical position. STOP clears pending commands and stops transmission;
it cannot cancel a position already received or remove servo power. A 250 ms
target deadline and separate image-freshness checks protect the host path;
ACK, actual position feedback and a controller-side watchdog are unverified.
See [configuration and stop semantics](docs/servo-controller.md#pwm-自動跟拍與停止語意)
and [commissioning](docs/commissioning.md).

`config/jetson-m0-uart.toml` documents the retained protocol but still contains
an old enabled `/dev/ttyTHS1` profile and motion settings. General examples still
select disabled Pelco-D/Moxa. Neither is a verified current CH340G deployment
profile; review the actual target configuration before any separately authorized
deployment. Old M0 motion/deployment tools also require review of their assumptions.

RS485/Pelco-D code remains for compatibility with the stored head. Its 2026-11-06
head-only motion and 42 zero-RX probes apply to that head, not these PWM servos.
Historical wiring, manual controls and capabilities are retained in the
[RS485 reference](docs/pelco-d-test.md) and [control contract](docs/servo-controller.md#歷史-rs485-雲台已收起).

Eligible completed clips are queued for the event-driven Nano Drive backup.
Offline clips remain on Jetson. Preview zoom is digital; optical zoom is separate.
The native JetPack/TensorRT runtime uses exported ONNX detector/action models.
Software tests do not establish physical motion or recording acceptance.

Credentials belong only in the Jetson's root-readable
`/etc/shore-tracker/secrets.env`; never put an authenticated RTSP URL in TOML,
Git, commands or reports.

See [architecture](docs/architecture.md), [hardware](docs/hardware.md), and the
ordered [commissioning guide](docs/commissioning.md).

See [recording workflow](deploy/manual-recording.md) and
[public source setup](docs/project-setup.md). Device inventory and deployment
observations are maintained locally and are excluded from this repository.

## Offline dashboard preview

Without a Jetson connection, run `python3 deploy/preview-web.py` and open
`http://127.0.0.1:18092/kiosk?layout=nano` in a 1024x600 viewport. This uses the current dashboard assets, labels all values
as offline defaults, and rejects control commands. It does not open SSH, access
camera/servo hardware, record footage, or start the Drive receiver. Stop the
foreground preview with Ctrl+C.

The kiosk preview uses the same 960x540 image area. Lens,
tuning and recording controls open via the gear button. Offline preview rejects
all hardware commands and does not invent live metrics.
