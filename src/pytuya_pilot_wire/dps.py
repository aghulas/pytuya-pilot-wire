"""Data points of the supported products, and their decoding.

One table per verified product. The numbers and codes come from the vendor's own code-to-DP
mapping, checked against real readings (2026-11-15). A data point that is not in the table is
ignored, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DataPoint:
    """One data point: its LAN number, its Tuya code when it has one, and how to read it."""

    dp: int
    key: str
    code: str | None = None
    divisor: float = 1
    kind: str = "number"  # "number", "bool" or "text"
    note: str = ""


# Tuya "FP Thermostat" (pilot-wire thermostat), category ``wk``.
PRODUCT_FP_THERMOSTAT = "fu1bfwza"

_FP_THERMOSTAT = (
    DataPoint(1, "switch", "switch", kind="bool"),
    DataPoint(2, "mode", "mode", kind="text"),
    DataPoint(16, "temperature", "temp_current", divisor=10),
    DataPoint(17, "window_state", "window_state", kind="text"),
    DataPoint(19, "temperature_correction", "temp_correction"),
    DataPoint(20, "fault", "fault"),
    DataPoint(24, "holiday_setpoint", "holiday_temp_set", divisor=10),
    DataPoint(29, "window_detection", "window_check", kind="bool"),
    DataPoint(39, "child_lock", "child_lock", kind="bool"),
    DataPoint(50, "setpoint", "temp_set", divisor=10, note="setpoint of the manual mode"),
    DataPoint(101, "voltage", "cur_voltage", divisor=10, note="only while heating"),
    DataPoint(102, "current_ma", "cur_current", note="only while heating"),
    DataPoint(107, "energy", "energy_all", divisor=10, note="cumulative, kWh"),
    DataPoint(116, "frost_setpoint", "hors_temp_set", divisor=10),
    DataPoint(117, "eco_setpoint", "eco_temp_set", divisor=10),
    DataPoint(120, "night_mode", "night_btn", kind="bool"),
    DataPoint(121, "night_start", "night_time_start", note="minutes after midnight"),
    DataPoint(122, "night_end", "night_time_end", note="minutes after midnight"),
    DataPoint(124, "work_state", "work_state", kind="text"),
)

PRODUCTS: dict[str, tuple[DataPoint, ...]] = {PRODUCT_FP_THERMOSTAT: _FP_THERMOSTAT}

# Modes the product declares. A device may report another value (a frost-protection mode was
# seen on real hardware): it is passed through as read and is never a valid write.
DECLARED_MODES = ("eco", "holiday", "program", "manual")

# The mode value reported outside the declared list: frost protection. In that mode the device
# stores a setpoint write and does not apply it.
MODE_FROST_PROTECTION = "hors"

# The only mode in which a written room setpoint is applied and kept. In ``program`` mode the
# device replaces it with the setpoint of the running schedule slot (seen on real hardware), in
# frost protection it stores it without applying it, and in ``eco`` and ``holiday`` nothing
# shows that it would be kept.
MODE_MANUAL = "manual"

# The modes a write may set. ``manual`` and ``program`` were written and read back on real
# hardware; ``eco`` and ``holiday`` are declared but stay closed: the device then follows a
# setpoint this library has no proven route to write (eco) or a holiday period it does not set.
WRITABLE_MODES = ("manual", "program")

# Load state while the radiator is heating.
WORK_STATE_HEATING = "opened"

# Keys computed from a reading rather than read from one data point.
DERIVED_KEYS = ("heating", "power")


def table(product_id: str) -> tuple[DataPoint, ...]:
    """Data-point table of a supported product. Raises ``KeyError`` for an unknown product."""
    return PRODUCTS[product_id]


def _value(point: DataPoint, raw: Any) -> Any:
    if point.kind == "bool":
        return raw if isinstance(raw, bool) else None
    if point.kind == "text":
        return raw if isinstance(raw, str) else None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return raw / point.divisor if point.divisor != 1 else raw


def _derive(decoded: dict[str, Any]) -> dict[str, Any]:
    current = decoded.get("current_ma")
    if current is not None:
        decoded["heating"] = current > 0
    elif "work_state" in decoded:
        decoded["heating"] = decoded["work_state"] == WORK_STATE_HEATING

    if decoded.get("heating") is False:
        decoded["power"] = 0.0
    elif current is not None and "voltage" in decoded:
        decoded["power"] = round(decoded["voltage"] * current / 1000, 1)
    return decoded


def _decode(points: dict[str, DataPoint], raw_values: dict[str, Any]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for name, point in points.items():
        raw = raw_values.get(name)
        if raw is None:
            continue
        value = _value(point, raw)
        if value is not None:
            decoded[point.key] = value
    return _derive(decoded)


def decode_lan(product_id: str, dps: dict[str, Any]) -> dict[str, Any]:
    """A LAN reading ``{"16": 195, ...}`` to ``{"temperature": 19.5, ...}``.

    Only the data points present in the reading appear in the result: the gateway does not return
    all of them each time, and an absent value is unknown, not zero. A value of the wrong type is
    dropped. ``heating`` and ``power`` are derived when their inputs are present.
    """
    return _decode({str(point.dp): point for point in table(product_id)}, dps)


def decode_codes(product_id: str, values: dict[str, Any]) -> dict[str, Any]:
    """A cloud reading keyed by Tuya code, ``{"temp_current": 195, ...}``, decoded the same way.

    Serves the sharing API (device status) and the developer API (shadow properties): both report
    the raw value of the data point under its code. A code that is not in the table is ignored.
    """
    return _decode({p.code: p for p in table(product_id) if p.code is not None}, values)


def code_of(product_id: str, key: str) -> str:
    """Tuya code of a model key, to address a write. Raises ``KeyError`` when there is none."""
    for point in table(product_id):
        if point.key == key and point.code is not None:
            return point.code
    raise KeyError(key)


def point_of(product_id: str, key: str) -> DataPoint:
    """Data point of a model key. Raises ``KeyError`` when the key is not in the table."""
    for point in table(product_id):
        if point.key == key:
            return point
    raise KeyError(key)
