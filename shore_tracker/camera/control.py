"""Bounded ONVIF lens and imaging controls for the dedicated camera link."""
from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections.abc import Callable
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree as ET


NS = {
    "d": "http://www.onvif.org/ver10/device/wsdl",
    "m": "http://www.onvif.org/ver10/media/wsdl",
    "p": "http://www.onvif.org/ver20/ptz/wsdl",
    "i": "http://www.onvif.org/ver20/imaging/wsdl",
    "t": "http://www.onvif.org/ver10/schema",
}

# key, label, settings path, options path, control kind
FIELDS = (
    ("brightness", "亮度", "Brightness", "Brightness", "number"),
    ("contrast", "對比", "Contrast", "Contrast", "number"),
    ("saturation", "飽和度", "ColorSaturation", "ColorSaturation", "number"),
    ("sharpness", "銳利度", "Sharpness", "Sharpness", "number"),
    ("exposure_mode", "曝光模式", "Exposure/Mode", "Exposure/Mode", "select"),
    ("exposure_time", "曝光時間（µs）", "Exposure/ExposureTime", "Exposure/ExposureTime", "number"),
    ("white_balance", "白平衡", "WhiteBalance/Mode", "WhiteBalance/Mode", "select"),
    ("red_gain", "紅色增益", "WhiteBalance/CrGain", "WhiteBalance/YrGain", "number"),
    ("blue_gain", "藍色增益", "WhiteBalance/CbGain", "WhiteBalance/YbGain", "number"),
    ("wdr_mode", "寬動態 WDR", "WideDynamicRange/Mode", "WideDynamicRange/Mode", "select"),
    ("wdr_level", "WDR 強度", "WideDynamicRange/Level", "WideDynamicRange/Level", "number"),
    ("backlight_mode", "背光補償", "BacklightCompensation/Mode", "BacklightCompensation/Mode", "select"),
    ("backlight_level", "背光補償強度", "BacklightCompensation/Level", "BacklightCompensation/Level", "number"),
    ("ir_cut", "日夜濾鏡 IR-cut", "IrCutFilter", "IrCutFilterModes", "select"),
    ("defog_mode", "除霧", "Extension/Extension/Extension/Defogging/Mode", "Extension/Extension/Extension/DefoggingOptions/Mode", "select"),
    ("defog_level", "除霧強度", "Extension/Extension/Extension/Defogging/Level", "Extension/Extension/Extension/DefoggingOptions/Level", "unit"),
    ("noise_reduction", "降噪強度", "Extension/Extension/Extension/NoiseReduction/Level", "Extension/Extension/Extension/NoiseReductionOptions/Level", "unit"),
)


def find(node: ET.Element, path: str) -> ET.Element | None:
    return node.find("/".join("{*}" + part for part in path.split("/")))


def value(node: ET.Element, path: str) -> str | None:
    child = find(node, path)
    return None if child is None else child.text


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError("camera redirect refused")


class CameraControls:
    def __init__(self, host_reader: Callable[[], str], *, sleeper=time.sleep) -> None:
        self.host_reader = host_reader
        self.sleeper = sleeper
        self._lock = threading.RLock()
        self._host = ""
        self._urls: dict[str, str] = {}
        self._profile = ""
        self._source = ""
        self._zoom = False
        self._focus = False
        self._focus_speed = 1.0
        self._opener = build_opener(_NoRedirect)

    def _soap(self, url: str, body: ET.Element) -> ET.Element:
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != self._host or parsed.username or parsed.password:
            raise ValueError("unexpected camera endpoint")
        envelope = ET.Element("{http://www.w3.org/2003/05/soap-envelope}Envelope")
        ET.SubElement(envelope, "{http://www.w3.org/2003/05/soap-envelope}Body").append(body)
        request = Request(url, data=ET.tostring(envelope), headers={"Content-Type": "application/soap+xml; charset=utf-8"}, method="POST")
        with self._opener.open(request, timeout=2) as response:
            payload = response.read(262145)
        if len(payload) > 262144:
            raise ValueError("camera response too large")
        root = ET.fromstring(payload)
        if root.find(".//{*}Fault") is not None:
            raise RuntimeError("camera rejected request")
        if root.find(".//{*}" + body.tag.rsplit("}", 1)[-1] + "Response") is None:
            raise RuntimeError("missing camera acknowledgement")
        return root

    def _request(self, service: str, operation: str) -> ET.Element:
        root = ET.Element("{" + NS[service] + "}" + operation)
        if service in {"i", "p"}:
            token = "VideoSourceToken" if service == "i" else "ProfileToken"
            ET.SubElement(root, "{" + NS[service] + "}" + token).text = self._source if service == "i" else self._profile
        return root

    def _discover(self) -> None:
        host = str(ipaddress.IPv4Address(self.host_reader()))
        if self._host == host and self._urls:
            return
        self._host = host
        cap = self._request("d", "GetCapabilities")
        ET.SubElement(cap, "{" + NS["d"] + "}Category").text = "All"
        response = self._soap(f"http://{host}/onvif/device_service", cap)
        urls = {key: response.findtext(".//{*}" + name + "/{*}XAddr") or "" for key, name in (("m", "Media"), ("p", "PTZ"), ("i", "Imaging"))}
        for key in ("m", "i"):
            if urlsplit(urls[key]).hostname != host:
                raise RuntimeError("camera control unavailable")
        profiles = self._soap(urls["m"], self._request("m", "GetProfiles"))
        profile = profiles.find(".//{*}Profiles")
        if profile is None:
            raise RuntimeError("camera profile unavailable")
        self._profile = profile.get("token", "")
        self._source = profile.findtext("{*}VideoSourceConfiguration/{*}SourceToken") or ""
        self._zoom = profile.find("{*}PTZConfiguration/{*}ZoomLimits") is not None and urlsplit(urls["p"]).hostname == host
        if not self._source or not self._profile:
            raise RuntimeError("camera tokens unavailable")
        moves = self._soap(urls["i"], self._request("i", "GetMoveOptions"))
        speed = moves.find(".//{*}MoveOptions/{*}Continuous/{*}Speed")
        self._focus = speed is not None
        if speed is not None:
            maximum = float(value(speed, "Max") or 1)
            self._focus_speed = min(1.0, maximum)
        self._urls = urls

    def _read(self) -> tuple[ET.Element, list[dict]]:
        self._discover()
        settings = self._soap(self._urls["i"], self._request("i", "GetImagingSettings")).find(".//{*}ImagingSettings")
        options = self._soap(self._urls["i"], self._request("i", "GetOptions")).find(".//{*}ImagingOptions")
        if settings is None or options is None:
            raise RuntimeError("camera imaging options unavailable")
        fields = []
        for key, label, path, option_path, kind in FIELDS:
            current, option = value(settings, path), find(options, option_path)
            if current is None or option is None:
                continue
            field = {"key": key, "label": label, "kind": "number" if kind == "unit" else kind}
            if kind == "select":
                choices = [node.text for node in options.findall("/".join("{*}" + part for part in option_path.split("/"))) if node.text]
                field.update(value=current, choices=list(dict.fromkeys([current, *choices])))
            else:
                low, high = (0.0, 1.0) if kind == "unit" else (float(value(option, "Min") or 0), float(value(option, "Max") or 0))
                field.update(value=float(current), min=low, max=high, step=0.01 if kind == "unit" else 1)
            fields.append(field)
        return settings, fields

    def snapshot(self) -> dict:
        with self._lock:
            _, fields = self._read()
            return {"fields": fields, "zoom": self._zoom, "focus": self._focus, "zoom_factor": None}

    def _stop(self, service: str) -> None:
        last_error = None
        for _ in range(3):
            try:
                request = self._request(service, "Stop")
                if service == "p":
                    ET.SubElement(request, "{" + NS[service] + "}PanTilt").text = "false"
                    ET.SubElement(request, "{" + NS[service] + "}Zoom").text = "true"
                self._soap(self._urls[service], request)
                return
            except Exception as exc:
                last_error = exc
        raise RuntimeError("camera stop not confirmed") from last_error

    def command(self, action: str, payload: dict) -> None:
        with self._lock:
            self._discover()
            if action in {"zoom_in", "zoom_out", "focus_near", "focus_far"}:
                if payload:
                    raise ValueError("lens step takes no parameters")
                zoom = action.startswith("zoom")
                if not (self._zoom if zoom else self._focus):
                    raise ValueError("lens control unavailable")
                service = "p" if zoom else "i"
                request = self._request(service, "ContinuousMove" if zoom else "Move")
                if zoom:
                    velocity = ET.SubElement(request, "{" + NS[service] + "}Velocity")
                    ET.SubElement(velocity, "{" + NS["t"] + "}Zoom", x="0.2" if action == "zoom_in" else "-0.2")
                    ET.SubElement(request, "{" + NS[service] + "}Timeout").text = "PT1S"
                else:
                    focus = ET.SubElement(request, "{" + NS[service] + "}Focus")
                    continuous = ET.SubElement(focus, "{" + NS["t"] + "}Continuous")
                    ET.SubElement(continuous, "{" + NS["t"] + "}Speed").text = str(self._focus_speed if action == "focus_far" else -self._focus_speed)
                try:
                    self._soap(self._urls[service], request)
                    self.sleeper(0.3 if zoom else 0.15)
                finally:
                    self._stop(service)
            elif action == "stop":
                if payload: raise ValueError("stop takes no parameters")
                failed = False
                for service, available in (("p", self._zoom), ("i", self._focus)):
                    if available:
                        try: self._stop(service)
                        except Exception: failed = True
                if failed: raise RuntimeError("lens stop not confirmed")
            elif action == "settings":
                self._apply_settings(payload)
            else:
                raise ValueError("unknown camera action")

    def _apply_settings(self, changes: dict) -> None:
        settings, fields = self._read()
        by_key = {field["key"]: field for field in fields}
        if not changes or set(changes) - set(by_key):
            raise ValueError("unknown or empty camera settings")
        for key, item in changes.items():
            field = by_key[key]
            if field["kind"] == "select":
                if item not in field["choices"]: raise ValueError("invalid camera mode")
            elif type(item) not in {int, float} or not math.isfinite(item) or not field["min"] <= item <= field["max"]:
                raise ValueError("camera value outside supported range")
        request = self._request("i", "SetImagingSettings")
        target = ET.SubElement(request, "{" + NS["i"] + "}ImagingSettings")
        paths = {key: path for key, _, path, _, _ in FIELDS}
        for key, item in changes.items():
            path = paths[key]
            node = target
            parts = path.split("/")
            for part in parts:
                child = node.find("{" + NS["t"] + "}" + part)
                if child is None: child = ET.SubElement(node, "{" + NS["t"] + "}" + part)
                node = child
            node.text = str(item)
        # ONVIF imaging groups require their mode even for a level-only update.
        for group in ("Exposure", "WhiteBalance", "WideDynamicRange", "BacklightCompensation"):
            node = target.find("{" + NS["t"] + "}" + group)
            mode = value(settings, group + "/Mode")
            if node is not None and node.find("{" + NS["t"] + "}Mode") is None and mode:
                child = ET.Element("{" + NS["t"] + "}Mode")
                child.text = mode
                node.insert(0, child)
        order = {name: index for index, name in enumerate(("BacklightCompensation", "Brightness", "ColorSaturation", "Contrast", "Exposure", "Focus", "IrCutFilter", "Sharpness", "WideDynamicRange", "WhiteBalance", "Extension"))}
        target[:] = sorted(target, key=lambda child: order.get(child.tag.rsplit("}", 1)[-1], 100))
        for group in target:
            group[:] = sorted(group, key=lambda child: 0 if child.tag.rsplit("}", 1)[-1] == "Mode" else 1)
        ET.SubElement(request, "{" + NS["i"] + "}ForcePersistence").text = "false"
        self._soap(self._urls["i"], request)
