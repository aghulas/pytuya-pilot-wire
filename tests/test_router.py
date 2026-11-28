"""Router. Synthetic data only."""

import pytest

from pytuya_pilot_wire import dps
from pytuya_pilot_wire.errors import RouteRefused
from pytuya_pilot_wire.model import Channel, Device
from pytuya_pilot_wire.router import (
    MODE_ROUTES,
    ROUTES,
    Operation,
    Router,
    RouteState,
    operation_of,
    write_name,
)

T1 = Device("devsynthetic0001", "Room 1", dps.PRODUCT_FP_THERMOSTAT)
T2 = Device("devsynthetic0002", "Room 2", dps.PRODUCT_FP_THERMOSTAT)
ALL = Router(Channel)


def test_every_operation_has_a_route_entry() -> None:
    assert set(ROUTES[dps.PRODUCT_FP_THERMOSTAT]) == set(Operation)


@pytest.mark.parametrize(
    ("operation", "channel"),
    [
        (Operation.READ_STATE, Channel.SHARING),
        (Operation.READ_POWER_ENERGY, Channel.LAN),
        (Operation.READ_SETTINGS, Channel.DEVELOPER),
        (Operation.READ_SCHEDULE, Channel.DEVELOPER),
        (Operation.SWITCH, Channel.SHARING),
        (Operation.MODE, Channel.SHARING),
        (Operation.SETPOINT, Channel.SHARING),
        (Operation.SCHEDULE, Channel.DEVELOPER),
    ],
)
def test_proven_routes(operation: Operation, channel: Channel) -> None:
    assert ALL.route(T1, operation) is channel


def test_a_read_takes_the_next_proven_channel() -> None:
    assert Router([Channel.LAN]).route(T1, Operation.READ_SETTINGS) is Channel.LAN


def test_no_fallback_to_another_channel() -> None:
    with pytest.raises(RouteRefused, match="sharing channel is not available"):
        Router([Channel.LAN, Channel.DEVELOPER]).route(T1, Operation.SETPOINT)
    with pytest.raises(RouteRefused, match="developer or lan"):
        Router([Channel.SHARING]).route(T1, Operation.READ_SETTINGS)


@pytest.mark.parametrize(
    "operation",
    [
        Operation.HOLIDAY_SETPOINT,
        Operation.TEMPERATURE_CORRECTION,
        Operation.WINDOW_DETECTION,
        Operation.CHILD_LOCK,
    ],
)
def test_candidates_are_closed(operation: Operation) -> None:
    with pytest.raises(RouteRefused, match="candidate"):
        ALL.route(T1, operation)


@pytest.mark.parametrize(
    "operation", [Operation.ECO_SETPOINT, Operation.FROST_SETPOINT, Operation.NIGHT_MODE]
)
def test_no_route(operation: Operation) -> None:
    with pytest.raises(RouteRefused, match="no route"):
        ALL.route(T1, operation)
    with pytest.raises(RouteRefused, match="no route"):
        Router(Channel, {operation: T1.dev_id}).route(T1, operation)


def test_a_trial_opens_a_candidate_for_one_device_only() -> None:
    router = Router(Channel, {Operation.CHILD_LOCK: T1.dev_id})
    assert router.route(T1, Operation.CHILD_LOCK) is Channel.SHARING
    with pytest.raises(RouteRefused, match="candidate"):
        router.route(T2, Operation.CHILD_LOCK)
    with pytest.raises(RouteRefused, match="candidate"):
        router.route(T1, Operation.WINDOW_DETECTION)


def test_unsupported_product() -> None:
    with pytest.raises(RouteRefused, match="not supported"):
        ALL.route(Device("devsynthetic0003", "Other", "otherproduct"), Operation.READ_STATE)


def test_describe() -> None:
    table = Router([Channel.SHARING]).describe(dps.PRODUCT_FP_THERMOSTAT)
    assert table["setpoint"]["state"] == RouteState.PROVEN and table["setpoint"]["available"]
    assert table["schedule"] == {
        "state": "proven",
        "channels": ["developer"],
        "available": False,
        "record": "written read-modify-write on one thermostat and restored, 2026-11-14",
    }
    assert table["frost_setpoint"]["channels"] == []
    assert Router(Channel).describe("otherproduct") == {}


def test_operation_of() -> None:
    assert operation_of("setpoint") is Operation.SETPOINT
    assert Operation.SETPOINT.is_write and not Operation.READ_STATE.is_write
    for key in ("temperature", "read_state", "nothing"):
        with pytest.raises(RouteRefused):
            operation_of(key)


def test_mode_values_have_routes_of_their_own() -> None:
    assert ALL.route(T1, Operation.MODE, "manual") is Channel.SHARING
    assert ALL.route(T1, Operation.MODE, "program") is Channel.SHARING
    assert ALL.route(T1, Operation.MODE, "holiday") is Channel.SHARING
    assert ALL.route(T1, Operation.MODE, "eco") is Channel.SHARING
    assert ALL.state(T1.product_id, Operation.MODE, "hors") is RouteState.NONE
    with pytest.raises(RouteRefused, match="mode:hors: no route"):
        ALL.route(T1, Operation.MODE, dps.MODE_FROST_PROTECTION)
    with pytest.raises(RouteRefused, match="mode:comfort: no route"):
        ALL.route(T1, Operation.MODE, "comfort")
    assert set(MODE_ROUTES[dps.PRODUCT_FP_THERMOSTAT]) == {
        "manual",
        "program",
        "eco",
        "holiday",
        dps.MODE_FROST_PROTECTION,
    }


def test_a_mode_trial_is_named_after_its_value() -> None:
    assert write_name(Operation.MODE, "holiday") == "mode:holiday"
    assert write_name(Operation.CHILD_LOCK) == "child_lock"
    router = Router(Channel, {"mode:hors": T1.dev_id, Operation.CHILD_LOCK: T2.dev_id})
    assert router.trials == {"mode:hors": T1.dev_id, "child_lock": T2.dev_id}
    assert router.route(T2, Operation.CHILD_LOCK) is Channel.SHARING
    with pytest.raises(RouteRefused):
        router.route(T1, Operation.CHILD_LOCK)


def test_eco_and_frost_setpoints_are_not_declared_by_the_sharing_api() -> None:
    for operation in (Operation.ECO_SETPOINT, Operation.FROST_SETPOINT):
        with pytest.raises(RouteRefused, match="does not declare it writable"):
            ALL.route(T1, operation)


def test_describe_lists_the_mode_values() -> None:
    table = ALL.describe(dps.PRODUCT_FP_THERMOSTAT)
    assert table["mode:eco"]["state"] == "proven"
    assert table["mode:hors"]["state"] == "none"
    assert table["mode:holiday"]["state"] == "proven"
    assert table["mode:manual"]["state"] == "proven"
