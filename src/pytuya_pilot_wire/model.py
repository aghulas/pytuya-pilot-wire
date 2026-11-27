"""Device model: what a thermostat is, what was read from it, and through which channel.

The model keys are those of :mod:`pytuya_pilot_wire.dps` (``temperature``, ``setpoint``,
``mode``…). A value is kept with the channel it came from and the time it was read; a value that
was never read is absent, never defaulted.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from . import dps


class Channel(StrEnum):
    """The three ways to reach a thermostat behind a Tuya gateway."""

    SHARING = "sharing"
    LAN = "lan"
    DEVELOPER = "developer"


@dataclass(frozen=True)
class Device:
    """A device as the account lists it. ``node_id`` addresses a sub-device through its gateway."""

    dev_id: str
    name: str
    product_id: str
    category: str = ""
    gateway_id: str | None = None
    node_id: str | None = None
    online: bool | None = None

    @property
    def supported(self) -> bool:
        """Whether the product has a data-point table."""
        return self.product_id in dps.PRODUCTS


@dataclass(frozen=True)
class Bounds:
    """Bounds and step of a numeric setting, in the unit of the model (°C for a setpoint)."""

    minimum: float
    maximum: float
    step: float

    def check(self, value: Any) -> float:
        """The value as a float when it is within bounds and on the step, else ``ValueError``."""
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{value!r} is not a number")
        if not self.minimum <= value <= self.maximum:
            raise ValueError(
                f"{value:g} is outside the device's bounds ({self.minimum:g} to {self.maximum:g})"
            )
        steps = (value - self.minimum) / self.step
        if abs(steps - round(steps)) > 1e-6:
            raise ValueError(f"{value:g} is not on the device's step ({self.step:g})")
        return float(value)


def _values(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


@dataclass(frozen=True)
class DeviceSpec:
    """What a device declares about itself: writable codes, bounds, modes, fault names.

    Built from the instruction set the sharing API returns with each device. Bounds are the
    device's own, converted to the unit of the model with the divisor of the data-point table.
    """

    product_id: str
    writable: frozenset[str] = frozenset()
    bounds: Mapping[str, Bounds] = field(default_factory=dict)
    modes: tuple[str, ...] = ()
    fault_labels: tuple[str, ...] = ()

    @classmethod
    def from_instruction_set(
        cls,
        product_id: str,
        functions: list[Mapping[str, Any]],
        status: list[Mapping[str, Any]],
    ) -> DeviceSpec:
        """From the ``functions`` (writable) and ``status`` (readable) lists of a device.

        Each entry is ``{"code", "type", "values"}``, ``values`` being a JSON string or a mapping.
        Codes that are not in the product's table are ignored.
        """
        by_code = {p.code: p for p in dps.PRODUCTS.get(product_id, ()) if p.code is not None}
        writable: set[str] = set()
        bounds: dict[str, Bounds] = {}
        modes: tuple[str, ...] = ()
        fault_labels: tuple[str, ...] = ()
        for entry in functions:
            point = by_code.get(str(entry.get("code")))
            if point is None:
                continue
            writable.add(point.key)
            values = _values(entry.get("values"))
            if entry.get("type") == "Integer" and "min" in values and "max" in values:
                try:
                    bounds[point.key] = Bounds(
                        float(values["min"]) / point.divisor,
                        float(values["max"]) / point.divisor,
                        float(values.get("step") or 1) / point.divisor,
                    )
                except (TypeError, ValueError):
                    continue
            elif entry.get("type") == "Enum" and point.key == "mode":
                declared = values.get("range")
                if isinstance(declared, list):
                    modes = tuple(str(mode) for mode in declared)
        for entry in status:
            if entry.get("code") == "fault" and entry.get("type") == "Bitmap":
                labels = _values(entry.get("values")).get("label")
                if isinstance(labels, list):
                    fault_labels = tuple(str(label) for label in labels)
        return cls(product_id, frozenset(writable), bounds, modes, fault_labels)

    def faults(self, bitmap: int) -> list[str]:
        """Names of the faults set in a bitmap; a bit without a declared name is ``bit_<n>``."""
        names: list[str] = []
        for bit in range(max(bitmap.bit_length(), 0)):
            if bitmap >> bit & 1:
                names.append(
                    self.fault_labels[bit] if bit < len(self.fault_labels) else f"bit_{bit}"
                )
        return names


@dataclass(frozen=True)
class Reading:
    """One read of one device through one channel. ``values`` holds only what was returned."""

    dev_id: str
    channel: Channel
    at: float
    values: Mapping[str, Any]
    online: bool | None = None


@dataclass(frozen=True)
class Sourced:
    """A value with the channel it came from and the time (epoch seconds) it was read."""

    value: Any
    channel: Channel
    at: float


# Electrical measures exist only while heating: a reading that tells the heating state replaces
# them as a whole, so that a stale voltage or current never outlives the heating it belonged to.
_HEATING_BOUND = ("power", "voltage", "current_ma")


@dataclass
class DeviceState:
    """Latest known values of a device, merged from the readings of every channel."""

    device: Device
    values: dict[str, Sourced] = field(default_factory=dict)
    online: bool | None = None

    def apply(self, reading: Reading) -> set[str]:
        """Merge a reading; returns the keys whose value changed (``"online"`` included).

        A reading older than what is held for a key does not replace it.
        """
        if reading.dev_id != self.device.dev_id:
            raise ValueError("reading of another device")
        changed: set[str] = set()
        if reading.online is not None and reading.online != self.online:
            self.online = reading.online
            changed.add("online")
        incoming = dict(reading.values)
        if "heating" in incoming:
            for key in _HEATING_BOUND:
                held = self.values.get(key)
                if key not in incoming and held is not None and held.at <= reading.at:
                    del self.values[key]
                    changed.add(key)
        for key, value in incoming.items():
            held = self.values.get(key)
            if held is not None and held.at > reading.at:
                continue
            if held is None or held.value != value:
                changed.add(key)
            self.values[key] = Sourced(value, reading.channel, reading.at)
        return changed

    def get(self, key: str) -> Any:
        """The value held for a key, or ``None`` when it was never read."""
        held = self.values.get(key)
        return None if held is None else held.value

    def snapshot(self) -> dict[str, Any]:
        """Plain ``{key: value}`` view of what is held."""
        return {key: held.value for key, held in self.values.items()}
