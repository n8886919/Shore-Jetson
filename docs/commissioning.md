# Direct M0 UART commissioning

1. Review the sibling source with `python3 -m tools.check_shore_spotter`.
2. Confirm GXServo 42 kg / 180 degrees and controller M0. Power off equipment
   before moving wires to Jetson J12 pin 8 TX -> controller RX and pin 6 GND.
   Controller TX and positive supply do not connect to Jetson.
3. Apply the prepared update while tracking is DISARMED and event recording idle.
   Preserve field gain, deadband, direction, zoom and camera orientation. The new
   service must remain DISARMED; opening UART alone must not send any command.
4. Confirm serial service readiness, camera link/advancing frames and inference.
   A ttyTHS1 device or successful write alone does not verify the physical signal.
5. With explicit movement authorization and a person at the servo power cut-off,
   verify 1500 us centre, direction and bounded motion around 90 degrees. Do not
   run the smoke tool alongside the service owning the UART. The tool requires
   `--execute --confirm-centered` and never runs as part of deployment.
6. Verify installed mechanical limits before full-range tracking. Test STOP,
   stale target, disconnect/restart and manual re-ARM; STOP is not power-off and
   the controller may complete its last requested position.
7. Restore camera Ethernet, check image orientation, then validate a full surf
   sequence: chase -> lock -> follow -> finish -> completed MKV -> private Drive.

For a separately authorized bounded motion test, after the service has stopped:
`python3 -m tools.servo_smoke_test --config <local.toml> --execute --confirm-centered`.
It assumes an independently verified 90-degree centre, then requests a slow
90 -> 95 -> 85 -> 90 sequence. There is no ACK or position sensor.

No network, boot firmware, GPIO pinmux, lights or audio changes are included in
the UART application update. If the pin cannot transmit, diagnose it before
considering any device-tree or firmware changes.
