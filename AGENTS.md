# Agent Instructions

Shore Jetson is a personal Python project for automatic surf tracking and filming
on Jetson with a Night Wolf YL-A85-800 / MCA85_AF camera and two 180° PWM servos
through CH340G USB-TTL → M0/M1 controller (owner reconfirmed 2026-11-14).
The RS485 pan-tilt head has been put away; its documentation is historical.
All user-facing names use **Shore Jetson**. The package, CLI, service identifiers
and installed paths retain `shore-tracker` for compatibility. Keep changes small
and practical; use Traditional Chinese when communicating with the owner.

## Working agreement

- Start with read-only inspection: confirm the checkout and `git status --short --branch`, then inspect the relevant code, tests and diff. Preserve existing edits and untracked files; never reset, clean or overwrite unrelated work.
- For an authorized development request, implement, run relevant host checks and review the result without asking permission for each local edit. A discussion-only request stays read-only.
- Use [the development guide](docs/development.md) for workflow and task examples. Read other documents only when relevant; a small fix needs no separate specification or plan file.
- Follow the existing Python and plain JavaScript structure. Add a dependency or abstraction only for a concrete need; do not introduce frameworks, CI, orchestration or infrastructure just to support AI development.
- This project is independent of Shore-Spotter. Do not require its checkout, version/hash review or firmware. Do not modify or flash it. Private upload is a separate optional integration described in `docs/project-setup.md`.
- Do not commit, push, deploy or initialize infrastructure unless requested. Existing authorization applies only to its stated task and target.

## Find the right context

| Work | Entry points |
| --- | --- |
| Product behavior / setup | `README.md`, `docs/project-setup.md`, `pyproject.toml` |
| Runtime / tracking / recording | `docs/architecture.md`, `shore_tracker/runtime.py`, `shore_tracker/coordinator.py`, `shore_tracker/inference/`, `shore_tracker/recording_triggers.py` |
| UI / status / preview | `docs/ui.md`, `shore_tracker/web/`, `shore_tracker/status.py`, `shore_tracker/display.py`, `deploy/preview-web.py` |
| PWM servos / hardware | `docs/servo-controller.md`, `docs/hardware.md`, `shore_tracker/servo/pulse_controller.py`, `shore_tracker/servo/simple.py`, `shore_tracker/config.py`; tracking: `shore_tracker/control/pan.py`, `shore_tracker/coordinator.py` |
| Historical RS485 head | Historical sections of `docs/servo-controller.md`, `docs/pelco-d-test.md`, `shore_tracker/servo/pelco_d.py`, `shore_tracker/servo/pelco_client.py`, `shore_tracker/control/velocity.py` |
| Camera / inference / deployment | `docs/camera-pipeline.md`, `docs/inference.md`, `docs/commissioning.md`; `docs/jetson-nano.md` for Nano |

Local `STATUS.md`, if present, contains dated operational evidence, not proof of
current device state. Prefer current source and verified device evidence; surface
conflicting documentation instead of silently choosing a hardware setting.

The current physical display is **1024x600 landscape** (owner confirmed
2026-10-31). Native UI work targets that viewport. Remote Web is responsive without
a fixed 1024x600 canvas, including older `?layout=nano` links (owner requested
2026-11-14); Web settings belong in the bottom bar with no reserved right rail.
The native App keeps close, minimise and settings together in the top-right
column; opening settings hides all three (owner reconfirmed 2026-11-14).
Follow `docs/ui.md` for geometry, acceptance and the selected
`frontend-design` skill reference. Preserve the existing plain HTML/CSS/JavaScript.

## Host verification

Use Python 3.11+ and Node.js; setup is in `docs/project-setup.md`. Commands run from
the repository root. Use the prepared virtual environment when available:

```sh
python3 -m pytest -q
node tests/test_dashboard_controls.js
node tests/test_kiosk_controls.js
node tests/test_control_page.js
node tests/test_pelco_controls.js
node tests/test_servo_test_controls.js
git diff --check
```

Run focused tests while iterating, then the relevant broader suite for behavior
changes. Add regression coverage for changed behavior. Documentation-only changes
need link/command/diff checks, not new tests. Report failures and skips honestly.
For UI work use `python3 deploy/preview-web.py` (loopback port 18092); `serve` starts
the real runtime and is not an offline preview. Host checks do not prove GPU,
camera, recording or physical motion acceptance.

## Device and data boundaries

- For camera discovery, direct-Jetson networking, ONVIF, RTSP or PTZ work, read `.agents/skills/yl-a85-camera/SKILL.md` completely before acting. This local skill is not shipped in Git; if missing, report that prerequisite before this work. Unrelated host development can continue.
- Obtain explicit approval before network changes, physical camera/servo movement, lights/audio, firmware/reset, Internet exposure or persistent installation. A historical deployment note is not new authorization.
- The dedicated camera interface is `enP8p1s0` on Orin and `eth0` on Nano. Verify the target and preserve Wi-Fi SSH and unrelated NetworkManager profiles.
- Current hardware is **CH340G USB-TTL → M0/M1 dual-channel controller → two 180° PWM servos**. The retained `m0_pulse` path uses 9600 8N1 and ASCII `#00Pxxxx!` / `#01Pxxxx!` without a newline. Jetson sends UART; the controller generates PWM. See `docs/servo-controller.md` and `docs/hardware.md`.
- The owner specified **horizontal M0 (`pan_channel=0`), vertical M1 (`tilt_channel=1`)** on 2026-11-14. This supersedes the 2026-10-31 swapped-channel record. The owner then requested fourfold test amplitudes: both 90-degree commands, left 20, right 40, up 40, down 40. Keep the 5°/s speed and 10°/s² acceleration bounds; the test takes about 47 seconds with a 60-second cap and unchanged 1.5-second heartbeat lease. Opening the page never starts it; keep exclusive ownership, cancel/lease guards and DISARMED restoration. Physical directions, centering and motion remain unverified until observed.
- Reconfirm current wiring, adapter identity, axis/channel mapping, direction, rated power, neutral and mechanical limits before enabling output. Code mappings (0–180°, 500–2500 µs, 90°/1500 µs), default channels and the dated tests in `docs/legacy-m0-controller.md` are not acceptance of this reassembled setup. Do not import Youmi 359° settings or RS485 supply/wiring.
- General TOML examples still select disabled `pelco_d`/Moxa; `config/jetson-m0-uart.toml` retains an old enabled M0 profile with `/dev/ttyTHS1` and unverified motion parameters. Neither is a verified current CH340G deployment profile. Check the actual target configuration separately; documentation changes do not authorize configuration changes or deployment.
- Preserve DISARMED startup, STOP and freshness guards. The PWM writer opens/arms without sending a cached or home target, but fresh tracking decisions can request the configured home from an unknown physical position. PWM STOP clears pending commands and stops sending; it does not cancel a position already received or remove power. UART writes and requested angles are not position feedback; ACK and an actuator-side watchdog are unverified.
- RS485/Pelco-D and its main-settings manual controls remain in source for compatibility. The 2026-11-06 head-only motion/42 zero-RX GET probes and Moxa R+/R−/12 V instructions apply only to that stored head, not current PWM hardware. Do not use the Pelco direction panel for PWM or reuse its STOP semantics. Restore RS485 operation only under a separate explicit task with hardware/configuration revalidation.
- This private local project may retain installed-camera identity and network details locally. Do not publish or push them without explicit approval. Keep credentials, tokens, authenticated/full stream URLs, captures and footage out of Git and reports.

## Completion

Review the task's diff, update affected behavior/setup documentation, and report
what changed, checks actually run, and remaining limitations. Separate host test
results, deployed software, live-device observations and physical acceptance.
Update local `STATUS.md` only when a handoff or device state materially changes;
do not create a log entry or another process document for every small edit.
