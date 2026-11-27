"""Developer API channel, against a fake cloud client. Synthetic data only."""

from __future__ import annotations

import json
from typing import Any

import pytest

from pytuya_pilot_wire import dps, schedule
from pytuya_pilot_wire.channels.developer import DeveloperChannel, DeveloperProject
from pytuya_pilot_wire.errors import AuthenticationError, ChannelError, ChannelUnavailable
from pytuya_pilot_wire.model import Channel, Device

T1 = "devsynthetic0001"
DEVICE = Device(T1, "Room 1", dps.PRODUCT_FP_THERMOSTAT)
PROJECT = DeveloperProject("synthetic-id", "synthetic-secret")
MONDAY = schedule.encode_day(1, [{"time": "00:00", "temperature": 16.0}])


class FakeCloud:
    def __init__(self, answer: Any = None, devices: Any = None, error: Any = None) -> None:
        self.answer, self.devices, self.error = answer, devices, error
        self.requests: list[tuple[str, Any]] = []

    def cloudrequest(self, url: str, post: Any = None) -> Any:
        self.requests.append((url, post))
        return self.answer

    def getdevices(self, verbose: bool = False) -> Any:
        return self.devices


def channel(cloud: FakeCloud) -> DeveloperChannel:
    return DeveloperChannel(PROJECT, cloud_factory=lambda project: cloud, clock=lambda: 7.0)


def properties(values: dict[str, Any]) -> dict[str, Any]:
    listed = [{"code": code, "value": value} for code, value in values.items()]
    return {"success": True, "result": {"properties": [*listed, {"code": "no_value"}, "junk"]}}


def test_project_repr_hides_credentials() -> None:
    assert "synthetic" not in repr(PROJECT)


def test_read_decodes_the_shadow() -> None:
    cloud = FakeCloud(properties({"temp_current": 185, "eco_temp_set": 160, "night_btn": True}))
    reading = channel(cloud).read(DEVICE)
    assert reading.channel is Channel.DEVELOPER and reading.at == 7.0
    assert reading.values == {"temperature": 18.5, "eco_setpoint": 16.0, "night_mode": True}
    assert cloud.requests == [(f"/v2.0/cloud/thing/{T1}/shadow/properties", None)]


def test_read_schedule() -> None:
    cloud = FakeCloud(properties({**MONDAY, "week_program_18_1": "", "temp_set": 200}))
    made = channel(cloud)
    assert made.schedule_parts(T1) == MONDAY
    assert made.read_schedule(T1) == {"monday": [{"time": "00:00", "temperature": 16.0}]}


def test_empty_shadow() -> None:
    assert channel(FakeCloud({"success": True, "result": None})).properties(T1) == {}


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (None, ChannelUnavailable),
        ({"Error": "Network Error", "Err": "901"}, ChannelUnavailable),
        ({"success": False, "code": 1106, "msg": "permission deny"}, AuthenticationError),
        ({"success": False, "code": 28841002, "msg": "subscription expired"}, AuthenticationError),
        ({"success": False, "code": 2009, "msg": "not supported"}, ChannelError),
    ],
)
def test_errors(answer: Any, expected: type[Exception]) -> None:
    with pytest.raises(expected) as raised:
        channel(FakeCloud(answer)).properties(T1)
    assert type(raised.value) is expected


def test_refused_project_and_bad_id() -> None:
    with pytest.raises(AuthenticationError):
        channel(FakeCloud(error={"Error": "bad secret"})).properties(T1)
    with pytest.raises(ChannelError, match="invalid device id"):
        channel(FakeCloud()).properties("../x")


def test_lan_inventory() -> None:
    cloud = FakeCloud(
        devices=[
            {"id": "gwsynthetic0001", "name": " Gateway ", "key": "synthetic-key", "sub": False},
            {
                "id": T1,
                "name": "Room 1",
                "key": "sub-key",
                "sub": True,
                "node_id": "node01",
                "product_id": dps.PRODUCT_FP_THERMOSTAT,
                "category": "wk",
                "gateway_id": "gwsynthetic0001",
            },
            {
                "id": "devsynthetic0002",
                "sub": True,
                "node_id": "node02",
                "parent": "gwsynthetic0001",
            },
            {"id": "devsynthetic0003", "sub": False},
            {"name": "no id"},
            "junk",
        ]
    )
    inventory = channel(cloud).lan_inventory()
    assert [(g.dev_id, g.name, g.local_key) for g in inventory.gateways] == [
        ("gwsynthetic0001", "Gateway", "synthetic-key")
    ]
    assert "synthetic-key" not in repr(inventory)
    assert inventory.devices == (
        Device(T1, "Room 1", dps.PRODUCT_FP_THERMOSTAT, "wk", "gwsynthetic0001", "node01"),
        Device("devsynthetic0002", "", "", "", "gwsynthetic0001", "node02"),
    )


def test_lan_inventory_errors() -> None:
    with pytest.raises(AuthenticationError):
        channel(
            FakeCloud(devices={"success": False, "code": 1010, "msg": "token invalid"})
        ).lan_inventory()
    for odd in ({"success": True}, None):
        with pytest.raises(ChannelError, match="unexpected device list"):
            channel(FakeCloud(devices=odd)).lan_inventory()


def test_issue_schedule_parts() -> None:
    cloud = FakeCloud({"success": True})
    channel(cloud).issue_schedule_parts(T1, MONDAY)
    url, post = cloud.requests[0]
    assert url == f"/v2.0/cloud/thing/{T1}/shadow/properties/issue"
    assert json.loads(post["properties"]) == MONDAY


def test_issue_refuses_anything_but_schedule_parts() -> None:
    cloud = FakeCloud({"success": True})
    made = channel(cloud)
    with pytest.raises(ChannelError, match="nothing to write"):
        made.issue_schedule_parts(T1, {})
    with pytest.raises(ChannelError, match="not a weekly-schedule part"):
        made.issue_schedule_parts(T1, {"temp_set": "200"})
    with pytest.raises(ValueError):
        made.issue_schedule_parts(T1, {"week_program_18_1_1": "AAAA"})
    assert cloud.requests == []
    with pytest.raises(ChannelError, match="refused"):
        channel(FakeCloud({"success": False, "code": 501, "msg": "refused"})).issue_schedule_parts(
            T1, MONDAY
        )
