# Shore Jetson

Jetson proof of concept for automatic surf tracking and filming with an
ONVIF/RTSP camera mounted on a one-axis GXServo pan mechanism.

The Python package, CLI and installed service retain the `shore-tracker` name.

The primary goal is automatic surf filming. Manual recording is an additional trigger.
The current policy is defined by the user's 2026-09-05 clarification:

1. Boot DISARMED with the kiosk/private web UI (currently previewed at 1280x800). Click 開始追蹤 to enable the flow.
2. First observe stable **chasing**: start a trial clip, keep pan at 90 degrees,
   and remember the surfers seen chasing. Do not acquire people who appear only
   already taking off or surfing.
3. Lock the first qualifying takeoff from that chasing pool and begin following
   that tracker. A directly observed chase-to-surfing transition is also valid.
4. If chasing ends without a qualifying takeoff/ride, stop the trial, return to
   90 degrees and delete that new automatic video. It is never uploaded.
5. Once qualified, follow through takeoff/surfing. When both end or the target is
   lost beyond the grace period, finalize and upload the clip privately through
   the existing `surf-track` receiver. Returning to chasing also ends the ride.
6. The manual start/end button is additional: it can start a recording without
   ARM or preserve/extend a trial. Overlapping triggers share one clip; a clip
   with any manual trigger is kept for labeling even if the automatic trial fails.

One setting, `tracking.ride_exit_seconds = 5.0`, controls both missing-ID
retention and qualified ride exit. An ID expires five seconds after its last
matched detection. A qualified ride ends five seconds after its last valid
takeoff/surfing observation; missing and low-score frames share that deadline.
Recovery of the same rider before the deadline continues the same clip. While
waiting, a visible rider is followed even with low action scores; a missing rider
holds the last commanded angle. Matching remains IoU-based, so ID retention does
not guarantee recovery after large displacement or occlusion. Trial abandonment
and post-ride cooldown still use two seconds. The old `lost_grace_seconds` TOML
key is accepted only for compatibility and ignored.

After the qualified ride grace expires, pan returns to 90 degrees. Background ten-minute MKV
segments remain a separate local backup, not the successful event clips above.
Only the video belonging to a newly failed automatic trial is deleted; existing
recordings and Drive files are untouched. Small discard metadata is retained so
the UI can report that the trial was discarded.

Pan uses bounded proportional control. Inference always sees the original wide
frame. Digital preview zoom can be enabled through Max zoom. The current installation
uses a persistent maximum of 1x so the preview does not automatically zoom.

The installed direction is fixed: 0 degrees faces right, 90 degrees is centre.
For a target on the image's right, the controller decreases the servo angle.

## Development

Before every change, check the sibling Shore-Spotter source version and interface
contents, including uncommitted edits. The reviewed source boundary is documented separately; see
[controller compatibility](docs/shore-spotter.md) for update/review and board-version checks.

```bash
python3 -m tools.check_shore_spotter
python3 -m pytest -q
node tests/test_dashboard_controls.js
node tests/test_kiosk_controls.js
node tests/test_control_page.js
python3 -m shore_tracker --config config/example.toml doctor
python3 -m shore_tracker --config config/example.toml serve
```

The dashboard is loopback-only at `http://127.0.0.1:8090`.

- `/` and `/kiosk`: 1280x800 layout, normally a complete 1280x720 image and
  an 80px single-row footer. The complete 1280x800 canvas scales uniformly to a
  smaller browser viewport and remains anchored at the top left; opening controls
  never changes the footer geometry or text. 控制 opens inline: video becomes 960x540, the right
  320x720 holds lens/tuning/inference-rate/manual-record controls, and the lower
  960x180 holds metrics, fault reasons and expandable diagnostics. Closing
  restores the full image; opening controls never sends a hardware command.
- Web/mobile preview connections default to a 5 FPS delivery cap. Each client
  samples the latest JPEG independently, without slowing camera production or
  inference. `/kiosk?display=local` requests camera-rate delivery for the physical
  Jetson display; `deploy/start-kiosk` selects this mode automatically.
- `/control` and `/advanced` redirect to `/kiosk?controls=1`; there is no separate
  kiosk control page. `/mobile` retains its existing layout and links into the
  shared inline control surface.
- Main statuses use text and coloured backgrounds without dots. The UART state
  lives in diagnostics: an open host serial port does not prove that the M0 board
  received commands or that the Servo reached a requested position.

Inference defaults to 2 Hz (one model pass per 0.5 seconds). The control slider
accepts 1–10 Hz and changes the running worker immediately; restarting uses the
configured `inference.max_fps` (default 2). Preview remains independent at 20 FPS.
The worker waits before taking the newest frame, and never queues skipped frames.

Tracking changes from 開始追蹤 to 取消啟動 or 停止追蹤 according to reported state.
Status loss permits STOP but never a new ARM request. Lens adjustments require
tracking to be stopped. Servo angle is a requested value, not position feedback.

The control page retains read-only `/api/diagnostics` and queued camera recovery.
Manual reconnection is rejected while tracking or event recording is active,
queued or finalizing. Recovery remains DISARMED. Incomplete fragments are
retained without claiming successful finalization. Unknown resource ownership
blocks replacement. No camera passwords or stream URLs are exposed.

## Current direct UART control (2026-10-17)

The servo is **GXServo 42 kg / 180 degrees, centre 90**, not the sibling
project's Youmi 359. Use `config/jetson-m0-uart.toml` for a new direct deployment:
Jetson 40-pin UART -> M0 controller at 9600 8N1, `#00Pxxxx!` without newline.
See [wiring and limits](docs/hardware.md) and [commissioning](docs/commissioning.md).

Startup remains DISARMED and sends no home command. ARM/STOP only gate host
sending. STOP does not remove power or guarantee immediate physical stop.
The controller provides no known ACK/position feedback or command watchdog;
the 250 ms freshness guard runs on the Jetson. Rear debug port ttyTCU0 and
active kernel console ports are rejected before opening.

Existing configs without a protocol retain legacy 115200 `SET` behavior; they
are not compatible with current Shore-Spotter Station firmware. Migration is
explicit and preserves field tuning, camera settings and recordings. The direct
path shares M0 frame syntax with upstream, not its servo model or angle scale.

Completed clips are queued for the computer receiver and private Drive upload.
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

Without a Jetson connection, run `python3 deploy/preview-web.py --open` and open
`http://127.0.0.1:18092`. This uses the current dashboard assets, labels all values
as offline defaults, and rejects control commands. It does not open SSH, access
camera/servo hardware, record footage, or start the Drive receiver. Stop the
foreground preview with Ctrl+C.

The kiosk preview uses the same 1280x720 image area and 80px bottom bar. Lens,
tuning and recording controls open inline via 控制. Offline preview rejects
all hardware commands and does not invent live metrics.
