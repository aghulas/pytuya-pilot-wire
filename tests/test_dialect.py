"""Zigbee2MQTT dialect codec. Synthetic data only."""

import pytest

from pytuya_pilot_wire import dialect, dps
from pytuya_pilot_wire.errors import DialectError
from pytuya_pilot_wire.model import Channel, Device, DeviceSpec, DeviceState, Reading

DEVICE = Device("devsynthetic0001", "Room #1", dps.PRODUCT_FP_THERMOSTAT, "wk", "gwsynthetic01")


def state_of(**values: object) -> DeviceState:
    state = DeviceState(DEVICE)
    state.apply(Reading(DEVICE.dev_id, Channel.SHARING, 1_800_000_000, values, True))
    return state


def test_full_state() -> None:
    state = state_of(
        temperature=19.5,
        setpoint=20,
        switch=True,
        mode="program",
        window_state="close",
        window_detection=False,
        child_lock=True,
        temperature_correction=-1,
        holiday_setpoint=8.0,
        eco_setpoint=16.0,
        frost_setpoint=7.0,
        fault=0b10,
        night_mode=True,
    )
    state.apply(
        Reading(
            DEVICE.dev_id,
            Channel.LAN,
            1_800_000_060,
            dps.decode_lan(DEVICE.product_id, {"101": 2300, "102": 4000, "107": 1234}),
        )
    )
    payload = dialect.encode_state(state, DeviceSpec(DEVICE.product_id, fault_labels=("e1", "e2")))
    extension = payload.pop("tuya")
    assert payload == {
        "local_temperature": 19.5,
        "current_heating_setpoint": 20.0,
        "system_mode": "heat",
        "preset": "program",
        "running_state": "heat",
        "window_open": False,
        "window_detection": "OFF",
        "child_lock": "LOCK",
        "local_temperature_calibration": -1,
        "holiday_temperature": 8.0,
        "eco_temperature": 16.0,
        "antifrost_temperature": 7.0,
        "power": 920.0,
        "voltage": 230.0,
        "current": 4.0,
        "energy": 123.4,
        "fault": ["e2"],
    }
    assert extension["mode"] == "program"
    assert extension["sources"]["local_temperature"] == {
        "channel": "sharing",
        "at": "2027-01-15T08:00:00+00:00",
    }
    assert extension["sources"]["power"]["channel"] == "lan"
    assert set(extension["sources"]) == set(payload)


def test_unread_properties_are_absent() -> None:
    assert dialect.encode_state(state_of(temperature=18.0)) == {
        "local_temperature": 18.0,
        "tuya": {
            "sources": {
                "local_temperature": {"channel": "sharing", "at": "2027-01-15T08:00:00+00:00"}
            }
        },
    }


def test_frost_protection_and_unknown_mode() -> None:
    frost = dialect.encode_state(state_of(mode="hors", switch=False))
    assert frost["preset"] == "antifrost" and frost["system_mode"] == "off"
    unknown = dialect.encode_state(state_of(mode="something_new"))
    assert "preset" not in unknown and unknown["tuya"]["mode"] == "something_new"


def test_values_of_the_wrong_type_are_not_published() -> None:
    payload = dialect.encode_state(
        state_of(temperature="warm", switch="yes", window_state=3, current_ma="x", fault=True)
    )
    assert set(payload) == {"tuya"}


def test_fault_without_spec_names_bits() -> None:
    assert dialect.encode_state(state_of(fault=1))["fault"] == ["bit_0"]
    assert dialect.encode_state(state_of(fault=0))["fault"] == []


def test_decode_commands() -> None:
    commands = dialect.decode_command(
        {
            "system_mode": "off",
            "preset": "program",
            "current_heating_setpoint": 19.5,
            "child_lock": "LOCK",
            "window_detection": "on",
            "local_temperature_calibration": 2,
            "holiday_temperature": 8,
            "eco_temperature": 16,
            "antifrost_temperature": 7,
        }
    )
    assert [(c.key, c.value) for c in commands] == [
        ("switch", False),
        ("mode", "program"),
        ("setpoint", 19.5),
        ("child_lock", True),
        ("window_detection", True),
        ("temperature_correction", 2),
        ("holiday_setpoint", 8.0),
        ("eco_setpoint", 16.0),
        ("frost_setpoint", 7.0),
    ]
    assert commands[2].property == "current_heating_setpoint"
    assert dialect.decode_command({"preset": "antifrost"})[0].value == "hors"
    assert dialect.decode_command({"child_lock": False})[0].value is False


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        [],
        {"unknown": 1},
        {"local_temperature": 20},
        {"power": 5},
        {"current_heating_setpoint": "19"},
        {"current_heating_setpoint": True},
        {"system_mode": "cool"},
        {"system_mode": 1},
        {"preset": "boost"},
        {"preset": 3},
        {"local_temperature_calibration": 1.5},
    ],
)
def test_refused_commands(payload: object) -> None:
    with pytest.raises(DialectError):
        dialect.decode_command(payload)


def test_property_names() -> None:
    assert dialect.property_of("setpoint") == "current_heating_setpoint"
    with pytest.raises(KeyError):
        dialect.property_of("night_mode")
    names = [prop.name for prop in dialect.PROPERTIES]
    assert len(names) == len(set(names))


def test_availability() -> None:
    assert dialect.encode_availability(True) == {"state": "online"}
    assert dialect.encode_availability(False) == {"state": "offline"}
    assert dialect.encode_availability(None) == {"state": "offline"}


def test_friendly_names_are_topic_safe_and_unique() -> None:
    assert dialect.friendly_name("Hall #2 / north+", "x") == "Hall 2 north"
    assert dialect.friendly_name("#+/", "devsynthetic0003") == "devsynthetic0003"
    devices = [
        Device("devsynthetic0002", "Hall #1", "p"),
        Device("devsynthetic0001", "Hall 1", "p"),
        Device("devsynthetic0003", "bridge", "p"),
    ]
    assert dialect.friendly_names(devices) == {
        "devsynthetic0001": "Hall 1",
        "devsynthetic0002": "Hall 1 2",
        "devsynthetic0003": "bridge 2",
    }


def test_descriptor() -> None:
    assert dialect.encode_descriptor(DEVICE, "Room 1") == {
        "friendly_name": "Room 1",
        "type": "EndDevice",
        "supported": True,
        "definition": {
            "vendor": "Tuya",
            "model": "FP Thermostat",
            "description": "Pilot-wire thermostat",
        },
        "tuya": {
            "dev_id": "devsynthetic0001",
            "product_id": "fu1bfwza",
            "category": "wk",
            "gateway": "gwsynthetic01",
        },
    }
