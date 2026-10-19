# Shore-Spotter source boundary

The current sibling source is 0.6-dev. Its normal Station build has removed
115200 SET/SET2 external input; it only selects manual/GPS and drives its own
Youmi 359-degree servo controller. This app's new `m0_pulse` mode bypasses
Station and talks directly to the separate controller on M0 at 9600 8N1.

**This project's servo remains GXServo 42 kg / 180 degrees, centre 90 degrees.**
Only the M0 frame syntax and baud are shared. The sibling's 359-degree range,
179.5-degree centre, wiring, stored speed and physical acceptance do not apply.
The host planner retains its 20 Hz, bounded speed/acceleration and 250 ms fresh
target requirement. No actuator-side lease, ACK or position feedback is claimed.

`protocol="shore_spotter"` remains the legacy 115200 SET encoder so existing
configs never silently switch wire formats. It is not compatible with the
current normal 0.6-dev Station. New direct deployments must explicitly use
`protocol="m0_pulse"`, `serial_baud=9600` and a verified non-console device.

Before changes run `python3 -m tools.check_shore_spotter`. The checker now accepts
prerelease versions and tracks the actual M0 controller source. Review version,
Git state and interface hashes even when HEAD/version have not changed. Record
with `--record-reviewed` only after adaptation and software validation.
The baseline is a source review, not installed firmware or physical proof.

The upstream C++ comparison runs its actual controller output at matching pulse
positions and compares complete frames with this project's encoder. It explicitly
uses different angle scales for the two servo models. Local tests separately
reject values above 180 degrees, preserve DISARMED/no-write startup, enforce
fresh targets and reject combined/debug console ports.
