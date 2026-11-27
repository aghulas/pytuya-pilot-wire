"""Router: for each operation, the channel proven for it on real hardware, or a refusal.

A route is **proven** when a supervised write on real hardware was read back from an independent
source (for a read: when the values were checked against the vendor app). A **candidate** is a
write the device declares and nobody tried: it stays closed. It opens only for the one device
designated for its supervised trial, and becomes proven in a change of its own that records the
trial. There is no fallback to a channel that merely accepts the call.

The table mirrors ``ha-tuya-pilot-wire/docs/architecture.md`` ("Routing") and §4 of the MQTT
contract.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from . import dps
from .errors import RouteRefused
from .model import Channel, Device


class Operation(StrEnum):
    """What can be asked of a thermostat. A write is named after the model key it changes."""

    READ_STATE = "read_state"
    READ_POWER_ENERGY = "read_power_energy"
    READ_SETTINGS = "read_settings"
    READ_SCHEDULE = "read_schedule"
    SWITCH = "switch"
    MODE = "mode"
    SETPOINT = "setpoint"
    HOLIDAY_SETPOINT = "holiday_setpoint"
    TEMPERATURE_CORRECTION = "temperature_correction"
    WINDOW_DETECTION = "window_detection"
    CHILD_LOCK = "child_lock"
    ECO_SETPOINT = "eco_setpoint"
    FROST_SETPOINT = "frost_setpoint"
    NIGHT_MODE = "night_mode"
    SCHEDULE = "schedule"

    @property
    def is_write(self) -> bool:
        return not self.value.startswith("read_")


class RouteState(StrEnum):
    PROVEN = "proven"
    CANDIDATE = "candidate"
    NONE = "none"


@dataclass(frozen=True)
class Route:
    """The route of one operation: channels in order of preference, state, and the evidence."""

    channels: tuple[Channel, ...]
    state: RouteState
    record: str


_NO_ROUTE = Route((), RouteState.NONE, "no write tried on any channel")
_CANDIDATE = Route(
    (Channel.SHARING,), RouteState.CANDIDATE, "declared by the device, never written"
)
_PROVEN_WRITE = "written on one thermostat and read back, 2026-10-31"

ROUTES: Mapping[str, Mapping[Operation, Route]] = {
    dps.PRODUCT_FP_THERMOSTAT: {
        Operation.READ_STATE: Route(
            (Channel.SHARING,), RouteState.PROVEN, "read on six thermostats, 2026-10-31"
        ),
        Operation.READ_POWER_ENERGY: Route(
            (Channel.LAN,), RouteState.PROVEN, "read on six thermostats, 2026-11-15"
        ),
        Operation.READ_SETTINGS: Route(
            (Channel.DEVELOPER, Channel.LAN),
            RouteState.PROVEN,
            "read on six thermostats, 2026-11-15",
        ),
        Operation.READ_SCHEDULE: Route(
            (Channel.DEVELOPER,), RouteState.PROVEN, "read on six thermostats, 2026-11-03"
        ),
        Operation.SWITCH: Route((Channel.SHARING,), RouteState.PROVEN, _PROVEN_WRITE),
        Operation.MODE: Route((Channel.SHARING,), RouteState.PROVEN, _PROVEN_WRITE),
        Operation.SETPOINT: Route((Channel.SHARING,), RouteState.PROVEN, _PROVEN_WRITE),
        Operation.HOLIDAY_SETPOINT: _CANDIDATE,
        Operation.TEMPERATURE_CORRECTION: _CANDIDATE,
        Operation.WINDOW_DETECTION: _CANDIDATE,
        Operation.CHILD_LOCK: _CANDIDATE,
        Operation.ECO_SETPOINT: _NO_ROUTE,
        Operation.FROST_SETPOINT: _NO_ROUTE,
        Operation.NIGHT_MODE: _NO_ROUTE,
        Operation.SCHEDULE: Route(
            (Channel.DEVELOPER,),
            RouteState.PROVEN,
            "written read-modify-write on one thermostat and restored, 2026-11-14",
        ),
    }
}


# The model keys each read operation is proven to deliver. A channel's reading is trusted for
# the keys of the operations routed to it, and for nothing else it happens to return.
READ_KEYS: Mapping[Operation, frozenset[str]] = {
    Operation.READ_STATE: frozenset(
        {
            "switch",
            "mode",
            "temperature",
            "setpoint",
            "window_state",
            "window_detection",
            "child_lock",
            "temperature_correction",
            "holiday_setpoint",
            "fault",
        }
    ),
    Operation.READ_POWER_ENERGY: frozenset(
        {"voltage", "current_ma", "energy", "work_state", "heating", "power"}
    ),
    Operation.READ_SETTINGS: frozenset(
        {"frost_setpoint", "eco_setpoint", "night_mode", "night_start", "night_end"}
    ),
}


class Router:
    """Routes an operation to its proven channel among those available, or refuses.

    ``trials`` designates, per candidate operation, the single device on which its supervised
    trial is run: the candidate is open for that device only. It is empty in normal service.
    """

    def __init__(
        self,
        available: Iterable[Channel],
        trials: Mapping[Operation, str] | None = None,
    ) -> None:
        self.available = frozenset(available)
        self.trials = dict(trials or {})

    def route(self, device: Device, operation: Operation) -> Channel:
        """The channel to use, or :class:`RouteRefused`. Nothing else decides where a call goes."""
        routes = ROUTES.get(device.product_id)
        if routes is None:
            raise RouteRefused(f"product {device.product_id!r} is not supported")
        route = routes.get(operation, _NO_ROUTE)
        if route.state is RouteState.NONE:
            raise RouteRefused(f"{operation.value}: no route ({route.record})")
        if route.state is RouteState.CANDIDATE and self.trials.get(operation) != device.dev_id:
            raise RouteRefused(
                f"{operation.value}: candidate route, closed until its supervised trial"
                f" ({route.record})"
            )
        for channel in route.channels:
            if channel in self.available:
                return channel
        needed = " or ".join(channel.value for channel in route.channels)
        raise RouteRefused(f"{operation.value}: the {needed} channel is not available")

    def read_keys(self, device: Device, channel: Channel) -> frozenset[str]:
        """The model keys to take from a reading of ``device`` made through ``channel``."""
        keys: set[str] = set()
        for operation, proven in READ_KEYS.items():
            try:
                routed = self.route(device, operation)
            except RouteRefused:
                continue
            if routed is channel:
                keys |= proven
        return frozenset(keys)

    def describe(self, product_id: str) -> dict[str, dict[str, object]]:
        """The routing table of a product, for diagnostics (``bridge/info``)."""
        return {
            operation.value: {
                "state": route.state.value,
                "channels": [channel.value for channel in route.channels],
                "available": any(channel in self.available for channel in route.channels),
                "record": route.record,
            }
            for operation, route in ROUTES.get(product_id, {}).items()
        }


def operation_of(key: str) -> Operation:
    """The write operation of a model key. :class:`RouteRefused` when there is none."""
    try:
        operation = Operation(key)
    except ValueError:
        operation = None
    if operation is None or not operation.is_write:
        raise RouteRefused(f"{key}: no route")
    return operation
