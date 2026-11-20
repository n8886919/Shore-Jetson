# Camera and recording pipeline

The supplied [IP Camera manual](assets/manuals/camera-and-rs485-pan-tilt-20261106.pdf#page=1)
occupies PDF pages 1–5; see the [manual index](hardware-manuals.md) for provenance
and model applicability.

Current actuators are **two 180° PWM servos through CH340G USB-TTL → M0/M1**
(owner reconfirmed 2026-11-14); the RS485 head is stored.
See [the control contract](servo-controller.md). No camera was attached during
the 2026-11-06 head-only tests. Camera ONVIF/RTSP and optical-lens capabilities
are independent of head motion and have not been established by those tests.

The authenticated RTSP URI is read from the environment and assigned directly
to `rtspsrc`; it is never placed in TOML, command arguments or UI status.

```text
rtspsrc (TCP video only)
  -> H.264/H.265 depay + parse
  -> Jetson decode -> bounded leaky JPEG preview -> UI and inference
```

`camera.rotation_degrees` accepts `0` (default) or `180`. Rotation is applied
by the Jetson converter before JPEG delivery, so kiosk preview and inference
use the same orientation. The encoded recording branch retains the camera
source orientation.

Audio is not linked. The UI/inference branch is bounded and leaky. The runtime
always builds this preview pipeline without a recording sink, including after
errors and reconnects. Opening the UI or connecting the camera creates no
background recordings. Existing segment files remain available in the file browser.

Automatic following and the additional manual trigger share a separate encoded
RTSP-to-MKV event recorder. It opens only while requested and waits for EOS before
publishing the completed clip and SHA-256. This does not re-encode the video or
restart the preview pipeline. On finalization the Nano notifies the backup worker.
A completed chase-only trial is archived under the capture date’s `fail/` folder.
Confirmed rides and manual or continuous clips use the date folder. Partial files
remain on the card for recovery.

Camera start is retried. Terminal pipeline state or a preview stall disarms the
tracker, stops that pipeline and builds a new one. The old preview callback
cannot replace the active preview. Disk pressure pauses recording rather than
deleting footage.

The low-level pipeline builder retains its segmented-sink compatibility code, but
the application runtime never selects it. Authenticated decode, reconnect behaviour
and event recording still require a Jetson field canary.

If format negotiation fails, compare the codec advertised by the camera profile
with the one in the stream's SDP and the caps negotiated by the decoder.
Check that decoded frames keep advancing. If preview and event recording each
work alone, test them together with the same input and decode a finalized clip.
For a camera overlay change, read the setting back and check its effect on a fresh frame.

## Independent preview and inference rates

`camera.preview_fps` controls the JPEG preview cap independently (default 20, configurable 1–60). `videorate` only drops excess frames; it cannot manufacture more camera frames. Encoded MKV recording retains the source stream and does not use the preview cap.

The camera publishes only the newest JPEG into a bounded store. The MJPEG client and the inference worker consume it independently; a slow inference call does not hold the producer lock or queue old frames. Increasing preview FPS may increase JPEG CPU cost and network usage, so 20 FPS is a configured cap pending Jetson measurement.

The UI shows preview production FPS and completed inference FPS separately. Preview FPS is measured over recent published JPEGs, resets on camera-session changes, and becomes unavailable when frames are stale. It does not measure browser receive/render FPS or the raw RTSP frame rate.

To measure camera-to-screen delay, place a running timer in the camera's field
of view. Photograph the timer and its displayed preview together and compare
the readings. Repeat across several frames to capture variation.
