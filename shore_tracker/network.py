from __future__ import annotations

import ipaddress
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


Executor = Callable[[list[str]], subprocess.CompletedProcess[str]]


class NetworkInterfaceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InterfaceIPv4:
    interface: str
    address: str
    broadcast: str
    prefix_length: int


@dataclass(frozen=True, slots=True)
class RouteIPv4:
    interface: str
    source: str
    gateway: str | None = None


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def interface_ipv4(interface: str, *, executor: Executor = _run) -> InterfaceIPv4:
    """Read one global IPv4 address without exposing SSIDs or credentials."""

    if not interface or any(character.isspace() for character in interface):
        raise ValueError("invalid network interface")
    try:
        result = executor(["ip", "-j", "-4", "addr", "show", "dev", interface])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NetworkInterfaceError("cannot inspect network interface") from exc
    if result.returncode != 0:
        raise NetworkInterfaceError(f"interface {interface} is unavailable")
    try:
        payload: Any = json.loads(result.stdout)
        # iproute2 4.15 (JetPack 4) emits empty placeholder objects before
        # and after the selected interface. Match the actual interface row.
        entries = [entry for item in payload if item.get("ifname") == interface
                   for entry in item.get("addr_info", [])]
        candidates = [
            entry
            for entry in entries
            if entry.get("family") == "inet"
            and entry.get("scope") == "global"
            and entry.get("local")
            and entry.get("broadcast")
        ]
        if not candidates:
            raise ValueError("no global IPv4 address")
        entry = candidates[0]
        address = str(ipaddress.IPv4Address(entry["local"]))
        broadcast = str(ipaddress.IPv4Address(entry["broadcast"]))
        prefix_length = int(entry["prefixlen"])
        ipaddress.IPv4Network(f"{address}/{prefix_length}", strict=False)
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise NetworkInterfaceError(
            f"interface {interface} has no usable global IPv4 address"
        ) from exc
    return InterfaceIPv4(interface, address, broadcast, prefix_length)


def route_interface(destination: str, *, executor: Executor = _run) -> str:
    """Return the kernel-selected interface for a single already-known address."""

    try:
        address = str(ipaddress.IPv4Address(destination))
        result = executor(["ip", "-j", "route", "get", address])
    except ValueError as exc:
        raise NetworkInterfaceError("destination must be one IPv4 address") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NetworkInterfaceError("cannot inspect destination route") from exc
    if result.returncode != 0:
        raise NetworkInterfaceError("destination has no route")
    try:
        interface = _route_item(result.stdout, address)["dev"]
        if not isinstance(interface, str) or not interface:
            raise ValueError
        return interface
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise NetworkInterfaceError("route output is invalid") from exc


def route_ipv4(destination: str, *, executor: Executor = _run) -> RouteIPv4:
    """Return the selected device and source for one literal IPv4 route."""

    try:
        address = str(ipaddress.IPv4Address(destination))
        result = executor(["ip", "-j", "route", "get", address])
    except ValueError as exc:
        raise NetworkInterfaceError("destination must be one IPv4 address") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NetworkInterfaceError("cannot inspect destination route") from exc
    if result.returncode != 0:
        raise NetworkInterfaceError("destination has no route")
    try:
        item = _route_item(result.stdout, address)
        interface = item["dev"]
        source = item.get("prefsrc", item.get("src"))
        gateway_value = item.get("gateway")
        if not isinstance(interface, str) or not interface:
            raise ValueError
        if not isinstance(source, str):
            raise ValueError
        source = str(ipaddress.IPv4Address(source))
        gateway = (
            None
            if gateway_value is None
            else str(ipaddress.IPv4Address(gateway_value))
        )
        return RouteIPv4(interface=interface, source=source, gateway=gateway)
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise NetworkInterfaceError("route output is invalid") from exc


def _route_item(output: str, destination: str) -> dict[str, Any]:
    if output.lstrip().startswith("["):
        return json.loads(output)[0]
    # Older iproute2 accepts -j but still prints route-get output as text.
    # Require the exact literal destination and preserve gateway/source checks.
    words = output.splitlines()[0].split()
    if not words or words[0] != destination:
        raise ValueError("unexpected route destination")
    result = {}
    for token, key in (("dev", "dev"), ("src", "src"), ("via", "gateway")):
        if words.count(token) > 1:
            raise ValueError("ambiguous route field")
        if token in words:
            result[key] = words[words.index(token) + 1]
    return result
