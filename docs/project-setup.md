# Public source setup

Shore Jetson contains the application, host tests and deployment tooling. The
Python distribution, CLI, service account and installed paths retain the
`shore-tracker` name. This repository does not contain installed-device inventory,
private deployment history, credentials, models, captures or recordings.

## Host development

Use Python 3.11 or newer and Node.js for the browser-control tests:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
node tests/test_dashboard_controls.js
node tests/test_kiosk_controls.js
node tests/test_control_page.js
```

For source-contract work, keep the reviewed Shore-Spotter checkout beside this
repository and run `python3 -m tools.check_shore_spotter` before editing. Review
any version or interface drift as described in [the source boundary](shore-spotter.md).
The C++ comparison test requires that sibling checkout and a C++17 compiler;
without them it is skipped. Host tests do not establish installed firmware,
camera decode, GPU performance or physical servo behavior.

## Local configuration

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

New direct M0 deployments use `config/jetson-m0-uart.toml` and the explicit
migration/commissioning flow in [commissioning](commissioning.md). The initial
installer's `jetson-disarmed.toml` retains the legacy serial configuration; it
does not silently select the new controller protocol.

Supply camera credentials locally through the protected environment mechanism
described in the README. Never put a real stream URI in configuration examples,
commands, tests or Git. Test fixtures use synthetic credentials and endpoints.

For a private UI tunnel and the recording receiver, pass a verified SSH host
alias explicitly, for example `deploy/open-web jetson`. The alias and its host
key belong in the user's local SSH configuration. Private Drive upload also
requires the separate `surf-track` receiver and its local credentials.

Agent instructions, installed-camera references and `STATUS.md` remain local.
Raw media, model artifacts, build output and publication backups are excluded
by `.gitignore`.
