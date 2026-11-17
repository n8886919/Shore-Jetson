import json
import subprocess

import pytest

from shore_tracker.network import (
    NetworkInterfaceError,
    interface_ipv4,
    route_interface,
    route_ipv4,
)


def _result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, "")


def test_interface_ipv4_uses_only_global_address_and_broadcast() -> None:
    payload = [
        {
            "ifname": "wlP1p1s0",
            "addr_info": [
                {"family": "inet6", "scope": "link", "local": "fe80::1"},
                {
                    "family": "inet",
                    "scope": "global",
                    "local": "192.0.2.12",
                    "broadcast": "192.0.2.255",
                    "prefixlen": 24,
                },
            ],
        }
    ]
    calls = []

    def execute(argv):
        calls.append(argv)
        return _result(json.dumps(payload))

    result = interface_ipv4("wlP1p1s0", executor=execute)
    assert result.address == "192.0.2.12"
    assert result.broadcast == "192.0.2.255"
    assert calls == [["ip", "-j", "-4", "addr", "show", "dev", "wlP1p1s0"]]


def test_interface_ipv4_rejects_missing_link() -> None:
    with pytest.raises(NetworkInterfaceError, match="unavailable"):
        interface_ipv4("wlan0", executor=lambda _argv: _result("", 1))


def test_route_interface_is_scoped_to_one_address() -> None:
    def execute(argv):
        assert argv == ["ip", "-j", "route", "get", "192.0.2.44"]
        return _result('[{"dst":"192.0.2.44","dev":"wlP1p1s0"}]')

    assert route_interface("192.0.2.44", executor=execute) == "wlP1p1s0"


def test_route_interface_rejects_hostnames() -> None:
    with pytest.raises(NetworkInterfaceError, match="IPv4"):
        route_interface("camera.local")


def test_route_ipv4_requires_and_returns_selected_source() -> None:
    def execute(argv):
        assert argv == ["ip", "-j", "route", "get", "192.0.2.44"]
        return _result(
            '[{"dst":"192.0.2.44","dev":"enP8p1s0",'
            '"prefsrc":"192.0.2.10"}]'
        )

    result = route_ipv4("192.0.2.44", executor=execute)
    assert result.interface == "enP8p1s0"
    assert result.source == "192.0.2.10"
    assert result.gateway is None


def test_legacy_iproute_placeholder_rows_do_not_hide_selected_address():
    output = [{"addr_info": [{}]}, {"ifname": "eth0", "addr_info": [
        {"family": "inet", "scope": "global", "local": "192.0.2.1",
         "broadcast": "192.0.2.255", "prefixlen": 24}]}, {"addr_info": [{}]}]
    assert interface_ipv4("eth0", executor=lambda _: _result(json.dumps(output))).address == "192.0.2.1"
    with pytest.raises(NetworkInterfaceError):
        interface_ipv4("wlan0", executor=lambda _: _result(json.dumps(output)))


def test_legacy_text_route_preserves_source_and_gateway():
    output = "192.0.2.44 via 198.51.100.1 dev wlan0 src 198.51.100.2 uid 1000\n    cache\n"
    route = route_ipv4("192.0.2.44", executor=lambda _: _result(output))
    assert (route.interface, route.source, route.gateway) == ("wlan0", "198.51.100.2", "198.51.100.1")
    assert route_interface("192.0.2.44", executor=lambda _: _result(output)) == "wlan0"


@pytest.mark.parametrize("output", ["", "192.0.2.45 dev eth0 src 192.0.2.1",
    "192.0.2.44 dev eth0", "192.0.2.44 dev eth0 src 192.0.2.1 dev wlan0"])
def test_legacy_text_route_rejects_incomplete_or_ambiguous_results(output):
    with pytest.raises(NetworkInterfaceError):
        route_ipv4("192.0.2.44", executor=lambda _: _result(output))
