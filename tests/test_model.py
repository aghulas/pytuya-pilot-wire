"""Device model. Synthetic data only."""

import json

import pytest

from pytuya_pilot_wire import dps
from pytuya_pilot_wire.model import Bounds, Channel, Device, DeviceSpec, DeviceState, Reading

DEVICE = Device("devsynthetic0001", "Room 1", dps.PRODUCT_FP_THERMOSTAT, "wk", node_id="node01")

FUNCTIONS = [
    {"code": "switch", "type": "Boolean", "values": "{}"},
    {"code": "mode", "type": "Enum", "values": json.dumps({"range": ["eco", "manual"]})},
    {
        "code": "temp_set",
        "type": "Integer",
        "values": json.dumps({"min": 50, "max": 400, "scale": 1, "step": 5}),
    },
    {"code": "temp_correction", "type": "Integer", "values": {"min": -9, "max": 9, "step": 1}},
    {"code": "holiday_temp_set", "type": "Integer", "values": {"min": "x", "max": 9}},
    {"code": "not_in_table", "type": "Boolean", "values": "{}"},
    {"code": "child_lock", "type": "Boolean", "values": "not json"},
]
STATUS = [
    {"code": "fault", "type": "Bitmap", "values": json.dumps({"label": ["e1", "e2", "e3"]})},
    {"code": "temp_current", "type": "Integer", "values": ""},
]


def test_device_supported() -> None:
    assert DEVICE.supported
    assert not Device("devsynthetic0002", "Gateway", "otherproduct").supported


def test_bounds() -> None:
    bounds = Bounds(5, 40, 0.5)
    assert bounds.check(19.5) == 19.5
    assert bounds.check(5) == 5.0
    for bad in (4.5, 40.5, 19.3, True, "19"):
        with pytest.raises(ValueError):
            bounds.check(bad)


def test_spec_from_instruction_set() -> None:
    spec = DeviceSpec.from_instruction_set(dps.PRODUCT_FP_THERMOSTAT, FUNCTIONS, STATUS)
    assert spec.writable == {
        "switch",
        "mode",
        "setpoint",
        "temperature_correction",
        "holiday_setpoint",
        "child_lock",
    }
    assert spec.bounds == {
        "setpoint": Bounds(5.0, 40.0, 0.5),
        "temperature_correction": Bounds(-9.0, 9.0, 1.0),
    }
    assert spec.modes == ("eco", "manual")
    assert spec.fault_labels == ("e1", "e2", "e3")


def test_spec_of_unknown_product_is_empty() -> None:
    spec = DeviceSpec.from_instruction_set("otherproduct", FUNCTIONS, STATUS)
    assert spec.writable == frozenset() and spec.bounds == {}


def test_fault_names() -> None:
    spec = DeviceSpec(dps.PRODUCT_FP_THERMOSTAT, fault_labels=("e1", "e2"))
    assert spec.faults(0) == []
    assert spec.faults(0b101) == ["e1", "bit_2"]


def reading(channel: Channel, at: float, online: bool | None = None, **values: object) -> Reading:
    return Reading(DEVICE.dev_id, channel, at, values, online)


def test_state_merges_channels() -> None:
    state = DeviceState(DEVICE)
    assert state.apply(reading(Channel.SHARING, 10, True, temperature=19.0, mode="manual")) == {
        "online",
        "temperature",
        "mode",
    }
    assert state.apply(reading(Channel.LAN, 20, temperature=19.0, energy=12.3)) == {"energy"}
    assert state.values["temperature"].channel is Channel.LAN
    assert state.values["mode"].channel is Channel.SHARING
    assert state.online is True
    assert state.get("energy") == 12.3 and state.get("setpoint") is None
    assert state.snapshot() == {"temperature": 19.0, "mode": "manual", "energy": 12.3}


def test_state_ignores_an_older_reading() -> None:
    state = DeviceState(DEVICE)
    state.apply(reading(Channel.LAN, 20, temperature=19.0))
    assert state.apply(reading(Channel.SHARING, 10, temperature=18.0)) == set()
    assert state.get("temperature") == 19.0


def test_electrical_measures_do_not_outlive_heating() -> None:
    state = DeviceState(DEVICE)
    state.apply(reading(Channel.LAN, 10, heating=True, voltage=230.0, current_ma=4000, power=920.0))
    changed = state.apply(reading(Channel.LAN, 20, heating=False, power=0.0))
    assert changed == {"heating", "power", "voltage", "current_ma"}
    assert state.snapshot() == {"heating": False, "power": 0.0}
    # heating again, told by the load state only: the power is unknown, not zero
    assert "power" in state.apply(reading(Channel.LAN, 30, heating=True))
    assert state.snapshot() == {"heating": True}


def test_state_refuses_another_device() -> None:
    with pytest.raises(ValueError):
        DeviceState(DEVICE).apply(Reading("devsynthetic0009", Channel.LAN, 1, {}))
