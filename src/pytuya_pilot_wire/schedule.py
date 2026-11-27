"""Weekly schedule codec of the Tuya pilot-wire thermostat (category ``wk``).

The schedule is held in 28 data points, ``week_program_18_<day>_<part>``: 7 days (1 = Monday) by
4 six-hour parts. Each value is base64 of 36 bytes:

- a **12-byte header**, one byte repeated: ``0x11`` on Monday, Wednesday, Friday and Sunday,
  ``0x55`` on Tuesday, Thursday and Saturday;
- **24 setpoints**, one per 15 minutes, °C = byte / 2.

Format checked on real hardware (2026-11-03, read; 2026-11-14, written and restored). A write is
read-modify-write: the header read from the device is kept as is, only the 24 setpoints change.

Pure functions, no I/O.
"""

from __future__ import annotations

import base64
import binascii
from typing import TypedDict

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

HEADER_BYTES = 12
PART_SETPOINTS = 24  # 6 h at a 15-minute step
PARTS_PER_DAY = 4
DAY_SETPOINTS = PARTS_PER_DAY * PART_SETPOINTS  # 96
STEP_MINUTES = 15
PART_BYTES = HEADER_BYTES + PART_SETPOINTS  # 36

HEADER_ODD_DAY = 0x11  # Monday, Wednesday, Friday, Sunday
HEADER_EVEN_DAY = 0x55  # Tuesday, Thursday, Saturday

# Bounds accepted for a scheduled setpoint. Narrower than the device's own 5-40 °C on purpose.
MIN_TEMPERATURE = 5.0
MAX_TEMPERATURE = 30.0
TEMPERATURE_STEP = 0.5


class Transition(TypedDict):
    """A setpoint change: ``time`` is ``"HH:MM"``, ``temperature`` is in °C."""

    time: str
    temperature: float


def part_code(day: int, part: int) -> str:
    """Tuya code of one part: ``day`` 1 (Monday) to 7, ``part`` 1 to 4."""
    if not 1 <= day <= 7:
        raise ValueError(f"day {day} out of range (1-7)")
    if not 1 <= part <= PARTS_PER_DAY:
        raise ValueError(f"part {part} out of range (1-{PARTS_PER_DAY})")
    return f"week_program_18_{day}_{part}"


def default_header(day: int) -> bytes:
    """Header the thermostats carry for ``day`` (1 = Monday). Used only when none was read."""
    if not 1 <= day <= 7:
        raise ValueError(f"day {day} out of range (1-7)")
    return bytes([HEADER_ODD_DAY if day % 2 == 1 else HEADER_EVEN_DAY] * HEADER_BYTES)


def _raw_part(value: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as err:
        raise ValueError("schedule part is not valid base64") from err
    if len(raw) != PART_BYTES:
        raise ValueError(f"schedule part is {len(raw)} bytes, expected {PART_BYTES}")
    return raw


def decode_part(value: str) -> list[float]:
    """One part (base64) to its 24 setpoints in °C."""
    return [b / 2 for b in _raw_part(value)[HEADER_BYTES:]]


def part_header(value: str) -> bytes:
    """The 12-byte header of one part (base64), to carry over in a write."""
    return _raw_part(value)[:HEADER_BYTES]


def _temperature_byte(temperature: float) -> int:
    if isinstance(temperature, bool) or not isinstance(temperature, int | float):
        raise ValueError(f"temperature {temperature!r} is not a number")
    if not MIN_TEMPERATURE <= temperature <= MAX_TEMPERATURE:
        raise ValueError(
            f"temperature {temperature:g} °C out of bounds "
            f"({MIN_TEMPERATURE:g}-{MAX_TEMPERATURE:g} °C)"
        )
    doubled = temperature * 2
    if abs(doubled - round(doubled)) > 1e-6:
        raise ValueError(f"temperature {temperature:g} °C is not a multiple of 0.5 °C")
    return round(doubled)


def encode_part(setpoints: list[float], header: bytes) -> str:
    """24 setpoints in °C and a 12-byte header to one part (base64)."""
    if len(setpoints) != PART_SETPOINTS:
        raise ValueError(f"expected {PART_SETPOINTS} setpoints, got {len(setpoints)}")
    if len(header) != HEADER_BYTES:
        raise ValueError(f"expected a {HEADER_BYTES}-byte header, got {len(header)}")
    raw = bytes(header) + bytes(_temperature_byte(t) for t in setpoints)
    return base64.b64encode(raw).decode()


def _minutes(time: str) -> int:
    try:
        hours, minutes = time.split(":")
        total = int(hours) * 60 + int(minutes)
    except (ValueError, AttributeError) as err:
        raise ValueError(f"time {time!r} is not HH:MM") from err
    if not 0 <= total < 24 * 60 or not 0 <= int(minutes) < 60:
        raise ValueError(f"time {time!r} is outside the day")
    if total % STEP_MINUTES:
        raise ValueError(f"time {time!r} is not on a {STEP_MINUTES}-minute step")
    return total


def to_transitions(setpoints: list[float]) -> list[Transition]:
    """A day's 96 setpoints to its list of changes."""
    transitions: list[Transition] = []
    previous: float | None = None
    for index, temperature in enumerate(setpoints):
        if temperature != previous:
            minutes = index * STEP_MINUTES
            transitions.append(
                {"time": f"{minutes // 60:02d}:{minutes % 60:02d}", "temperature": temperature}
            )
            previous = temperature
    return transitions


def from_transitions(transitions: list[Transition]) -> list[float]:
    """A day's list of changes to its 96 setpoints.

    The day is described in full: the first change is at 00:00, times are on the 15-minute step
    and distinct. Raises ``ValueError`` otherwise.
    """
    if not isinstance(transitions, list) or not transitions:
        raise ValueError("empty day: give at least one transition, starting at 00:00")
    pairs: list[tuple[int, int]] = []
    for transition in transitions:
        if not isinstance(transition, dict) or set(transition) != {"time", "temperature"}:
            raise ValueError(f"invalid transition {transition!r}")
        pairs.append((_minutes(transition["time"]), _temperature_byte(transition["temperature"])))
    pairs.sort()
    if pairs[0][0] != 0:
        raise ValueError("the first transition of a day must be at 00:00")
    for (first, _), (second, _) in zip(pairs, pairs[1:], strict=False):
        if first == second:
            raise ValueError(f"two transitions at {first // 60:02d}:{first % 60:02d}")
    setpoints: list[float] = []
    index = 0
    for slot in range(DAY_SETPOINTS):
        minutes = slot * STEP_MINUTES
        while index + 1 < len(pairs) and pairs[index + 1][0] <= minutes:
            index += 1
        setpoints.append(pairs[index][1] / 2)
    return setpoints


def decode_week(values: dict[str, str]) -> dict[str, list[Transition]]:
    """Schedule data points ``{code: base64}`` to ``{day name: transitions}``.

    A day with a missing part is left out: a partial day is not a schedule.
    """
    week: dict[str, list[Transition]] = {}
    for day in range(1, 8):
        setpoints: list[float] = []
        for part in range(1, PARTS_PER_DAY + 1):
            value = values.get(part_code(day, part))
            if not value:
                break
            setpoints += decode_part(value)
        else:
            week[DAYS[day - 1]] = to_transitions(setpoints)
    return week


def encode_day(
    day: int, transitions: list[Transition], current: dict[str, str] | None = None
) -> dict[str, str]:
    """One day's transitions to its four data points ``{code: base64}``.

    ``current`` holds the values read from the device: each part keeps the header it had. Without
    it the default header of the day is used.
    """
    setpoints = from_transitions(transitions)
    encoded: dict[str, str] = {}
    for part in range(1, PARTS_PER_DAY + 1):
        code = part_code(day, part)
        previous = (current or {}).get(code)
        header = part_header(previous) if previous else default_header(day)
        chunk = setpoints[(part - 1) * PART_SETPOINTS : part * PART_SETPOINTS]
        encoded[code] = encode_part(chunk, header)
    return encoded
