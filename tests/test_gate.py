"""Write gate, against fake channels. Synthetic data only."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from pytuya_pilot_wire import dps, schedule
from pytuya_pilot_wire.errors import ChannelError, RouteRefused, WriteRefused
from pytuya_pilot_wire.gate import WriteGate
from pytuya_pilot_wire.model import Bounds, Channel, Device, DeviceSpec, Reading
from pytuya_pilot_wire.router import Operation, Router

T1 = Device("devsynthetic0001", "Room 1", dps.PRODUCT_FP_THERMOSTAT)
SPEC = DeviceSpec(
    dps.PRODUCT_FP_THERMOSTAT,
    writable=frozenset(
        {
            "switch",
            "mode",
            "setpoint",
            "holiday_setpoint",
            "temperature_correction",
            "window_detection",
            "child_lock",
        }
    ),
    bounds={
        "setpoint": Bounds(5, 40, 0.5),
        "holiday_setpoint": Bounds(5, 40, 0.5),
        "temperature_correction": Bounds(-9, 9, 1),
    },
    modes=("eco", "holiday", "program", "manual"),
)


class FakeSharing:
    """A device that applies what it is sent after ``lag`` reads, or never (``deaf``)."""

    def __init__(self, lag: int = 0, deaf: bool = False, **values: Any) -> None:
        self.values: dict[str, Any] = {"mode": "manual", "setpoint": 19.0, "switch": True, **values}
        self.online: bool | None = True
        self.sent: list[tuple[str, list[dict[str, Any]]]] = []
        self.reads = 0
        self.lag, self.deaf = lag, deaf
        self.pending: dict[str, Any] = {}
        self.missing = False
        self.fail_send = False

    def read(self, dev_ids: list[str]) -> list[Reading]:
        self.reads += 1
        if self.pending and not self.deaf:
            if self.lag <= 0:
                self.values.update(self.pending)
                self.pending = {}
            self.lag -= 1
        if self.missing:
            return []
        return [
            Reading(dev_ids[0], Channel.SHARING, float(self.reads), dict(self.values), self.online)
        ]

    def send(self, dev_id: str, commands: list[dict[str, Any]]) -> None:
        if self.fail_send:
            raise ChannelError("command refused by Tuya")
        self.sent.append((dev_id, commands))
        for command in commands:
            point = next(p for p in dps.table(T1.product_id) if p.code == command["code"])
            raw = command["value"]
            self.pending[point.key] = raw / point.divisor if point.divisor != 1 else raw


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def gate(
    sharing: Any = None, developer: Any = None, router: Router | None = None, **kwargs: Any
) -> tuple[WriteGate, Clock]:
    clock = Clock()
    made = WriteGate(
        router or Router(Channel),
        sharing=sharing,
        developer=developer,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **{"enabled": True, **kwargs},
    )
    return made, clock


def test_disabled_by_default() -> None:
    fake = FakeSharing()
    closed = WriteGate(Router(Channel), sharing=fake)
    with pytest.raises(WriteRefused, match="disabled"):
        closed.write(T1, SPEC, "setpoint", 20.0)
    with pytest.raises(WriteRefused, match="disabled"):
        closed.write_schedule(T1, {"monday": [{"time": "00:00", "temperature": 16.0}]})
    planned = gate(fake)[0].plan(T1, SPEC, "setpoint", 20.0)
    with pytest.raises(WriteRefused, match="disabled"):
        closed.execute(planned)
    assert fake.sent == [] and fake.reads == 0


def test_setpoint_is_sent_once_and_read_back() -> None:
    fake = FakeSharing(lag=1)
    made, clock = gate(fake)
    result = made.write(T1, SPEC, "setpoint", 20.5)
    assert fake.sent == [(T1.dev_id, [{"code": "temp_set", "value": 205}])]
    assert result.confirmed and result.requested == 20.5 and result.channel is Channel.SHARING
    assert result.operation is Operation.SETPOINT and result.detail == ""
    assert result.reading is not None and result.reading.values["setpoint"] == 20.5
    assert clock.sleeps == [2.0, 2.0] and fake.reads == 3


@pytest.mark.parametrize(
    ("key", "value", "command"),
    [
        ("switch", False, {"code": "switch", "value": False}),
        ("mode", "program", {"code": "mode", "value": "program"}),
        ("setpoint", 5, {"code": "temp_set", "value": 50}),
    ],
)
def test_proven_writes(key: str, value: Any, command: dict[str, Any]) -> None:
    fake = FakeSharing()
    result = gate(fake)[0].write(T1, SPEC, key, value)
    assert fake.sent == [(T1.dev_id, [command])] and result.confirmed


def test_unconfirmed_write_is_reported_not_assumed() -> None:
    fake = FakeSharing(deaf=True)
    made, clock = gate(fake, confirm_timeout=6, confirm_interval=2)
    result = made.write(T1, SPEC, "setpoint", 21.0)
    assert not result.confirmed and len(fake.sent) == 1
    assert result.reading is not None and result.reading.values["setpoint"] == 19.0
    assert result.detail == "the device still reports 19.0 after 6 s"
    assert clock.sleeps == [2, 2, 2]


def test_unconfirmed_when_the_device_stops_answering() -> None:
    fake = FakeSharing(deaf=True)
    made, _ = gate(fake, confirm_timeout=2)
    planned = made.plan(T1, SPEC, "setpoint", 21.0)
    original = fake.read

    def read(dev_ids: list[str]) -> list[Reading]:
        fake.missing = fake.reads >= 1
        return original(dev_ids)

    fake.read = read  # type: ignore[method-assign]
    result = made.execute(planned)
    assert not result.confirmed and result.reading is None
    assert result.detail == "the device still reports None after 2 s"


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("setpoint", 4.5, "outside the device's bounds"),
        ("setpoint", 19.3, "not on the device's step"),
        ("setpoint", 30.5, "above the ceiling"),
        ("setpoint", "20", "not a number"),
        ("setpoint", True, "not a number"),
        ("mode", "comfort", "not one the device declares"),
        ("mode", 3, "not one the device declares"),
        ("switch", "on", "expected true or false"),
        ("schedule", {}, "write_schedule"),
    ],
)
def test_value_refusals(key: str, value: Any, message: str) -> None:
    fake = FakeSharing()
    with pytest.raises(WriteRefused, match=message):
        gate(fake)[0].write(T1, SPEC, key, value)
    assert fake.sent == [] and fake.reads == 0


@pytest.mark.parametrize(
    "key", ["holiday_setpoint", "temperature_correction", "window_detection", "child_lock"]
)
def test_candidates_stay_closed(key: str) -> None:
    fake = FakeSharing()
    with pytest.raises(RouteRefused, match="candidate"):
        gate(fake)[0].write(T1, SPEC, key, True)
    assert fake.sent == []


@pytest.mark.parametrize("mode", ["eco", "holiday", dps.MODE_FROST_PROTECTION])
def test_candidate_modes_stay_closed(mode: str) -> None:
    fake = FakeSharing()
    with pytest.raises(RouteRefused, match=f"mode:{mode}: candidate"):
        gate(fake)[0].write(T1, SPEC, "mode", mode)
    assert fake.sent == [] and fake.reads == 0


@pytest.mark.parametrize("mode", ["eco", "holiday", dps.MODE_FROST_PROTECTION])
def test_a_trial_opens_one_mode_for_one_device(mode: str) -> None:
    fake = FakeSharing(mode="program")
    router = Router(Channel, {f"mode:{mode}": T1.dev_id})
    made, _ = gate(fake, router=router)
    result = made.write(T1, SPEC, "mode", mode)
    assert result.confirmed and fake.sent == [(T1.dev_id, [{"code": "mode", "value": mode}])]
    other = Device("devsynthetic0002", "Room 2", dps.PRODUCT_FP_THERMOSTAT)
    with pytest.raises(RouteRefused, match="candidate"):
        made.write(other, SPEC, "mode", mode)
    others = {"eco", "holiday", dps.MODE_FROST_PROTECTION} - {mode}
    for closed in others:
        with pytest.raises(RouteRefused, match="candidate"):
            made.write(T1, SPEC, "mode", closed)
    assert made.write(T1, SPEC, "mode", "manual").confirmed  # proven modes stay open


def test_the_frost_value_is_refused_by_a_device_that_lists_its_modes_but_not_open() -> None:
    router = Router(Channel, {"mode:eco": T1.dev_id})
    with pytest.raises(RouteRefused, match="mode:hors"):
        gate(FakeSharing(), router=router)[0].write(T1, SPEC, "mode", "hors")


@pytest.mark.parametrize("key", ["eco_setpoint", "frost_setpoint", "night_mode", "temperature"])
def test_no_route(key: str) -> None:
    with pytest.raises(RouteRefused, match="no route"):
        gate(FakeSharing())[0].write(T1, SPEC, key, 16.0)


def test_a_trial_opens_a_candidate_and_still_validates() -> None:
    fake = FakeSharing(child_lock=False, temperature_correction=0, holiday_setpoint=8.0)
    router = Router(
        Channel,
        {
            Operation.CHILD_LOCK: T1.dev_id,
            Operation.TEMPERATURE_CORRECTION: T1.dev_id,
            Operation.HOLIDAY_SETPOINT: T1.dev_id,
        },
    )
    made, _ = gate(fake, router=router)
    assert made.write(T1, SPEC, "child_lock", True).confirmed
    assert made.write(T1, SPEC, "temperature_correction", -2).confirmed
    assert [commands for _, commands in fake.sent] == [
        [{"code": "child_lock", "value": True}],
        [{"code": "temp_correction", "value": -2}],
    ]
    assert fake.values["temperature_correction"] == -2
    with pytest.raises(WriteRefused, match="above the ceiling"):
        made.write(T1, SPEC, "holiday_setpoint", 35.0)
    with pytest.raises(WriteRefused, match="bounds"):
        made.write(T1, SPEC, "temperature_correction", 10)


def test_other_refusals_before_any_io() -> None:
    fake = FakeSharing()
    made, _ = gate(fake)
    other = Device("devsynthetic0002", "Other", "otherproduct")
    with pytest.raises(WriteRefused, match="not verified"):
        made.write(other, SPEC, "setpoint", 20.0)
    with pytest.raises(WriteRefused, match="does not declare it writable"):
        made.write(T1, None, "setpoint", 20.0)
    with pytest.raises(WriteRefused, match="does not declare it writable"):
        made.write(T1, DeviceSpec(T1.product_id), "setpoint", 20.0)
    with pytest.raises(WriteRefused, match="declares no bounds"):
        made.write(T1, DeviceSpec(T1.product_id, writable=frozenset({"setpoint"})), "setpoint", 20)
    with pytest.raises(RouteRefused, match="not available"):
        gate(fake, router=Router([Channel.LAN]))[0].write(T1, SPEC, "setpoint", 20.0)
    with pytest.raises(RouteRefused, match="cannot write"):
        gate(None)[0].write(T1, SPEC, "setpoint", 20.0)
    assert fake.sent == [] and fake.reads == 0


def test_refusals_from_the_fresh_read() -> None:
    frost = FakeSharing(mode="hors")
    with pytest.raises(WriteRefused, match="'hors' mode, where it stores a setpoint"):
        gate(frost)[0].write(T1, SPEC, "setpoint", 20.0)
    # mode and on/off stay writable in frost protection: that is how one leaves it
    assert gate(frost)[0].write(T1, SPEC, "mode", "manual").confirmed

    unknown = FakeSharing()
    del unknown.values["mode"]
    with pytest.raises(WriteRefused, match="mode of the device could not be read"):
        gate(unknown)[0].write(T1, SPEC, "setpoint", 20.0)

    for online in (False, None):
        offline = FakeSharing()
        offline.online = online
        with pytest.raises(WriteRefused, match="offline"):
            gate(offline)[0].write(T1, SPEC, "setpoint", 20.0)
        assert offline.sent == []

    missing = FakeSharing()
    missing.missing = True
    with pytest.raises(WriteRefused, match="could not be read"):
        gate(missing)[0].write(T1, SPEC, "setpoint", 20.0)
    assert frost.sent == [(T1.dev_id, [{"code": "mode", "value": "manual"}])]


@pytest.mark.parametrize(
    ("mode", "why"),
    [
        ("program", "the running schedule slot overwrites"),
        ("eco", "nothing shows that a written setpoint is kept"),
        ("holiday", "nothing shows that a written setpoint is kept"),
        ("hors", "it stores a setpoint and does not apply it"),
        ("unexpected", "nothing shows that a written setpoint is kept"),
    ],
)
def test_setpoint_is_written_only_in_manual_mode(mode: str, why: str) -> None:
    fake = FakeSharing(mode=mode)
    with pytest.raises(WriteRefused, match=f"{mode}' mode, where {why}.*only in 'manual' mode"):
        gate(fake)[0].write(T1, SPEC, "setpoint", 20.0)
    assert fake.sent == [] and fake.reads == 1
    # the other proven writes do not depend on the mode
    assert gate(fake)[0].write(T1, SPEC, "mode", "manual").confirmed
    assert fake.sent == [(T1.dev_id, [{"code": "mode", "value": "manual"}])]


def test_a_channel_error_is_not_retried() -> None:
    fake = FakeSharing()
    fake.fail_send = True
    with pytest.raises(ChannelError):
        gate(fake)[0].write(T1, SPEC, "setpoint", 20.0)
    assert fake.reads == 1


# -- weekly schedule ---------------------------------------------------------------------------

NIGHT = [{"time": "00:00", "temperature": 16.0}]
DAY = [
    {"time": "00:00", "temperature": 16.0},
    {"time": "07:30", "temperature": 19.5},
    {"time": "11:00", "temperature": 16.0},
]


def week(header: int = 0x33) -> dict[str, str]:
    parts: dict[str, str] = {}
    for day in range(1, 8):
        current = {
            schedule.part_code(day, part): schedule.encode_part([16.0] * 24, bytes([header] * 12))
            for part in range(1, 5)
        }
        parts.update(schedule.encode_day(day, NIGHT, current))  # type: ignore[arg-type]
    return parts


class FakeDeveloper:
    def __init__(self, parts: dict[str, str], deaf: bool = False) -> None:
        self.parts = dict(parts)
        self.issued: list[dict[str, str]] = []
        self.reads = 0
        self.deaf = deaf

    def schedule_parts(self, dev_id: str) -> dict[str, str]:
        self.reads += 1
        return dict(self.parts)

    def issue_schedule_parts(self, dev_id: str, parts: Mapping[str, str]) -> None:
        self.issued.append(dict(parts))
        if not self.deaf:
            self.parts.update(parts)


def test_schedule_write_is_read_modify_write() -> None:
    fake = FakeDeveloper(week())
    made, clock = gate(developer=fake)
    result = made.write_schedule(T1, {"tuesday": DAY})  # type: ignore[dict-item]
    assert result.confirmed and result.days == ("tuesday",)
    # only the 06:00-12:00 part differs from the night-long schedule
    assert result.written == ("week_program_18_2_2",)
    assert list(fake.issued[0]) == ["week_program_18_2_2"]
    assert schedule.part_header(fake.issued[0]["week_program_18_2_2"]) == bytes([0x33] * 12)
    assert result.week["tuesday"] == DAY and result.week["monday"] == NIGHT
    assert clock.sleeps == [2.0] and fake.reads == 2


def test_schedule_already_as_asked_writes_nothing() -> None:
    fake = FakeDeveloper(week())
    result = gate(developer=fake)[0].write_schedule(T1, {"sunday": NIGHT, "monday": NIGHT})  # type: ignore[dict-item]
    assert result.confirmed and result.written == () and result.detail == "already as asked"
    assert result.days == ("monday", "sunday") and fake.issued == []


def test_schedule_not_confirmed() -> None:
    fake = FakeDeveloper(week(), deaf=True)
    made, clock = gate(developer=fake, confirm_timeout=4)
    result = made.write_schedule(T1, {"monday": DAY})  # type: ignore[dict-item]
    assert not result.confirmed and result.week["monday"] == NIGHT
    assert result.detail == "the schedule read back differs after 4 s"
    assert len(fake.issued) == 1 and clock.sleeps == [2.0, 2.0]


def test_schedule_refusals() -> None:
    fake = FakeDeveloper(week())
    made, _ = gate(developer=fake)
    cases: list[tuple[Any, str]] = [
        ({}, "at least one day"),
        ([], "at least one day"),
        ({"funday": NIGHT}, "unknown day"),
        ({"monday": [{"time": "01:00", "temperature": 16.0}]}, "00:00"),
        ({"monday": [{"time": "00:00", "temperature": 35.0}]}, "out of bounds"),
    ]
    for days, message in cases:
        with pytest.raises(WriteRefused, match=message):
            made.write_schedule(T1, days)
    assert fake.reads == 0

    partial = FakeDeveloper({k: v for k, v in week().items() if k != "week_program_18_1_3"})
    with pytest.raises(WriteRefused, match="could not be read in full"):
        gate(developer=partial)[0].write_schedule(T1, {"monday": DAY})  # type: ignore[dict-item]
    broken = FakeDeveloper({**week(), "week_program_18_1_3": "AAAA"})
    with pytest.raises(WriteRefused, match="36"):
        gate(developer=broken)[0].write_schedule(T1, {"monday": DAY})  # type: ignore[dict-item]
    with pytest.raises(RouteRefused, match="cannot write"):
        gate(developer=None)[0].write_schedule(T1, {"monday": DAY})  # type: ignore[dict-item]
    with pytest.raises(RouteRefused, match="not available"):
        gate(developer=fake, router=Router([Channel.SHARING]))[0].write_schedule(
            T1,
            {"monday": DAY},  # type: ignore[dict-item]
        )
    assert fake.issued == [] and partial.issued == [] and broken.issued == []
