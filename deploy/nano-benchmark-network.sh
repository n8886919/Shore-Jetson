#!/bin/bash
# Explicitly authorized temporary direct-camera networking for an FPS test.
# No shared-mode NAT, Internet gateway, Wi-Fi changes, or boot enablement.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
profile=shore-camera-benchmark
unit=shore-camera-benchmark-dhcp
if [[ ${1:-} == --remove ]]; then
  systemctl stop "$unit.service" || true
  nmcli connection delete "$profile" || true
  for direction in -i -o; do
    if iptables -C FORWARD "$direction" eth0 -m comment --comment shore-camera-benchmark -j DROP 2>/dev/null; then
      iptables -D FORWARD "$direction" eth0 -m comment --comment shore-camera-benchmark -j DROP
    fi
  done
  exit 0
fi
[[ -d /sys/class/net/eth0 ]] || exit 1
[[ $(cat /sys/class/net/eth0/carrier) == 1 ]] || { echo 'Camera cable is disconnected.' >&2; exit 1; }
if nmcli -g connection.id connection show "$profile" >/dev/null 2>&1; then
  echo 'Benchmark profile already exists; inspect it or use --remove first.' >&2
  exit 1
fi
if ip -4 address show dev eth0 | grep -q 'inet '; then
  echo 'eth0 already has IPv4 configuration; refusing to replace it.' >&2
  exit 1
fi
if ip -4 route show | grep -q '^10\.42\.0\.'; then
  echo 'Camera subnet conflicts with an existing route.' >&2
  exit 1
fi
for direction in -i -o; do
  if ! iptables -C FORWARD "$direction" eth0 -m comment --comment shore-camera-benchmark -j DROP 2>/dev/null; then
    iptables -I FORWARD 1 "$direction" eth0 -m comment --comment shore-camera-benchmark -j DROP
  fi
done
nmcli connection add type ethernet ifname eth0 con-name "$profile" \
  ipv4.method manual ipv4.addresses 10.42.0.1/24 ipv4.never-default yes \
  ipv6.method link-local ipv6.never-default yes connection.autoconnect no
nmcli connection up "$profile"
systemd-run --unit="$unit" /usr/sbin/dnsmasq --keep-in-foreground \
  --conf-file=/dev/null --port=0 --interface=eth0 --bind-interfaces \
  --dhcp-range=10.42.0.100,10.42.0.254,255.255.255.0,12h \
  --dhcp-option=3 --dhcp-option=6 --dhcp-authoritative \
  --dhcp-leasefile=/run/shore-camera-benchmark.leases
echo 'CAMERA_BENCHMARK_NETWORK_READY'
echo 'To remove this temporary setup, run this script with --remove.'
