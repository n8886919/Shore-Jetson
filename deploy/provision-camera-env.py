#!/usr/bin/env python3
"""Fetch the current ONVIF RTSP URI into a private systemd env file.

The URI is never printed. This helper is intentionally limited to the current
direct-camera IPv4 address and rejects redirects to another host. Cameras that
were verified to permit unauthenticated decode can use ``--no-auth``.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import os
import tempfile
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree


MAX_SOAP_BYTES = 256 * 1024


def soap(url: str, body: str) -> ElementTree.Element:
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        f"<s:Body>{body}</s:Body></s:Envelope>"
    ).encode()
    request = Request(
        url,
        data=envelope,
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        payload = response.read(MAX_SOAP_BYTES + 1)
    if len(payload) > MAX_SOAP_BYTES:
        raise ValueError("ONVIF response is too large")
    return ElementTree.fromstring(payload)


def fetch_rtsp_uri(camera_host: str) -> str:
    host = str(ipaddress.IPv4Address(camera_host))
    device_url = f"http://{host}/onvif/device_service"
    capabilities = soap(
        device_url,
        '<tds:GetCapabilities xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
        '<tds:Category>Media</tds:Category></tds:GetCapabilities>',
    )
    media_url = capabilities.findtext(".//{*}Media/{*}XAddr") or ""
    parsed_media = urlparse(media_url)
    if parsed_media.scheme != "http" or parsed_media.hostname != host:
        raise ValueError("ONVIF media endpoint changed host")

    profiles = soap(
        media_url,
        '<trt:GetProfiles xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/>',
    )
    token = ""
    for profile in profiles.findall(".//{*}Profiles"):
        candidate = profile.attrib.get("token", "")
        if candidate:
            token = candidate
            break
    if not token:
        raise ValueError("ONVIF returned no usable profile")

    stream = soap(
        media_url,
        '<trt:GetStreamUri xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        '<trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>'
        '<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>'
        '</trt:StreamSetup>'
        f'<trt:ProfileToken>{token}</trt:ProfileToken></trt:GetStreamUri>',
    )
    uri = stream.findtext(".//{*}Uri") or ""
    parsed_uri = urlparse(uri)
    if (
        parsed_uri.scheme not in {"rtsp", "rtsps"}
        or parsed_uri.hostname != host
        or any(character in uri for character in ("\x00", "\n", "\r"))
    ):
        raise ValueError("ONVIF returned an unsafe stream URI")
    return uri


def add_credentials(uri: str, username: str, password: str) -> str:
    if not username or not password:
        raise ValueError("camera username and password are required")
    parsed = urlparse(uri)
    if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
        raise ValueError("RTSP URI is invalid")
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    authority = f"{quote(username, safe='')}:{quote(password, safe='')}@{host}"
    return urlunparse(parsed._replace(netloc=authority))


def write_environment(path: Path, uri: str) -> None:
    destination = path.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    quoted = uri.replace("\\", "\\\\").replace('"', '\\"')
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f'SHORE_TRACKER_RTSP_URI="{quoted}"\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera-host", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-auth", action="store_true")
    args = parser.parse_args()
    uri = fetch_rtsp_uri(args.camera_host)
    if not args.no_auth:
        username = input("Camera username: ").strip()
        password = getpass.getpass("Camera password: ")
        uri = add_credentials(uri, username, password)
    write_environment(args.output, uri)
    print("camera_environment=written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
