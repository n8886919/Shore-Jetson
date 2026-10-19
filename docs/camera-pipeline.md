# Camera and recording pipeline

The authenticated RTSP URI is read from the environment and assigned directly
to `rtspsrc`; it is never placed in TOML, command arguments or UI status.

```text
rtspsrc (TCP video only)
  -> H.264/H.265 depay + parse -> tee
       |-> splitmuxsink -> Matroska segments, requested every 600 s
       `-> Jetson decode -> bounded leaky JPEG preview -> UI and inference
```

`camera.rotation_degrees` accepts `0` (default) or `180`. Rotation is applied
by the Jetson converter before JPEG delivery, so kiosk preview and inference
use the same orientation. The encoded recording branch retains the camera
source orientation.

Audio is not linked. The UI/inference branch is bounded and leaky so a slow
consumer cannot stall recording. File names include local date, process-session
ID and a segment counter. These background segments remain on disk without upload or automatic deletion.

Automatic following and the additional manual trigger share a separate encoded
RTSP-to-MKV event recorder. It opens only while requested and waits for EOS before
publishing the completed clip and SHA-256. This does not re-encode the video or
restart the preview pipeline. The completion API feeds the private Drive receiver;
Failed/partial manual or qualified event files are preserved but never presented
as complete. A new automatic trial that never qualified and had no manual trigger
is discarded, including its partial video; existing recordings are untouched.

Camera start is retried. Terminal pipeline state or a preview stall disarms the
tracker, stops that pipeline and builds a new one. The old preview callback
cannot replace the active preview. Disk pressure pauses recording rather than
deleting footage.

`splitmuxsink` splits on keyframes, so the actual file duration can exceed 600
seconds by the source GOP interval. Authenticated decode, reconnect behaviour
and long-running segment cuts still require a Jetson field canary.

## Independent preview and inference rates

`camera.preview_fps` controls the JPEG preview cap independently (default 20, configurable 1–60). `videorate` only drops excess frames; it cannot manufacture more camera frames. Encoded MKV recording retains the source stream and does not use the preview cap.

The camera publishes only the newest JPEG into a bounded store. The MJPEG client and the inference worker consume it independently; a slow inference call does not hold the producer lock or queue old frames. Increasing preview FPS may increase JPEG CPU cost and network usage, so 20 FPS is a configured cap pending Jetson measurement.

The UI shows preview production FPS and completed inference FPS separately. Preview FPS is measured over recent published JPEGs, resets on camera-session changes, and becomes unavailable when frames are stale. It does not measure browser receive/render FPS or the raw RTSP frame rate.
