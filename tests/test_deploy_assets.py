from pathlib import Path


def test_camera_guard_uses_kernel_compatible_drop() -> None:
    rules = Path("deploy/shore-tracker-camera-guard.nft.template.example").read_text()

    assert 'meta skuid "shore-tracker"' in rules
    assert " oifname != $shore_camera_interface drop" in rules
    assert " reject" not in rules


def test_installer_verifies_services_before_reporting_success() -> None:
    installer = Path("deploy/install-disarmed.sh").read_text()

    success = installer.index('echo "deployment=disarmed"')
    assert installer.index(
        "systemctl is-active --quiet shore-tracker-camera-guard.service"
    ) < success
    assert installer.index("systemctl is-active --quiet shore-tracker.service") < success


def test_camera_network_unit_uses_bounded_interface_specific_helper() -> None:
    helper = Path("deploy/activate-camera-network").read_text()
    unit = Path("deploy/shore-tracker-camera-network.service").read_text()
    installer = Path("deploy/install-disarmed.sh").read_text()

    assert 'interface=enP8p1s0' in helper
    assert 'profile=codex-ipcam-temp' in helper
    assert '"$attempt" -lt 60' in helper
    assert 'ifname "$interface"' in helper
    assert "ExecStart=/opt/shore-tracker/deploy/activate-camera-network" in unit
    assert 'chmod 0755 "$target/deploy/activate-camera-network"' in installer
    assert not (
        'install -o root -g root -m 0755 \\\n'
        '    "$target/deploy/activate-camera-network" \\\n'
        '    "$target/deploy/activate-camera-network"'
    ) in installer


def test_installer_prepares_kiosk_autologin_and_kiosk_waits_for_service() -> None:
    installer = Path("deploy/install-disarmed.sh").read_text()
    kiosk = Path("deploy/start-kiosk").read_text()

    assert 'cp -a "$gdm_config" "${gdm_config}.backup.${stamp}.$$"' in installer
    assert "AutomaticLoginEnable = true" in installer
    assert "AutomaticLogin = nolan" in installer
    assert '"$url/healthz"' in kiosk
    assert '"$attempt" -lt 90' in kiosk


def test_finalizer_stops_only_the_known_staging_runtime_before_install() -> None:
    finalizer = Path("deploy/apply-final-deployment").read_text()

    assert "jetson-staging-live.toml" in finalizer
    assert 'kill -TERM "$pid"' in finalizer
    assert '"$staging_root/secrets.env"' in finalizer
    assert '"$staging_root/models"' in finalizer


def test_remote_login_helper_keeps_password_out_of_output_and_enables_system_rdp() -> None:
    helper = Path("deploy/configure-remote-login").read_text()

    assert "stty -echo" in helper
    assert 'grdctl --system rdp set-credentials "$rdp_user" "$rdp_password"' in helper
    assert "--show-credentials" not in helper
    assert "gnome-remote-desktop.service" in helper
    assert "systemctl start gdm.service" in helper
    assert "systemctl enable --now gdm.service" not in helper
    assert "systemctl restart gnome-remote-desktop.service" in helper
    assert "rdp_address=" in helper
    assert "rdp_port=3389" in helper
