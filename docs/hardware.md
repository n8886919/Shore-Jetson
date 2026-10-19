# GXServo 42 kg / 180 degrees with direct UART

This project retains the GXServo 42 kg, 180-degree servo, centre 90 degrees.
Do not copy the sibling Shore-Spotter project's Youmi 359-degree profile.
The separate two-channel controller uses M0, 9600 8N1 and ASCII pulse commands.

## Wiring selected on 2026-10-17

Power equipment off before moving wires. Physical header numbers:

| Jetson Orin Nano 40-pin J12 | Controller |
| --- | --- |
| Pin 8, UART1 TX, 3.3 V | RX |
| Pin 6, GND | GND |
| Pin 10, RX | Leave disconnected; controller TX voltage/feedback is not established |
| 3.3 V / 5 V power pins | Do not connect |

The controller and servo retain their separate correctly rated supply. Do not
connect two transmitters (Jetson and LilyGO) to the controller RX simultaneously.
Servo is connected to M0; leave the controller in its documented manual-light
state and do not turn its potentiometer while sending UART commands.

The installed system maps UART1 at 0x3100000 to `/dev/ttyTHS1`; its device-tree
node reports `okay`. This is configuration evidence, not proof of pin output.
The app service already belongs to `dialout`. No UART command has been sent
as part of the read-only discovery.

The rear J14 header is the combined debug UART. The current kernel console and
serial login both use ttyTCU0. This app rejects that device, its symlink aliases
and active kernel consoles. Do not repurpose it by merely changing baud rate.

## Control boundary

`m0_pulse` sends `#00P0500!` through `#00P2500!`, no newline, at 9600 8N1.
The 180-degree command mapping uses 90 degrees = `#00P1500!`. This is a
configured pulse mapping, not an angle measurement. Verify the GXServo's
centre, direction and mechanical endpoints on the installed assembly before
full-range operation; the controller generates PWM, not the Jetson UART.

There is no ACK, physical angle feedback or established controller command
watchdog. STOP prevents new commands; the servo can still complete the last
requested position and remains powered. The 250 ms target deadline is host
software protection only. A frozen host cannot enforce an actuator-side stop.
Opening the port, starting the service and re-ARM do not transmit a cached or
home target. ARM with live tracking can request 90 degrees from an unknown
physical position; start only after the installed centre has been confirmed.

References:
- [NVIDIA UART1 / pins 8 and 10](https://forums.developer.nvidia.com/t/not-able-to-access-find-dev-ttyths0-on-jeton-orin-nano-devkit-nvidia-official-board/299789)
- [NVIDIA combined debug UART](https://docs.nvidia.com/jetson/archives/r39.2/DeveloperGuide/AT/JetsonLinuxDevelopmentTools/TegraCombinedUART.html)
