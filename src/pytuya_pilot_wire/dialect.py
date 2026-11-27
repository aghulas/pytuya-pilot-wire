"""Codec between the device model and the Zigbee2MQTT dialect of the bridge's MQTT contract.

The contract lives in ``tuyapilot2mqtt/docs/mqtt-contract.md``. Its property names are provisional
until the device's Zigbee2MQTT definition is known: they are all in :data:`PROPERTIES`, and
nowhere else.

Pure functions, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import dps
from .errors import DialectError
from .model import Device, DeviceSpec, DeviceState

CONTRACT_VERSION = "0"

# Descriptor constants. The model name is provisional (contract §2: fixed at milestone M0).
DEFINITION_VENDOR = "Tuya"
DEFINITION_MODEL = "FP Thermostat"
DEFINITION_DESCRIPTION = "Pilot-wire thermostat"

# Raw Tuya mode to dialect preset (names of contract version 2 onwards). The frost-protection
# value is reported by the devices outside their declared mode list; a raw mode that is not here
# gives no preset at all.
PRESETS: dict[str, str] = {
    "program": "program",
    "manual": "manual",
    "eco": "eco",
    "holiday": "holiday",
    dps.MODE_FROST_PROTECTION: "antifrost",
}
_MODES = {preset: mode for mode, preset in PRESETS.items()}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value != int(value):
        return None
    return int(value)


def _pair(true: str, false: str) -> tuple[Callable[[Any], Any], Callable[[Any], Any]]:
    def encode(value: Any) -> str | None:
        return (true if value else false) if isinstance(value, bool) else None

    def decode(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.upper() in (true.upper(), false.upper()):
            return value.upper() == true.upper()
        raise ValueError(f"expected {true!r} or {false!r}")

    return encode, decode


def _decode_number(value: Any) -> float:
    number = _number(value)
    if number is None:
        raise ValueError("expected a number")
    return number


def _decode_integer(value: Any) -> int:
    integer = _integer(value)
    if integer is None:
        raise ValueError("expected an integer")
    return integer


def _decode_preset(value: Any) -> str:
    if not isinstance(value, str) or value not in _MODES:
        raise ValueError(f"expected one of {', '.join(_MODES)}")
    return _MODES[value]


def _encode_window(value: Any) -> bool | None:
    return {"open": True, "close": False}.get(value) if isinstance(value, str) else None


_ENCODE_SWITCH, _DECODE_SWITCH = _pair("heat", "off")
_ENCODE_RUNNING, _ = _pair("heat", "idle")
_ENCODE_ONOFF, _DECODE_ONOFF = _pair("ON", "OFF")
_ENCODE_LOCK, _DECODE_LOCK = _pair("LOCK", "UNLOCK")


@dataclass(frozen=True)
class Property:
    """One dialect property: its name, the model key behind it, and the two conversions.

    ``decode`` is ``None`` for a property that is never a command (a measure).
    """

    name: str
    key: str
    encode: Callable[[Any], Any]
    decode: Callable[[Any], Any] | None = None


# The single table of dialect property names (contract §3 and §4).
PROPERTIES: tuple[Property, ...] = (
    Property("local_temperature", "temperature", _number),
    Property("current_heating_setpoint", "setpoint", _number, _decode_number),
    Property("system_mode", "switch", _ENCODE_SWITCH, _DECODE_SWITCH),
    Property("preset", "mode", lambda mode: PRESETS.get(mode), _decode_preset),
    Property("running_state", "heating", _ENCODE_RUNNING),
    Property("window_open", "window_state", _encode_window),
    Property("window_detection", "window_detection", _ENCODE_ONOFF, _DECODE_ONOFF),
    Property("child_lock", "child_lock", _ENCODE_LOCK, _DECODE_LOCK),
    Property("local_temperature_calibration", "temperature_correction", _integer, _decode_integer),
    Property("holiday_temperature", "holiday_setpoint", _number, _decode_number),
    Property("eco_temperature", "eco_setpoint", _number, _decode_number),
    Property("antifrost_temperature", "frost_setpoint", _number, _decode_number),
    Property("power", "power", _number),
    Property("voltage", "voltage", _number),
    Property(
        "current",
        "current_ma",
        lambda ma: None if _number(ma) is None else round(ma / 1000, 3),
    ),
    Property("energy", "energy", _number),
)
FAULT_PROPERTY = "fault"
EXTENSION_PROPERTY = "tuya"

_BY_NAME = {prop.name: prop for prop in PROPERTIES}


@dataclass(frozen=True)
class Command:
    """One write asked through ``<name>/set``, in model terms: ``Command("setpoint", 19.5)``."""

    key: str
    value: Any
    property: str


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, tz=UTC).isoformat(timespec="seconds")


def encode_state(state: DeviceState, spec: DeviceSpec | None = None) -> dict[str, Any]:
    """The state object published on ``<prefix>/<friendly_name>`` (contract §3).

    A property that was not read is absent. The ``tuya`` extension carries the raw mode and, for
    each property, the channel it came from and when it was read.
    """
    payload: dict[str, Any] = {}
    sources: dict[str, dict[str, str]] = {}

    def put(name: str, key: str, value: Any) -> None:
        if value is None:
            return
        payload[name] = value
        held = state.values[key]
        sources[name] = {"channel": held.channel.value, "at": _iso(held.at)}

    for prop in PROPERTIES:
        if prop.key in state.values:
            put(prop.name, prop.key, prop.encode(state.values[prop.key].value))
    bitmap = _integer(state.get("fault"))
    if bitmap is not None and bitmap >= 0:
        put(FAULT_PROPERTY, "fault", (spec or DeviceSpec(state.device.product_id)).faults(bitmap))

    extension: dict[str, Any] = {"sources": sources}
    mode = state.get("mode")
    if isinstance(mode, str):
        extension["mode"] = mode
    payload[EXTENSION_PROPERTY] = extension
    return payload


def decode_command(payload: Any) -> list[Command]:
    """A ``/set`` payload to the writes it asks for. Raises :class:`DialectError`.

    The codec only translates: whether a write has a route, and whether its value is acceptable to
    the device, is decided by the router and the write gate.
    """
    if not isinstance(payload, Mapping) or not payload:
        raise DialectError("a command is a non-empty JSON object")
    commands: list[Command] = []
    for name, value in payload.items():
        prop = _BY_NAME.get(name)
        if prop is None:
            raise DialectError(f"unknown property {name!r}")
        if prop.decode is None:
            raise DialectError(f"property {name!r} is read-only")
        try:
            commands.append(Command(prop.key, prop.decode(value), name))
        except ValueError as err:
            raise DialectError(f"{name}: {err}") from err
    return commands


def property_of(key: str) -> str:
    """Dialect name of a model key. Raises ``KeyError`` when the key is not published."""
    for prop in PROPERTIES:
        if prop.key == key:
            return prop.name
    raise KeyError(key)


def encode_availability(online: bool | None) -> dict[str, str]:
    """Payload of ``<prefix>/<friendly_name>/availability``. Unknown counts as offline."""
    return {"state": "online" if online else "offline"}


_UNSAFE = re.compile(r"[#+/\x00-\x1f]+")


def friendly_name(name: str, fallback: str) -> str:
    """A device name usable as one MQTT topic level.

    The wildcard characters ``#`` and ``+`` and the level separator ``/`` cannot be published in a
    topic: they are removed, and spaces collapsed. ``fallback`` serves when nothing is left.
    """
    cleaned = " ".join(_UNSAFE.sub(" ", name).split())
    return cleaned or fallback


def friendly_names(devices: list[Device]) -> dict[str, str]:
    """``{dev_id: friendly name}``, unique: a name taken twice gets a numeric suffix.

    Devices are taken in order of device id, so the result does not depend on listing order.
    """
    names: dict[str, str] = {}
    taken: set[str] = {"bridge"}
    for device in sorted(devices, key=lambda d: d.dev_id):
        base = friendly_name(device.name, device.dev_id)
        name, suffix = base, 1
        while name in taken:
            suffix += 1
            name = f"{base} {suffix}"
        taken.add(name)
        names[device.dev_id] = name
    return names


def encode_descriptor(device: Device, name: str) -> dict[str, Any]:
    """One entry of ``<prefix>/bridge/devices`` (contract §2)."""
    return {
        "friendly_name": name,
        "type": "EndDevice",
        "supported": device.supported,
        "definition": {
            "vendor": DEFINITION_VENDOR,
            "model": DEFINITION_MODEL,
            "description": DEFINITION_DESCRIPTION,
        },
        "tuya": {
            "dev_id": device.dev_id,
            "product_id": device.product_id,
            "category": device.category,
            "gateway": device.gateway_id,
        },
    }
