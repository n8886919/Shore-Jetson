#!/bin/bash
# Installs the application and a user service, without networking or actuation.
set -euo pipefail
[[ $(id -u) != 0 ]] || exit 1
source_dir=${1:?Usage: install-nano-user.sh SOURCE_DIR [DESKTOP_BINARY]}
desktop_binary=${2:-$source_dir/build/native/shore-tracker-desktop}
[[ -x "$desktop_binary" ]] || { echo "Build native/ with Qt 5.9+ first" >&2; exit 1; }
base="$HOME/.local/share/shore-tracker"
config_dir="$HOME/.config/shore-tracker"
python="$base/venv/bin/python"
selected_config="$config_dir/config.toml"
[[ -e "$selected_config" ]] || selected_config="$source_dir/config/jetson-nano.toml"
web_port=$(PYTHONPATH="$source_dir" "$python" - "$selected_config" <<'PY'
import sys
from shore_tracker.config import load_config
print(load_config(sys.argv[1]).web.port)
PY
)
unprivileged_port_start=1024
if [[ -r /proc/sys/net/ipv4/ip_unprivileged_port_start ]]; then
  read -r unprivileged_port_start </proc/sys/net/ipv4/ip_unprivileged_port_start
fi
if (( web_port < unprivileged_port_start )); then
  echo "User service cannot bind port $web_port; see docs/project-setup.md (Web port 80)." >&2
  exit 1
fi
"$python" -c 'import gi, tensorrt; assert tensorrt.__version__.startswith("8.2.1")'
umask 077
mkdir -p "$config_dir" "$base/data/recordings" "$base/models/engines" \
  "$HOME/.config/systemd/user" "$HOME/.config/autostart"
"$python" -m pip install --no-build-isolation --no-deps "$source_dir"
if [[ ! -e "$config_dir/config.toml" ]]; then
  install -m 0600 "$source_dir/config/jetson-nano.toml" "$config_dir/config.toml"
fi
cat >"$HOME/.config/systemd/user/shore-tracker.service" <<'UNIT'
[Unit]
Description=Shore Jetson on Jetson Nano

[Service]
Type=simple
UMask=0077
WorkingDirectory=%h/.local/share/shore-tracker/app
Environment=OPENBLAS_NUM_THREADS=1
EnvironmentFile=-%h/.config/shore-tracker/secrets.env
ExecStart=%h/.local/share/shore-tracker/venv/bin/python -m shore_tracker --config %h/.config/shore-tracker/config.toml serve
Restart=on-failure
RestartSec=3
TimeoutStopSec=40
NoNewPrivileges=true
LimitCORE=0

[Install]
WantedBy=default.target
UNIT
"$base/venv/bin/python" "$source_dir/deploy/install-kiosk-launcher.py"
"$base/venv/bin/python" "$source_dir/deploy/install-desktop.py" "$desktop_binary" --port "$web_port"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
systemctl --user daemon-reload
systemctl --user enable --now shore-tracker.service
systemctl --user is-active shore-tracker.service
echo 'Application installed; hardware readiness must be checked separately.'
