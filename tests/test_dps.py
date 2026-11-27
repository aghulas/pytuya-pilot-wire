"""Data-point decoding. Synthetic data only."""

import pytest

from pytuya_pilot_wire import dps


def test_heating_reading() -> None:
    decoded = dps.decode_lan(
        dps.PRODUCT_FP_THERMOSTAT,
        {"1": True, "2": "manual", "16": 185, "50": 200, "101": 2301, "102": 4300, "107": 1234},
    )
    assert decoded == {
        "switch": True,
        "mode": "manual",
        "temperature": 18.5,
        "setpoint": 20.0,
        "voltage": 230.1,
        "current_ma": 4300,
        "energy": 123.4,
        "heating": True,
        "power": 989.4,
    }


def test_absent_data_points_stay_unknown() -> None:
    decoded = dps.decode_lan(dps.PRODUCT_FP_THERMOSTAT, {"16": 190})
    assert decoded == {"temperature": 19.0}
    assert "heating" not in decoded and "power" not in decoded


def test_idle_from_work_state_gives_zero_power() -> None:
    decoded = dps.decode_lan(dps.PRODUCT_FP_THERMOSTAT, {"124": "closed"})
    assert decoded["heating"] is False and decoded["power"] == 0.0


def test_heating_without_measurement_gives_no_power() -> None:
    decoded = dps.decode_lan(dps.PRODUCT_FP_THERMOSTAT, {"124": "opened"})
    assert decoded["heating"] is True and "power" not in decoded


def test_wrong_types_and_unknown_data_points_are_dropped() -> None:
    decoded = dps.decode_lan(
        dps.PRODUCT_FP_THERMOSTAT, {"1": "yes", "2": 3, "16": "hot", "50": True, "999": 1}
    )
    assert decoded == {}


def test_undeclared_mode_is_passed_through() -> None:
    decoded = dps.decode_lan(dps.PRODUCT_FP_THERMOSTAT, {"2": "frost"})
    assert decoded["mode"] == "frost" and decoded["mode"] not in dps.DECLARED_MODES


def test_unknown_product() -> None:
    with pytest.raises(KeyError):
        dps.decode_lan("unknown", {})
