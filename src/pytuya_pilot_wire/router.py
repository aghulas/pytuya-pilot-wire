"""Router: for each operation, the channel proven for it on real hardware, or a refusal.

A route is **proven** when a supervised write on real hardware was read back from an independent
source (for a read: when the values were checked against the vendor app). A **candidate** is a
write the device declares and nobody tried: it stays closed. It opens only for the one device
designated for its supervised trial, and becomes proven in a change of its own that records the
trial. There is no fallback to a channel that merely accepts the call.

The mode is routed per value (:data:`MODE_ROUTES`): ``manual``, ``program``, ``holiday`` and
``eco`` are proven; the frost-protection value has no route (the sharing API refused it). A
trial is designated by **write name** (:func:`write_name`): the operation's value
(``"child_lock"``), or ``"mode:<raw mode>"`` (``"mode:holiday"``) for one mode value.

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


def _proven_by(trial: str) -> Route:
    return Route(
        (Channel.SHARING,),
        RouteState.PROVEN,
        f"written through the bridge on one thermostat and read back, 2026-11-28 (trial {trial})",
    )


_NOT_DECLARED = Route(
    (),
    RouteState.NONE,
    "the sharing API does not declare it writable (developer API shadow property only)",
)

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
        Operation.HOLIDAY_SETPOINT: _proven_by("5b"),
        Operation.TEMPERATURE_CORRECTION: _proven_by("5a"),
        Operation.WINDOW_DETECTION: _proven_by("4b"),
        Operation.CHILD_LOCK: _proven_by("4a"),
        Operation.ECO_SETPOINT: _NOT_DECLARED,
        Operation.FROST_SETPOINT: _NOT_DECLARED,
        Operation.NIGHT_MODE: _NO_ROUTE,
        Operation.SCHEDULE: Route(
            (Channel.DEVELOPER,),
            RouteState.PROVEN,
            "written read-modify-write on one thermostat and restored, 2026-11-14",
        ),
    }
}


_PROVEN_MODE = Route(
    (Channel.SHARING,),
    RouteState.PROVEN,
    "written on one thermostat (2026-10-31), then through the bridge on two (2026-11-28)",
)

# Routes of the mode, per raw value. A value absent from here has no route.
MODE_ROUTES: Mapping[str, Mapping[str, Route]] = {
    dps.PRODUCT_FP_THERMOSTAT: {
        "manual": _PROVEN_MODE,
        "program": _PROVEN_MODE,
        "eco": Route(
            (Channel.SHARING,),
            RouteState.PROVEN,
            "written through the bridge on one thermostat and read back, 2026-11-28 (trial 3c)",
        ),
        "holiday": Route(
            (Channel.SHARING,),
            RouteState.PROVEN,
            "written through the bridge on one thermostat and read back, 2026-11-28 (trial 3a)",
        ),
        dps.MODE_FROST_PROTECTION: Route(
            (),
            RouteState.NONE,
            "reported by the device outside its declared modes; the sharing API refused it,"
            " 2026-11-28 (trial 3b)",
        ),
    }
}


def write_name(operation: Operation, mode: str | None = None) -> str:
    """Name of a write for :class:`Router` trials: ``"child_lock"``, ``"mode:holiday"``."""
    return f"{operation.value}:{mode}" if operation is Operation.MODE and mode else operation.value


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

    ``trials`` designates, per candidate write (by :func:`write_name`; an :class:`Operation` is
    its own name), the single device on which its supervised trial is run: the candidate is open
    for that device only. It is empty in normal service.
    """

    def __init__(
        self,
        available: Iterable[Channel],
        trials: Mapping[str, str] | None = None,
    ) -> None:
        self.available = frozenset(available)
        self.trials: dict[str, str] = {str(name): dev for name, dev in (trials or {}).items()}

    def state(self, product_id: str, operation: Operation, mode: str | None = None) -> RouteState:
        """State of a write's route, before any trial designation."""
        return self._route_of(product_id, operation, mode).state

    @staticmethod
    def _route_of(product_id: str, operation: Operation, mode: str | None) -> Route:
        if operation is Operation.MODE and mode is not None:
            return MODE_ROUTES.get(product_id, {}).get(mode, _NO_ROUTE)
        return ROUTES.get(product_id, {}).get(operation, _NO_ROUTE)

    def route(self, device: Device, operation: Operation, mode: str | None = None) -> Channel:
        """The channel to use, or :class:`RouteRefused`. Nothing else decides where a call goes.

        For a write of the mode, ``mode`` is the raw value to write: each value has its route.
        """
        if device.product_id not in ROUTES:
            raise RouteRefused(f"product {device.product_id!r} is not supported")
        route = self._route_of(device.product_id, operation, mode)
        name = write_name(operation, mode)
        if route.state is RouteState.NONE:
            raise RouteRefused(f"{name}: no route ({route.record})")
        if route.state is RouteState.CANDIDATE and self.trials.get(name) != device.dev_id:
            raise RouteRefused(
                f"{name}: candidate route, closed until its supervised trial ({route.record})"
            )
        for channel in route.channels:
            if channel in self.available:
                return channel
        needed = " or ".join(channel.value for channel in route.channels)
        raise RouteRefused(f"{name}: the {needed} channel is not available")

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

        def entry(route: Route) -> dict[str, object]:
            return {
                "state": route.state.value,
                "channels": [channel.value for channel in route.channels],
                "available": any(channel in self.available for channel in route.channels),
                "record": route.record,
            }

        table = {op.value: entry(route) for op, route in ROUTES.get(product_id, {}).items()}
        for mode, route in MODE_ROUTES.get(product_id, {}).items():
            table[write_name(Operation.MODE, mode)] = entry(route)
        return table


def operation_of(key: str) -> Operation:
    """The write operation of a model key. :class:`RouteRefused` when there is none."""
    try:
        operation = Operation(key)
    except ValueError:
        operation = None
    if operation is None or not operation.is_write:
        raise RouteRefused(f"{key}: no route")
    return operation
