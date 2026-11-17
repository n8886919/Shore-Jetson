#!/bin/bash
# Repair the observed custom OpenSSH 9.6 daemon / Type=notify mismatch.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run using sudo' >&2; exit 1; }
grep -aq p3448 /proc/device-tree/compatible
grep -q 'REVISION: 7.6' /etc/nv_tegra_release
[[ $(systemctl show ssh.service -p KillMode --value) == process ]]
[[ $(systemctl show ssh.service -p FragmentPath --value) == /lib/systemd/system/ssh.service ]]
/usr/sbin/sshd -V 2>&1 | grep -q '^OpenSSH_9.6'
/usr/sbin/sshd -t
ldd /usr/sbin/sshd | grep -q '/opt/openssl-3.1.4/lib/libcrypto.so.3'
dropin=/etc/systemd/system/ssh.service.d/shore-nano-custom-sshd.conf
expected=$'[Service]\nType=simple'
if [[ -e "$dropin" ]]; then
    [[ $(cat "$dropin") == "$expected" ]] || {
        echo 'Unexpected existing SSH override; refusing to overwrite' >&2; exit 1;
    }
fi
install -d -m 755 /etc/systemd/system/ssh.service.d
printf '%s\n' "$expected" > "$dropin"
chmod 644 "$dropin"
systemctl daemon-reload
[[ $(systemctl show ssh.service -p Type --value) == simple ]]
# KillMode=process retains the authenticated session while replacing the listener.
systemctl restart ssh.service
sleep 2
systemctl is-active --quiet ssh.service
[[ $(systemctl show ssh.service -p SubState --value) == running ]]
ss -ltn 'sport = :22' | grep -q LISTEN
echo NANO_SSH_STARTUP_FIXED
