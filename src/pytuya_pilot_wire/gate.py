"""Write gate: the only way a write leaves the library.

A write goes through, in this order:

1. the gate is **enabled** (it is not by default);
2. the product is **verified** for writes on real hardware;
3. the **router** gives a proven route (or the device is the one designated for a candidate's
   supervised trial);
4. the device **declares** the setting writable, and the value passes the device's **own bounds
   and step** (and a ceiling for setpoints), or is a mode the device declares (the
   frost-protection value, which it reports outside that list, once its own route is open), and
   the router opens that mode value (each mode value has its own route);
5. a **fresh read** shows the device online, and for a room setpoint, in ``manual`` mode: in
   ``program`` mode the running schedule slot overwrites it, in frost protection the device
   stores it without applying it, and in ``eco`` or ``holiday`` nothing shows it would be kept;
6. the command is sent **once**, never retried;
7. the device is **read back** until it reports the value or a timeout passes.

Nothing is assumed: the result carries what was read back, and ``confirmed`` says whether the
device took the value. The caller updates its state from that reading, never from the request.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import dps, schedule
from .errors import RouteRefused, WriteRefused
from .model import Channel, Device, DeviceSpec, Reading
from .router import Operation, Router, operation_of

# Products on which the proven writes were made.
VERIFIED_PRODUCTS = frozenset({dps.PRODUCT_FP_THERMOSTAT})

# Ceiling for a room setpoint, narrower than the device's own 40 °C on purpose (same as the
# schedule codec).
SETPOINT_CEILING = schedule.MAX_TEMPERATURE
_CEILED = frozenset(
    {
        Operation.SETPOINT,
        Operation.HOLIDAY_SETPOINT,
        Operation.ECO_SETPOINT,
        Operation.FROST_SETPOINT,
    }
)
_BOOLEAN = frozenset({Operation.SWITCH, Operation.WINDOW_DETECTION, Operation.CHILD_LOCK})
_NUMERIC = frozenset(
    {
        Operation.SETPOINT,
        Operation.HOLIDAY_SETPOINT,
        Operation.TEMPERATURE_CORRECTION,
        Operation.ECO_SETPOINT,
        Operation.FROST_SETPOINT,
    }
)

CONFIRM_TIMEOUT = 20.0
CONFIRM_INTERVAL = 2.0


def setpoint_mode_refusal(mode: Any) -> str:
    """Why a room setpoint is refused in ``mode`` (any mode but ``manual``)."""
    if mode == dps.MODE_FROST_PROTECTION:
        why = "it stores a setpoint and does not apply it"
    elif mode == "program":
        why = "the running schedule slot overwrites a written setpoint"
    else:
        why = "nothing shows that a written setpoint is kept"
    return (
        f"setpoint: the device is in {mode!r} mode, where {why}; a setpoint is written only in"
        f" {dps.MODE_MANUAL!r} mode"
    )


class SharingWriter(Protocol):
    """What the gate needs from the sharing channel."""

    def read(self, dev_ids: list[str]) -> list[Reading]: ...

    def send(self, dev_id: str, commands: list[dict[str, Any]]) -> None: ...


class ScheduleWriter(Protocol):
    """What the gate needs from the developer channel."""

    def schedule_parts(self, dev_id: str) -> dict[str, str]: ...

    def issue_schedule_parts(self, dev_id: str, parts: Mapping[str, str]) -> None: ...


@dataclass(frozen=True)
class PlannedWrite:
    """A write that passed every check made before talking to the device."""

    device: Device
    operation: Operation
    channel: Channel
    value: Any
    code: str
    raw: Any


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a write. ``reading`` is the last read-back, whatever it shows."""

    dev_id: str
    operation: Operation
    channel: Channel
    requested: Any
    confirmed: bool
    reading: Reading | None
    detail: str = ""


@dataclass(frozen=True)
class ScheduleResult:
    """Outcome of a schedule write. ``week`` is the schedule read back after it."""

    dev_id: str
    days: tuple[str, ...]
    written: tuple[str, ...]
    confirmed: bool
    week: dict[str, list[schedule.Transition]] = field(default_factory=dict)
    detail: str = ""


class WriteGate:
    """Validates, sends once, reads back."""

    def __init__(
        self,
        router: Router,
        *,
        sharing: SharingWriter | None = None,
        developer: ScheduleWriter | None = None,
        enabled: bool = False,
        verified_products: frozenset[str] = VERIFIED_PRODUCTS,
        setpoint_ceiling: float = SETPOINT_CEILING,
        confirm_timeout: float = CONFIRM_TIMEOUT,
        confirm_interval: float = CONFIRM_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.router = router
        self.enabled = enabled
        self._sharing = sharing
        self._developer = developer
        self._verified = verified_products
        self._ceiling = setpoint_ceiling
        self._timeout = confirm_timeout
        self._interval = confirm_interval
        self._sleep = sleep
        self._monotonic = monotonic

    def replace(
        self,
        *,
        sharing: SharingWriter | None = None,
        developer: ScheduleWriter | None = None,
    ) -> None:
        """Swap in a rebuilt channel client (new session or credentials)."""
        if sharing is not None:
            self._sharing = sharing
        if developer is not None:
            self._developer = developer

    # -- checks made before any I/O --------------------------------------------------------

    def _admit(self, device: Device, operation: Operation) -> Channel:
        if not self.enabled:
            raise WriteRefused("writes are disabled")
        if device.product_id not in self._verified:
            raise WriteRefused(f"product {device.product_id!r} is not verified for writes")
        return self.router.route(device, operation)

    def _admit_mode(self, device: Device, spec: DeviceSpec | None, value: Any) -> Channel:
        """The mode value is one the device declares (or frost protection), and its route opens."""
        declared = () if spec is None else spec.modes
        if not isinstance(value, str) or (
            value not in declared and value != dps.MODE_FROST_PROTECTION
        ):
            raise WriteRefused(
                f"mode {value!r} is not one the device declares ({', '.join(declared)})"
            )
        return self.router.route(device, Operation.MODE, value)

    def plan(self, device: Device, spec: DeviceSpec | None, key: str, value: Any) -> PlannedWrite:
        """Every check that needs no I/O. Raises :class:`WriteRefused` or :class:`RouteRefused`."""
        operation = operation_of(key)
        if operation is Operation.SCHEDULE:
            raise WriteRefused("the weekly schedule is written with write_schedule")
        channel = self._admit(device, operation)
        if operation is Operation.MODE:
            channel = self._admit_mode(device, spec, value)
        if channel is not Channel.SHARING or self._sharing is None:
            raise RouteRefused(f"{operation.value}: the {channel.value} channel cannot write it")
        if spec is None or key not in spec.writable:
            raise WriteRefused(f"{key}: the device does not declare it writable")
        point = dps.point_of(device.product_id, key)
        assert point.code is not None  # a key the device declares has a code
        raw: Any
        if operation in _BOOLEAN:
            if not isinstance(value, bool):
                raise WriteRefused(f"{key}: expected true or false")
            raw = value
        elif operation is Operation.MODE:
            raw = value  # checked by _admit_mode
        elif operation in _NUMERIC:
            bounds = spec.bounds.get(key)
            if bounds is None:
                raise WriteRefused(f"{key}: the device declares no bounds for it")
            try:
                checked = bounds.check(value)
            except ValueError as err:
                raise WriteRefused(f"{key}: {err}") from err
            if operation in _CEILED and checked > self._ceiling:
                raise WriteRefused(f"{key}: {checked:g} is above the ceiling ({self._ceiling:g})")
            raw = round(checked * point.divisor)
            value = checked if point.divisor != 1 else int(checked)
        else:  # pragma: no cover - every routable write is in one of the groups above
            raise WriteRefused(f"{key}: no validation rule")
        return PlannedWrite(device, operation, channel, value, point.code, raw)

    # -- write through the sharing API -----------------------------------------------------

    def _read_one(self, dev_id: str) -> Reading | None:
        assert self._sharing is not None
        return next((r for r in self._sharing.read([dev_id]) if r.dev_id == dev_id), None)

    def write(self, device: Device, spec: DeviceSpec | None, key: str, value: Any) -> WriteResult:
        """Validate, send once, read back. Refusals raise; an unconfirmed write is a result."""
        return self.execute(self.plan(device, spec, key, value))

    def execute(self, planned: PlannedWrite) -> WriteResult:
        """Send a planned write. The fresh-read checks are made here, just before sending."""
        if not self.enabled:
            raise WriteRefused("writes are disabled")
        assert self._sharing is not None
        dev_id, key = planned.device.dev_id, planned.operation.value
        before = self._read_one(dev_id)
        if before is None:
            raise WriteRefused("the device could not be read before the write")
        if before.online is not True:
            raise WriteRefused("the device is offline")
        if planned.operation is Operation.SETPOINT:
            mode = before.values.get("mode")
            if mode is None:
                raise WriteRefused("setpoint: the mode of the device could not be read")
            if mode != dps.MODE_MANUAL:
                raise WriteRefused(setpoint_mode_refusal(mode))
        self._sharing.send(dev_id, [{"code": planned.code, "value": planned.raw}])
        deadline = self._monotonic() + self._timeout
        reading: Reading | None = None
        while True:
            self._sleep(self._interval)
            reading = self._read_one(dev_id) or reading
            if reading is not None and reading.values.get(key) == planned.value:
                return WriteResult(
                    dev_id, planned.operation, planned.channel, planned.value, True, reading
                )
            if self._monotonic() >= deadline:
                break
        seen = None if reading is None else reading.values.get(key)
        return WriteResult(
            dev_id,
            planned.operation,
            planned.channel,
            planned.value,
            False,
            reading,
            f"the device still reports {seen!r} after {self._timeout:g} s",
        )

    # -- weekly schedule through the developer API -----------------------------------------

    def write_schedule(
        self, device: Device, days: Mapping[str, list[schedule.Transition]]
    ) -> ScheduleResult:
        """Write the given days, read-modify-write, and read the schedule back.

        Each day is described in full; days not given are left untouched. Each part keeps the
        header read from the device, and only the parts that differ are written.
        """
        channel = self._admit(device, Operation.SCHEDULE)
        if channel is not Channel.DEVELOPER or self._developer is None:
            raise RouteRefused(f"schedule: the {channel.value} channel cannot write it")
        if not isinstance(days, Mapping) or not days:
            raise WriteRefused("schedule: give at least one day")
        numbers: dict[str, int] = {}
        for name in days:
            if name not in schedule.DAYS:
                raise WriteRefused(f"schedule: unknown day {name!r}")
            numbers[name] = schedule.DAYS.index(name) + 1
        try:
            for name, number in numbers.items():
                schedule.encode_day(number, days[name])
        except ValueError as err:
            raise WriteRefused(f"schedule: {err}") from err

        dev_id = device.dev_id
        current = self._developer.schedule_parts(dev_id)
        wanted: dict[str, str] = {}
        for name, number in numbers.items():
            codes = [schedule.part_code(number, part) for part in range(1, 5)]
            if any(code not in current for code in codes):
                raise WriteRefused(
                    f"schedule: {name} could not be read in full, so its headers are unknown"
                )
            try:
                wanted.update(schedule.encode_day(number, days[name], current))
            except ValueError as err:
                raise WriteRefused(f"schedule: {err}") from err
        changed = {code: value for code, value in wanted.items() if current[code] != value}
        ordered = tuple(sorted(numbers, key=schedule.DAYS.index))
        if not changed:
            return ScheduleResult(
                dev_id, ordered, (), True, schedule.decode_week(current), "already as asked"
            )
        self._developer.issue_schedule_parts(dev_id, changed)
        deadline = self._monotonic() + self._timeout
        while True:
            self._sleep(self._interval)
            after = self._developer.schedule_parts(dev_id)
            if all(after.get(code) == value for code, value in changed.items()):
                return ScheduleResult(
                    dev_id, ordered, tuple(sorted(changed)), True, schedule.decode_week(after)
                )
            if self._monotonic() >= deadline:
                return ScheduleResult(
                    dev_id,
                    ordered,
                    tuple(sorted(changed)),
                    False,
                    schedule.decode_week(after),
                    f"the schedule read back differs after {self._timeout:g} s",
                )
