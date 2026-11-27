"""Weekly schedule codec. Synthetic data only."""

import base64

import pytest

from pytuya_pilot_wire import schedule as s


def _part(header_byte: int, temperatures: list[float]) -> str:
    return base64.b64encode(
        bytes([header_byte] * 12) + bytes(int(t * 2) for t in temperatures)
    ).decode()


def test_part_round_trip() -> None:
    temperatures = [17.0] * 8 + [20.5] * 16
    value = _part(0x11, temperatures)
    assert s.decode_part(value) == temperatures
    assert s.part_header(value) == bytes([0x11] * 12)
    assert s.encode_part(temperatures, s.part_header(value)) == value


def test_default_header_follows_day_parity() -> None:
    assert [s.default_header(d)[0] for d in range(1, 8)] == [
        0x11, 0x55, 0x11, 0x55, 0x11, 0x55, 0x11,
    ]  # fmt: skip


@pytest.mark.parametrize("value", ["not base64 !", base64.b64encode(b"\x11" * 35).decode()])
def test_malformed_part_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        s.decode_part(value)


def test_transitions_round_trip() -> None:
    day: list[s.Transition] = [
        {"time": "00:00", "temperature": 16.0},
        {"time": "07:30", "temperature": 20.0},
        {"time": "17:15", "temperature": 16.0},
    ]
    setpoints = s.from_transitions(day)
    assert len(setpoints) == 96
    assert setpoints[29] == 16.0 and setpoints[30] == 20.0  # 07:15, 07:30
    assert setpoints[68] == 20.0 and setpoints[69] == 16.0  # 17:00, 17:15
    assert s.to_transitions(setpoints) == day


@pytest.mark.parametrize(
    "day",
    [
        [],
        [{"time": "06:00", "temperature": 19.0}],  # does not start at 00:00
        [{"time": "00:00", "temperature": 19.0}, {"time": "00:00", "temperature": 20.0}],
        [{"time": "00:10", "temperature": 19.0}],  # off the 15-minute step
        [{"time": "24:00", "temperature": 19.0}],
        [{"time": "00:00", "temperature": 4.5}],  # below the lower bound
        [{"time": "00:00", "temperature": 30.5}],  # above the upper bound
        [{"time": "00:00", "temperature": 19.3}],  # off the 0.5 °C step
        [{"time": "00:00", "temperature": True}],
        [{"time": "00:00"}],
        [{"time": "00:00", "temperature": 19.0, "extra": 1}],
    ],
)
def test_invalid_day_is_refused(day: list) -> None:
    with pytest.raises(ValueError):
        s.from_transitions(day)


def test_encode_day_keeps_the_header_read_from_the_device() -> None:
    current = {s.part_code(2, part): _part(0x77, [18.0] * 24) for part in range(1, 5)}
    encoded = s.encode_day(2, [{"time": "00:00", "temperature": 21.0}], current)
    assert set(encoded) == set(current)
    for value in encoded.values():
        assert s.part_header(value) == bytes([0x77] * 12)
        assert s.decode_part(value) == [21.0] * 24


def test_encode_day_without_a_reading_uses_the_default_header() -> None:
    encoded = s.encode_day(2, [{"time": "00:00", "temperature": 21.0}])
    assert all(s.part_header(v) == bytes([0x55] * 12) for v in encoded.values())


def test_week_round_trip_and_partial_day_left_out() -> None:
    monday: list[s.Transition] = [
        {"time": "00:00", "temperature": 16.0},
        {"time": "08:00", "temperature": 19.5},
    ]
    values = s.encode_day(1, monday)
    values[s.part_code(3, 1)] = _part(0x11, [18.0] * 24)  # Wednesday: one part out of four
    assert s.decode_week(values) == {"monday": monday}


@pytest.mark.parametrize(("day", "part"), [(0, 1), (8, 1), (1, 0), (1, 5)])
def test_part_code_bounds(day: int, part: int) -> None:
    with pytest.raises(ValueError):
        s.part_code(day, part)
