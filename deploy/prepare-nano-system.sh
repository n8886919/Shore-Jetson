#!/bin/bash
# Run only on a dedicated, user-authorized Jetson Nano appliance.
# Removes the listed unrelated applications without backups or autoremove.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
user=${1:?Usage: prepare-nano-system.sh LOGIN_USER}
id "$user" >/dev/null
[[ $(uname -m) == aarch64 ]] || exit 1
grep -q 't210' /etc/nv_tegra_release || exit 1
grep -q 'REVISION: 7.6' /etc/nv_tegra_release || exit 1

export DEBIAN_FRONTEND=noninteractive
remove=(libreoffice-common libreoffice-core thunderbird rhythmbox totem
  transmission-common simple-scan shotwell remmina gnome-mahjongg gnome-mines
  gnome-sudoku aisleriot cheese docker.io containerd snapd gpsd gpsd-clients)
# Fail before mutation if apt proposes removing essential appliance components.
plan=$(apt-get -s purge "${remove[@]}")
if printf '%s\n' "$plan" | grep -E '^Purg (openssh|network-manager|wpasupplicant|gdm3|lightdm|xserver|chromium|nvidia-l4t|libc6 |systemd )'; then
  echo 'Unexpected removal of required system components; stopping.' >&2
  exit 1
fi
apt-get purge -y "${remove[@]}"
apt-get clean
apt-get update
apt-get install -y --no-install-recommends \
  build-essential cmake pkg-config libffi-dev libgirepository1.0-dev \
  cuda-cudart-dev-10-2 \
  libcairo2-dev libnvinfer8 libnvinfer-plugin8 libnvonnxparsers8 libnvparsers8 \
  libnvinfer-dev libnvinfer-plugin-dev libnvonnxparsers-dev libnvparsers-dev \
  python3-libnvinfer python3-gi gir1.2-gst-plugins-base-1.0
apt-get clean
usermod -aG dialout,video "$user"
loginctl enable-linger "$user"
printf '\nNANO_SYSTEM_READY\n'
df -h /
