# Historical controller reference

As of 2026-10-30 this project is independent of Shore-Spotter. No sibling
checkout, version check, interface hash or firmware is required. Controller
information was copied once into local documents and reference images;
future changes are maintained and tested here.

The owner reconfirmed on 2026-11-14 that the RS485 head is stored and current
hardware is **CH340G USB-TTL → M0/M1 → two 180° PWM servos**, using the retained
`m0_pulse` path at 9600 8N1. See [the current contract](servo-controller.md) for
configuration gaps and unverified wiring/channel/direction/limits.
[Dated M0 records](legacy-m0-controller.md) do not establish current acceptance.
Youmi's 359-degree scale, supply and other-project tests do not apply.
The `shore_spotter` protocol name remains solely for legacy 115200 SET
configurations with an explicitly selected legacy protocol or stored 115200 baud,
without a project dependency. General deployment examples still select disabled
Pelco-D/Moxa and are not current PWM profiles; the named M0 profile also contains
historical device/motion settings. The main-settings direction panel remains
Pelco-D-only; its [RS485 guide](pelco-d-test.md) is historical. Preserve DISARMED
startup. No sibling firmware or project is involved.
