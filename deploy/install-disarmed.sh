#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "run this installer through sudo" >&2
    exit 1
fi

if [ "$#" -ne 3 ]; then
    echo "usage: install-disarmed.sh SOURCE_DIR SECRETS_ENV MODEL_ARTIFACT_DIR" >&2
    exit 1
fi

source_dir=$1
secrets_env=$2
model_artifact_dir=$3
action_model="$model_artifact_dir/actions_mobilenet_v3_small.onnx"
detector_model="$model_artifact_dir/surfer_fcos_raw.onnx"
action_engine="$model_artifact_dir/engines/actions-mobilev3.fp16.engine"
detector_engine="$model_artifact_dir/engines/surfer-fcos.fp16.engine"
target=/opt/shore-tracker
state=/var/lib/shore-tracker

for required in \
    "$source_dir/pyproject.toml" \
    "$source_dir/config/jetson-disarmed.toml" \
    "$source_dir/deploy/shore-tracker.service" \
    "$source_dir/deploy/shore-tracker-camera-network.service" \
    "$source_dir/deploy/activate-camera-network" \
    "$source_dir/deploy/shore-tracker-camera-guard.service" \
    "$source_dir/deploy/shore-tracker-camera-guard.nft.template" \
    "$source_dir/deploy/shore-tracker-camera-lease.conf" \
    "$source_dir/deploy/shore-tracker.sysusers.conf" \
    "$secrets_env" \
    "$action_model" \
    "$detector_model" \
    "$action_engine" \
    "$detector_engine"
do
    if [ ! -f "$required" ] || [ -L "$required" ]; then
        echo "missing or unsafe deployment input: $required" >&2
        exit 1
    fi
done

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
    python3-venv \
    python3-wheel \
    python3-numpy \
    python3-pil \
    python3-serial \
    python3-libnvinfer \
    nftables \
    nvidia-jetpack-runtime

systemd-sysusers "$source_dir/deploy/shore-tracker.sysusers.conf"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
next="/opt/shore-tracker.next.$$"
if [ -e "$next" ]; then
    echo "staging target already exists: $next" >&2
    exit 1
fi
install -d -o root -g root -m 0755 "$next"
cp -a "$source_dir/." "$next/"
python3 -m venv --system-site-packages "$next/.venv"
"$next/.venv/bin/pip" install --no-build-isolation --no-deps "$next"

if [ -e "$target" ]; then
    backup="${target}.backup.${stamp}.$$"
    mv "$target" "$backup"
    echo "previous_application=$backup"
fi
mv "$next" "$target"

install -d -o root -g root -m 0755 /etc/shore-tracker
if [ -f /etc/shore-tracker/config.toml ]; then
    cp -a /etc/shore-tracker/config.toml "/etc/shore-tracker/config.toml.backup.${stamp}.$$"
fi
if [ -f /etc/shore-tracker/secrets.env ]; then
    cp -a /etc/shore-tracker/secrets.env "/etc/shore-tracker/secrets.env.backup.${stamp}.$$"
fi
install -o root -g root -m 0644 \
    "$target/config/jetson-disarmed.toml" /etc/shore-tracker/config.toml
install -o root -g root -m 0600 "$secrets_env" /etc/shore-tracker/secrets.env

install -d -o shore-tracker -g shore-tracker -m 0700 "$state"
install -d -o shore-tracker -g shore-tracker -m 0700 "$state/models"
install -d -o shore-tracker -g shore-tracker -m 0700 "$state/models/engines"
install -o shore-tracker -g shore-tracker -m 0600 \
    "$action_model" \
    "$state/models/actions_mobilenet_v3_small.onnx"
install -o shore-tracker -g shore-tracker -m 0600 \
    "$detector_model" \
    "$state/models/surfer_fcos_raw.onnx"
install -o shore-tracker -g shore-tracker -m 0600 \
    "$action_engine" \
    "$state/models/engines/actions-mobilev3.fp16.engine"
install -o shore-tracker -g shore-tracker -m 0600 \
    "$detector_engine" \
    "$state/models/engines/surfer-fcos.fp16.engine"

install -o root -g root -m 0644 \
    "$target/deploy/shore-tracker.service" /etc/systemd/system/shore-tracker.service
install -o root -g root -m 0644 \
    "$target/deploy/shore-tracker-camera-network.service" \
    /etc/systemd/system/shore-tracker-camera-network.service
chown root:root "$target/deploy/activate-camera-network"
chmod 0755 "$target/deploy/activate-camera-network"
install -o root -g root -m 0644 \
    "$target/deploy/shore-tracker-camera-guard.service" \
    /etc/systemd/system/shore-tracker-camera-guard.service
install -o root -g root -m 0644 \
    "$target/deploy/shore-tracker-camera-guard.nft.template" \
    /etc/shore-tracker/camera-guard.nft
install -d -o root -g root -m 0755 /etc/NetworkManager/dnsmasq-shared.d
install -o root -g root -m 0644 \
    "$target/deploy/shore-tracker-camera-lease.conf" \
    /etc/NetworkManager/dnsmasq-shared.d/shore-tracker-camera.conf
install -d -o nolan -g nolan -m 0755 /home/nolan/.config/autostart
install -o nolan -g nolan -m 0644 \
    "$target/deploy/shore-tracker-kiosk.desktop" \
    /home/nolan/.config/autostart/shore-tracker-kiosk.desktop

gdm_config=/etc/gdm3/custom.conf
if [ ! -f "$gdm_config" ] || [ -L "$gdm_config" ]; then
    echo "missing or unsafe GDM configuration: $gdm_config" >&2
    exit 1
fi
cp -a "$gdm_config" "${gdm_config}.backup.${stamp}.$$"
gdm_next=$(mktemp /etc/gdm3/custom.conf.next.XXXXXX)
sed \
    -e 's/^[[:space:]#]*AutomaticLoginEnable[[:space:]]*=.*/AutomaticLoginEnable = true/' \
    -e 's/^[[:space:]#]*AutomaticLogin[[:space:]]*=.*/AutomaticLogin = nolan/' \
    "$gdm_config" >"$gdm_next"
if [ "$(grep -c '^AutomaticLoginEnable = true$' "$gdm_next")" -ne 1 ] || \
   [ "$(grep -c '^AutomaticLogin = nolan$' "$gdm_next")" -ne 1 ]; then
    rm -f "$gdm_next"
    echo "could not configure unique nolan GDM autologin entries" >&2
    exit 1
fi
install -o root -g root -m 0644 "$gdm_next" "$gdm_config"
rm -f "$gdm_next"

systemctl daemon-reload
systemctl enable shore-tracker-camera-network.service
systemctl enable --now shore-tracker-camera-guard.service
systemctl enable --now shore-tracker.service

if ! systemctl is-active --quiet shore-tracker-camera-guard.service; then
    echo "deployment failed: camera guard is not active" >&2
    exit 1
fi
if ! systemctl is-active --quiet shore-tracker.service; then
    echo "deployment failed: Shore Jetson is not active" >&2
    exit 1
fi

echo "deployment=disarmed"
echo "archive=local_mkv_only"
echo "servo=disabled_pending_rs485_calibration"
echo "inference=native_tensorrt"
