#!/bin/bash
# Dedicated Nano Ethernet only. No NAT, default route, DNS or Wi-Fi changes.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
[[ $(tr -d '\0' </proc/device-tree/model) == *'Jetson Nano'* ]] || exit 1
[[ -d /sys/class/net/eth0 ]] || exit 1
profile=shore-camera-benchmark
mkdir -p /etc/shore-tracker
touch /etc/shore-tracker/camera-unit.conf
if ! nmcli connection show "$profile" >/dev/null 2>&1; then
  nmcli connection add type ethernet ifname eth0 con-name "$profile" \
    ipv4.method manual ipv4.addresses 10.42.0.1/24 ipv4.never-default yes \
    ipv6.method link-local ipv6.never-default yes connection.autoconnect yes
fi
[[ $(nmcli -g connection.interface-name connection show "$profile") == eth0 ]] || exit 1
if ip -4 route show | awk '$1=="10.42.0.0/24" && $3!="eth0" {found=1} END {exit !found}'; then
  echo 'Camera subnet conflicts with another interface.' >&2; exit 1
fi
nmcli connection modify "$profile" connection.autoconnect yes \
  ipv4.method manual ipv4.addresses 10.42.0.1/24 ipv4.never-default yes \
  ipv4.gateway '' ipv4.dns '' ipv6.method link-local ipv6.never-default yes
cat >/etc/shore-tracker/camera-dhcp.conf <<'CONF'
port=0
interface=eth0
bind-interfaces
dhcp-range=10.42.0.100,10.42.0.254,255.255.255.0,12h
dhcp-option=3
dhcp-option=6
dhcp-authoritative
dhcp-leasefile=/run/shore-tracker-camera.leases
conf-file=/etc/shore-tracker/camera-unit.conf
CONF
cat >/etc/shore-tracker/camera-isolate <<'SCRIPT'
#!/bin/bash
set -euo pipefail
for direction in -i -o; do
  if ! /sbin/iptables -C FORWARD "$direction" eth0 -m comment --comment shore-tracker-camera -j DROP 2>/dev/null; then
    /sbin/iptables -I FORWARD 1 "$direction" eth0 -m comment --comment shore-tracker-camera -j DROP
  fi
done
SCRIPT
chmod 0700 /etc/shore-tracker/camera-isolate
cat >/etc/systemd/system/shore-tracker-camera-network.service <<'UNIT'
[Unit]
Description=Shore Jetson isolated Nano camera Ethernet
Requires=NetworkManager.service
After=NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/etc/shore-tracker/camera-isolate
ExecStart=/usr/bin/nmcli connection up shore-camera-benchmark
TimeoutStartSec=45

[Install]
WantedBy=multi-user.target
UNIT
cat >/etc/systemd/system/shore-tracker-camera-dhcp.service <<'UNIT'
[Unit]
Description=Shore Jetson camera DHCP without gateway or DNS
Requires=shore-tracker-camera-network.service
After=shore-tracker-camera-network.service

[Service]
Type=simple
ExecStart=/usr/sbin/dnsmasq --keep-in-foreground --conf-file=/etc/shore-tracker/camera-dhcp.conf
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl stop shore-camera-benchmark-dhcp.service 2>/dev/null || true
systemctl daemon-reload
systemctl enable shore-tracker-camera-network.service shore-tracker-camera-dhcp.service
systemctl start shore-tracker-camera-network.service shore-tracker-camera-dhcp.service
systemctl is-active shore-tracker-camera-network.service shore-tracker-camera-dhcp.service
echo CAMERA_NETWORK_INSTALLED
