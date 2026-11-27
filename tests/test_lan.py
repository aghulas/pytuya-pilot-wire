"""LAN channel, against a fake tinytuya. Synthetic data only."""

from __future__ import annotations

from typing import Any

import pytest

from pytuya_pilot_wire import dps
from pytuya_pilot_wire.channels.lan import Gateway, LanChannel
from pytuya_pilot_wire.errors import ChannelUnavailable
from pytuya_pilot_wire.model import Channel, Device

GATEWAY = Gateway("gwsynthetic0001", "gateway.invalid", "synthetic-key")


def device(n: int, **kwargs: Any) -> Device:
    fields = {"product_id": dps.PRODUCT_FP_THERMOSTAT, "node_id": f"node{n:02d}", **kwargs}
    return Device(f"devsynthetic{n:04d}", f"Room {n}", **fields)


class FakeTuya:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.created: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.closed = 0
        self.timeout: float | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.created.append((args, kwargs))
        if "parent" not in kwargs:
            return self
        answer = self.answers.get(kwargs["cid"])
        if isinstance(answer, Exception) and kwargs["cid"] == "node09":
            raise answer
        return _Sub(answer)

    def set_socketTimeout(self, timeout: float) -> None:  # noqa: N802 - tinytuya's name
        self.timeout = timeout

    def close(self) -> None:
        self.closed += 1
        raise OSError("already closed")


class _Sub:
    def __init__(self, answer: Any) -> None:
        self.answer = answer

    def status(self) -> Any:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_gateway_repr_hides_the_key() -> None:
    assert "synthetic-key" not in repr(GATEWAY)


def test_reads_each_sub_device_through_the_gateway() -> None:
    fake = FakeTuya(
        {
            "node01": {"dps": {"16": 195, "101": 2300, "102": 4000}},
            "node02": {"Error": "Timeout Waiting for Device", "Err": "902"},
            "node03": TimeoutError("slow"),
            "node04": None,
            "node09": RuntimeError("cannot build"),
        }
    )
    lan = LanChannel(GATEWAY, device_factory=fake, clock=lambda: 50.0, timeout=3)
    readings = lan.read(
        [
            device(1),
            device(2),
            device(3),
            device(4),
            device(9),
            device(5, node_id=None),
            device(6, product_id="otherproduct"),
            device(7, gateway_id="gwsynthetic0002"),
        ]
    )
    assert len(readings) == 1
    assert readings[0].dev_id == "devsynthetic0001" and readings[0].channel is Channel.LAN
    assert readings[0].at == 50.0 and readings[0].online is None
    assert readings[0].values == {
        "temperature": 19.5,
        "voltage": 230.0,
        "current_ma": 4000,
        "heating": True,
        "power": 920.0,
    }
    assert lan.errors == {
        "devsynthetic0002": "Timeout Waiting for Device",
        "devsynthetic0003": "TimeoutError",
        "devsynthetic0004": "empty answer",
        "devsynthetic0009": "RuntimeError",
    }
    gateway_args, gateway_kwargs = fake.created[0]
    assert gateway_args == ("gwsynthetic0001", "gateway.invalid", "synthetic-key")
    assert gateway_kwargs == {"version": 3.3, "persist": True}
    assert fake.created[1] == (("devsynthetic0001",), {"cid": "node01", "parent": fake})
    assert fake.timeout == 3 and fake.closed == 1


def test_nothing_to_read_opens_no_connection() -> None:
    fake = FakeTuya({})
    assert LanChannel(GATEWAY, device_factory=fake).read([device(5, node_id=None)]) == []
    assert fake.created == []


def test_silent_gateway_is_unavailable() -> None:
    fake = FakeTuya({"node01": {"Error": "Network Error"}})
    lan = LanChannel(GATEWAY, device_factory=fake)
    with pytest.raises(ChannelUnavailable):
        lan.read([device(1, gateway_id="gwsynthetic0001")])
    assert lan.errors == {"devsynthetic0001": "Network Error"} and fake.closed == 1
