# Public source setup

Shore Jetson contains the application, host tests and deployment tooling. All
user-facing application titles, launchers and service descriptions use Shore Jetson.
The Python distribution, CLI, service account and installed paths retain the
`shore-tracker` name for compatibility. The `shore_tracker` import name and
`ShoreTracker` window class are internal identifiers. This repository does not contain installed-device inventory,
private deployment history, credentials, models, captures or recordings.

## Host development

See [AI 開發與日常流程](development.md) for task scope, verification and handoff.
Use Python 3.11 or newer and Node.js for the browser-control tests (Node 22 was
used when checking this workflow). Run the following from the repository root
when preparing a local development environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
node tests/test_dashboard_controls.js
node tests/test_kiosk_controls.js
node tests/test_control_page.js
node tests/test_pelco_controls.js
```

For later sessions, `source .venv/bin/activate` makes the `python3` commands in
the README and development guide use this environment. Do not replace the system
Python on a Jetson; Nano preparation has a separate path in [the Nano guide](jetson-nano.md).
Dependencies use the version ranges in `pyproject.toml`; there is no complete
dependency lock. Record the actual environment when diagnosing version differences.

No sibling project or C++ compiler is needed. The controller wire contract and
current PWM path and configuration gaps are maintained in [the control contract](servo-controller.md).
Supplied camera/historical RS485 manuals are indexed [here](hardware-manuals.md);
PWM controller images and dated calibration records remain in [the M0 reference](legacy-m0-controller.md).
Host tests do not establish camera decode, GPU performance or physical servo behavior.

For a UI-only preview, run `python3 deploy/preview-web.py` and open
`http://127.0.0.1:18092`; stop with Ctrl+C. It rejects control commands.
`python3 -m shore_tracker --config config/example.toml doctor` is a static check,
not a connectivity or motion test. Exit code 1 on a non-Jetson host can mean
missing device prerequisites; inspect its individual checks. Do not start `serve`
as an offline preview or install device packages merely to make host checks green.
If pytest skips the serial test because `serial` is unavailable, the environment
is incomplete for that test; report the skip rather than claiming a full pass.

## Local configuration

### Nano recording card

Nano local recording uses the ext4 card mounted at `/mnt/shore-video`.
The preview does not write background recordings; footage is created only by
manual, automatic-following or ARM-session continuous recording triggers.
Its `data/recordings` and `data/manual-recordings` paths point to directories
on that mount. Keep the systemd mount guard in place. Use the capacity shown
in Web settings or the native App to check used and available GiB.

### Camera SD recording

Set `destination = "camera_sd"` in the active TOML's existing `[recording]`
section to control the camera's own recording schedule from Shore Jetson Web.
Omitting the setting selects the existing `jetson` recording mode. The camera
control account is supplied separately from the RTSP URI. See the
[Nano setup and recording acceptance steps](jetson-nano.md#camera-sd-recording).

### Web port 80

The runtime and supplied profiles default to HTTP port **80**. On an authorized
private-LAN installation, open `http://<Jetson-IP>/`; the native App, WebRTC relay
and SSH tunnel helpers use the same backend port. Offline preview remains on
18092. An existing configuration with an explicit port keeps that value; review
its `[web] port` and pass the same `--port` to the desktop/relay installers.
After migrating the backend, recreate existing SSH tunnels and restart the
recording receiver so they forward to port 80. Historical `apply-*-update.py`
helpers still target their original port 8090 and are not port-migration tools.

Linux normally reserves ports below 1024. The system service template grants
`CAP_NET_BIND_SERVICE` to its existing non-root account. A Nano `systemctl --user`
service cannot obtain that capability from an ordinary user manager. Its installer
checks the kernel's unprivileged-port threshold before making changes and stops
if the selected port is restricted. In that case, port 80 requires a separately
authorized administrator setup, such as a system service running as the Nano
user with `AmbientCapabilities=CAP_NET_BIND_SERVICE` and the correct user paths.
Do not run the camera runtime as root or apply capabilities to a shared Python
interpreter. Source changes do not migrate an installed service or configuration.
After migration to a system service, manage the backend with
`sudo systemctl status shore-tracker.service`; keep its former user service
disabled to prevent duplicate UART ownership. The WebRTC relay remains managed
with `systemctl --user status shore-tracker-webrtc.service`.

The populated camera lease and route guard remain local and are ignored by Git.
Before a separately authorized installation, prepare them from the examples:

```sh
cp deploy/shore-tracker-camera-lease.conf.example deploy/shore-tracker-camera-lease.conf
cp deploy/shore-tracker-camera-guard.nft.template.example deploy/shore-tracker-camera-guard.nft.template
```

Replace the documentation-only MAC/address in the lease and the `192.0.2.0/24`
subnet in the guard with independently verified local values. The lease must
belong to the dedicated camera subnet protected by the guard. Confirm the
dedicated interface and NetworkManager profile in the deployment helpers. The
installer checks that these local files exist before installing packages or
changing the system; copying examples alone does not complete commissioning.

For optional staging, copy `config/jetson-staging-live.example.toml` to the
ignored `config/jetson-staging-live.toml` and set paths for the target host. The
staging and installation helpers also contain deployment-account and directory
assumptions that must be reviewed for that host.

The owner reconfirmed on **2026-11-14** that current hardware is **CH340G USB-TTL
→ M0/M1 controller → two 180° PWM servos**; the RS485 head is stored.
The retained protocol is `m0_pulse`, 9600 8N1, ASCII `#00Pxxxx!` / `#01Pxxxx!`
without a newline. Follow [the control contract](servo-controller.md) and
[commissioning](commissioning.md) before any separately authorized device work.
Host-only layout preview:

```sh
python3 deploy/preview-web.py
```

Open `/kiosk?layout=nano&controls=1` on the preview server. Offline controls remain disabled.

The checkout's configurations have **not** been aligned with current hardware:

| Source | Current contents; not a verified device profile |
| --- | --- |
| `example.toml`, `jetson-disarmed.toml`, `jetson-nano.toml`, staging example | Disabled `pelco_d`, Moxa by-id path, address 1, speed caps 8, directions 0; historical RS485 profiles |
| `jetson-m0-uart.toml` | `m0_pulse`/9600, but old enabled `/dev/ttyTHS1` profile with unverified home, direction, ranges and speed |
| `ServoConfig` loader defaults | `m0_pulse`/9600, USB-TTL placeholder, enabled output and uncalibrated directions; defaults are not a deployment profile |

Do not copy these into an active installation without reviewing the actual
CH340G identity, wiring, channel mapping, directions, supply and mechanical limits.
The source's 0–180°/500–2500 µs mapping and dated M0 tests are not current physical
acceptance. This documentation task does not modify TOML or the installed configuration.
Existing private configs are not rewritten; omitted protocol with baud 115200
loads the historical `shore_spotter` SET format, which is not the PWM wire protocol.

The settings drawer's direction/speed manual panel requires `pelco_d`; it is not
a PWM control panel. Its [RS485 operation guide](pelco-d-test.md) is historical.
The standalone test page/server remains removed. Old deployment/motion helpers
retain device and neutral assumptions that must be reviewed before use.

Supply camera credentials locally through the protected environment mechanism
described in the README. Never put a real stream URI in configuration examples,
commands, tests or Git. Test fixtures use synthetic credentials and endpoints.

For a private UI tunnel and the recording receiver, pass a verified SSH host
alias explicitly, for example `deploy/open-web jetson`. The alias and its host
key belong in the user's local SSH configuration. Private Drive upload also
requires the separate `surf-track` receiver and its local credentials.

The root `AGENTS.md` and development guide are shareable source documentation.
Local `.agents/` skills, `.codex/` records, installed-camera inventory and
`STATUS.md` remain ignored. A fresh clone supports host development without these
private records. Camera discovery/networking/ONVIF/RTSP/PTZ work also requires
the local camera skill specified in `AGENTS.md`; obtain it before that work.
Raw media, model artifacts, build output and publication backups are excluded by
`.gitignore`. Review both file contents and the intended diff before any separately
authorized publication; an ignore rule does not remove already tracked content.


### Tailscale Web access

With `[web] allow_lan = true`, the dashboard also accepts clients in Tailscale's
`100.64.0.0/10` IPv4 range. Connect using the Nano's numeric Tailscale address.
The tailnet policy must grant the requesting device TCP 80 access to the Nano.
Host validation and same-origin control checks apply to these connections.
On installations using `/usr/local/sbin/shore-tailnet-guard`, also add the approved
device addresses to its TCP 80 input and established reply rules. The installed
`tailscaled` service loads that script before starting, so its saved rules must
match the intended tailnet Web access.
