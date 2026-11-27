"""Bridge daemon core, against fake channels and a fake MQTT transport. Synthetic data only."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest

from pytuya_pilot_wire import dps, schedule
from pytuya_pilot_wire.bridge import Bridge, BridgeConfig
from pytuya_pilot_wire.channels.developer import GatewayKey, LanInventory
from pytuya_pilot_wire.errors import AuthenticationError, ChannelError, ChannelUnavailable
from pytuya_pilot_wire.gate import WriteGate
from pytuya_pilot_wire.model import Bounds, Channel, Device, DeviceSpec, Reading
from pytuya_pilot_wire.router import Operation

P = dps.PRODUCT_FP_THERMOSTAT
T1, T2, GW = "devsynthetic0001", "devsynthetic0002", "gwsynthetic0001"
SPEC = DeviceSpec(
    P,
    writable=frozenset({"switch", "mode", "setpoint", "child_lock"}),
    bounds={"setpoint": Bounds(5, 40, 0.5)},
    modes=("eco", "holiday", "program", "manual"),
    fault_labels=("e1",),
)


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class Transport:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any, bool]] = []
        self.subscriptions: dict[str, Callable[[str, str], None]] = {}

    def publish(self, topic: str, payload: str, *, retain: bool = False) -> None:
        self.published.append((topic, json.loads(payload), retain))

    def subscribe(self, topic: str, callback: Callable[[str, str], None]) -> None:
        self.subscriptions[topic] = callback

    def last(self, topic: str) -> Any:
        return next(payload for t, payload, _ in reversed(self.published) if t == topic)

    def topics(self) -> list[str]:
        return [topic for topic, _, _ in self.published]

    def clear(self) -> None:
        self.published.clear()


class Sharing:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.devices = [
            Device(T1, "Hall #1", P, "wk", node_id="node01", online=True),
            Device(T2, "Hall #2", P, "wk", node_id="node02", online=True),
            Device(GW, "Gateway", "gatewayproduct", "wg2", online=True),
        ]
        self.values: dict[str, dict[str, Any]] = {
            T1: {
                "temperature": 19.5,
                "setpoint": 20.0,
                "mode": "manual",
                "switch": True,
                "fault": 0,
            },
            T2: {"temperature": 18.0, "setpoint": 20.0, "mode": "hors", "switch": True},
        }
        self.online = {T1: True, T2: True}
        self.error: Exception | None = None
        self.discoveries = 0
        self.read_calls: list[list[str]] = []
        self.sent: list[tuple[str, list[dict[str, Any]]]] = []
        self.deaf = False

    def discover(self) -> list[Device]:
        self.discoveries += 1
        if self.error:
            raise self.error
        return list(self.devices)

    def spec(self, dev_id: str) -> DeviceSpec | None:
        return SPEC if dev_id in self.values else None

    def read(self, dev_ids: list[str]) -> list[Reading]:
        self.read_calls.append(list(dev_ids))
        if self.error:
            raise self.error
        return [
            Reading(d, Channel.SHARING, self.clock(), dict(self.values[d]), self.online[d])
            for d in dev_ids
            if d in self.values
        ]

    def send(self, dev_id: str, commands: list[dict[str, Any]]) -> None:
        self.sent.append((dev_id, commands))
        if self.deaf:
            return
        for command in commands:
            point = next(p for p in dps.table(P) if p.code == command["code"])
            raw = command["value"]
            self.values[dev_id][point.key] = raw / point.divisor if point.divisor != 1 else raw


class Lan:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.error: Exception | None = None
        self.calls: list[Sequence[Device]] = []
        self.raw: dict[str, dict[str, Any]] = {
            T1: {"16": 150, "50": 100, "101": 2300, "102": 4000, "107": 1234, "117": 160}
        }

    def read(self, devices: Sequence[Device]) -> list[Reading]:
        self.calls.append(devices)
        if self.error:
            raise self.error
        return [
            Reading(d.dev_id, Channel.LAN, self.clock(), dps.decode_lan(P, self.raw[d.dev_id]))
            for d in devices
            if d.dev_id in self.raw
        ]


WEEK = {
    code: value
    for day in range(1, 8)
    for code, value in schedule.encode_day(day, [{"time": "00:00", "temperature": 16.0}]).items()
}


class Developer:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.error: Exception | None = None
        self.parts = dict(WEEK)
        self.reads = 0
        self.issued: list[dict[str, str]] = []

    def lan_inventory(self) -> LanInventory:
        if self.error:
            raise self.error
        return LanInventory(
            (GatewayKey(GW, "Gateway", "synthetic-key"),),
            (
                Device(T1, "Hall #1", P, "wk", GW, "node01"),
                Device(T2, "Hall #2", P, "wk", GW, "node02"),
            ),
        )

    def read(self, device: Device) -> Reading:
        self.reads += 1
        if self.error:
            raise self.error
        values = {"eco_setpoint": 16.0, "frost_setpoint": 7.0, "temperature": 99.0}
        return Reading(device.dev_id, Channel.DEVELOPER, self.clock(), values)

    def schedule_parts(self, dev_id: str) -> dict[str, str]:
        return dict(self.parts)

    def read_schedule(self, dev_id: str) -> dict[str, Any]:
        if self.error:
            raise self.error
        return dict(schedule.decode_week(self.parts))

    def issue_schedule_parts(self, dev_id: str, parts: Mapping[str, str]) -> None:
        self.issued.append(dict(parts))
        self.parts.update(parts)


class Rig:
    def __init__(self, *, channels: str = "sld", **config: Any) -> None:
        self.clock = Clock()
        self.transport = Transport()
        self.sharing = Sharing(self.clock) if "s" in channels else None
        self.lan = Lan(self.clock) if "l" in channels else None
        self.developer = Developer(self.clock) if "d" in channels else None
        self.bridge = Bridge(
            self.transport,
            sharing=self.sharing,
            lan=self.lan,
            developer=self.developer,
            config=BridgeConfig(**config),
            clock=self.clock,
        )
        if self.bridge.gate.enabled:
            self.bridge.gate = WriteGate(
                self.bridge.router,
                sharing=self.sharing,
                developer=self.developer,
                enabled=True,
                sleep=lambda seconds: None,
                monotonic=self.clock,
                confirm_timeout=0,
            )

    def send(self, topic: str, payload: Any) -> None:
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        for pattern, callback in self.transport.subscriptions.items():
            if pattern.split("/")[-1] in ("set", "#"):
                callback(f"tuyapilot2mqtt/{topic}", raw)
                break
        self.bridge.step()

    def response(self, action: str) -> Any:
        return self.transport.last(f"tuyapilot2mqtt/bridge/response/{action}")


def started(**kwargs: Any) -> Rig:
    rig = Rig(**kwargs)
    rig.bridge.start()
    rig.bridge.step()
    return rig


def test_start_announces_and_subscribes() -> None:
    rig = Rig()
    rig.bridge.start()
    assert set(rig.transport.subscriptions) == {
        "tuyapilot2mqtt/+/set",
        "tuyapilot2mqtt/bridge/request/#",
    }
    assert rig.transport.published[0] == ("tuyapilot2mqtt/bridge/state", {"state": "online"}, True)
    info = rig.transport.last("tuyapilot2mqtt/bridge/info")
    assert info["contract_version"] == "0" and info["writes_enabled"] is False
    assert info["channels"]["sharing"] == {"state": "idle", "error": None, "last_ok": None}
    assert info["routes"]["setpoint"]["state"] == "proven"
    rig.bridge.stop()
    assert rig.transport.published[-1] == (
        "tuyapilot2mqtt/bridge/state",
        {"state": "offline"},
        True,
    )


def test_first_poll_publishes_devices_states_and_availability() -> None:
    rig = started()
    devices = rig.transport.last("tuyapilot2mqtt/bridge/devices")
    assert [d["friendly_name"] for d in devices] == ["Hall 1", "Hall 2"]
    assert devices[0]["tuya"] == {"dev_id": T1, "product_id": P, "category": "wk", "gateway": GW}
    state = rig.transport.last("tuyapilot2mqtt/Hall 1")
    sources = state.pop("tuya")["sources"]
    assert state == {
        "local_temperature": 19.5,
        "current_heating_setpoint": 20.0,
        "system_mode": "heat",
        "preset": "manual",
        "running_state": "heat",
        "eco_temperature": 16.0,
        "frost_protection_temperature": 7.0,
        "power": 920.0,
        "voltage": 230.0,
        "current": 4.0,
        "energy": 123.4,
        "fault": [],
    }
    # each channel is trusted only for what is proven on it
    assert sources["local_temperature"]["channel"] == "sharing"
    assert sources["power"]["channel"] == "lan"
    assert sources["eco_temperature"]["channel"] == "developer"
    assert rig.transport.last("tuyapilot2mqtt/Hall 2")["preset"] == "frost_protection"
    assert rig.transport.last("tuyapilot2mqtt/Hall 1/availability") == {"state": "online"}
    info = rig.transport.last("tuyapilot2mqtt/bridge/info")
    assert {c["state"] for c in info["channels"].values()} == {"ok"}
    assert all(retain for _, _, retain in rig.transport.published)


def test_polls_follow_their_intervals() -> None:
    rig = started()
    assert rig.sharing and rig.lan and rig.developer
    rig.transport.clear()
    rig.bridge.step()
    assert rig.transport.published == [] and len(rig.sharing.read_calls) == 1
    rig.clock.now += 61
    rig.sharing.values[T1]["temperature"] = 20.0
    rig.bridge.step()
    assert "tuyapilot2mqtt/bridge/info" not in rig.transport.topics()  # nothing changed in it
    assert len(rig.sharing.read_calls) == 2 and len(rig.lan.calls) == 2 and rig.developer.reads == 2
    assert rig.transport.last("tuyapilot2mqtt/Hall 1")["local_temperature"] == 20.0
    assert "tuyapilot2mqtt/bridge/devices" not in rig.transport.topics()
    rig.clock.now += 3600
    rig.bridge.step()
    assert rig.developer.reads == 4 and rig.sharing.discoveries == 1
    rig.clock.now += 6 * 3600
    rig.bridge.step()
    assert rig.sharing.discoveries == 2


def test_run_serves_until_stopped() -> None:
    rig = Rig()
    stop = threading.Event()
    original = rig.bridge.step

    def step(timeout: float = 0.0) -> None:
        original(0.01)
        stop.set()

    rig.bridge.step = step  # type: ignore[method-assign]
    rig.bridge.run(stop)
    assert rig.transport.published[-1][1] == {"state": "offline"}
    assert "tuyapilot2mqtt/Hall 1" in rig.transport.topics()


def test_a_device_going_offline_or_silent_becomes_unavailable() -> None:
    rig = started()
    assert rig.sharing and rig.lan
    rig.sharing.online[T1] = False
    rig.clock.now += 61
    rig.bridge.step()
    assert rig.transport.last("tuyapilot2mqtt/Hall 1/availability") == {"state": "offline"}
    assert rig.transport.last("tuyapilot2mqtt/Hall 2/availability") == {"state": "online"}
    rig.sharing.error = ChannelUnavailable("network: ConnectionError")
    rig.lan.error = ChannelUnavailable("gateway unreachable")
    rig.clock.now += 700
    rig.bridge.step()
    assert rig.transport.last("tuyapilot2mqtt/Hall 2/availability") == {"state": "offline"}
    info = rig.transport.last("tuyapilot2mqtt/bridge/info")
    assert info["channels"]["sharing"]["state"] == "error"
    assert info["channels"]["sharing"]["error"] == "network: ConnectionError"


def test_a_failing_channel_backs_off_and_recovers() -> None:
    rig = started()
    assert rig.sharing
    rig.sharing.error = ChannelError("Tuya: error (code 500)")
    for _ in range(2):
        rig.clock.now += 61
        rig.bridge.step()
    assert len(rig.sharing.read_calls) == 2  # one failed read, then a 120 s delay
    rig.clock.now += 120
    rig.bridge.step()
    assert len(rig.sharing.read_calls) == 3
    rig.sharing.error = None
    rig.clock.now += 300
    rig.bridge.step()
    assert rig.bridge.status[Channel.SHARING].state == "ok"
    assert rig.bridge.status[Channel.SHARING].failures == 0


def test_refused_credentials_pause_the_channel_until_replaced() -> None:
    rig = started()
    assert rig.sharing
    rig.sharing.error = AuthenticationError("token refused: log in again", "1010")
    rig.clock.now += 61
    rig.bridge.step()
    calls = len(rig.sharing.read_calls)
    rig.clock.now += 7200
    rig.bridge.step()
    assert len(rig.sharing.read_calls) == calls
    info = rig.transport.last("tuyapilot2mqtt/bridge/info")
    assert info["channels"]["sharing"]["state"] == "unauthenticated"
    fresh = Sharing(rig.clock)
    rig.bridge.replace_channel(Channel.SHARING, fresh)
    rig.bridge.step()
    assert len(fresh.read_calls) == 1 and rig.bridge.status[Channel.SHARING].state == "ok"
    rig.bridge.replace_channel(Channel.LAN, Lan(rig.clock))
    rig.bridge.replace_channel(Channel.DEVELOPER, Developer(rig.clock))
    with pytest.raises(ValueError):
        Rig(channels="s").bridge.replace_channel(Channel.LAN, Lan(rig.clock))


def test_failed_discovery_is_retried() -> None:
    rig = Rig()
    assert rig.sharing and rig.developer
    rig.sharing.error = ChannelUnavailable("network: ConnectionError")
    rig.developer.error = ChannelError("developer API: down")
    rig.bridge.start()
    rig.bridge.step()
    assert "tuyapilot2mqtt/bridge/devices" not in rig.transport.topics()
    rig.sharing.error = None
    rig.clock.now += 200
    rig.bridge.step()
    devices = rig.transport.last("tuyapilot2mqtt/bridge/devices")
    assert devices[0]["tuya"]["gateway"] is None  # the developer inventory was not available


def test_a_removed_device_goes_offline() -> None:
    rig = started()
    assert rig.sharing
    rig.sharing.devices = rig.sharing.devices[:1]
    rig.clock.now += 6 * 3600 + 1
    rig.bridge.step()
    assert rig.transport.last("tuyapilot2mqtt/Hall 2/availability") == {"state": "offline"}
    assert [d["friendly_name"] for d in rig.transport.last("tuyapilot2mqtt/bridge/devices")] == [
        "Hall 1"
    ]
    assert T2 not in rig.bridge.states


def test_configured_names() -> None:
    rig = started(
        names={T1: "Library/1", T2: "Library 1", "devunknown0001": "x"}, prefix="site/tuya"
    )
    devices = rig.transport.last("site/tuya/bridge/devices")
    assert [d["friendly_name"] for d in devices] == ["Library 1", "Library 1 2"]
    assert rig.transport.last("site/tuya/Library 1")["local_temperature"] == 19.5


def test_without_the_sharing_api_only_lan_proven_keys_are_published() -> None:
    rig = started(channels="ld")
    state = rig.transport.last("tuyapilot2mqtt/Hall 1")
    assert "local_temperature" not in state and "current_heating_setpoint" not in state
    assert state["energy"] == 123.4 and state["eco_temperature"] == 16.0
    assert rig.transport.last("tuyapilot2mqtt/Hall 1/availability") == {"state": "online"}
    assert "tuyapilot2mqtt/Hall 2/availability" in rig.transport.topics()
    info = rig.transport.last("tuyapilot2mqtt/bridge/info")
    assert info["channels"]["sharing"] == {"state": "absent"}
    assert Rig(channels="l").bridge.discover() is False


def test_lan_serves_settings_when_there_is_no_developer_channel() -> None:
    rig = started(channels="sl")
    state = rig.transport.last("tuyapilot2mqtt/Hall 1")
    assert state["eco_temperature"] == 16.0
    assert state["tuya"]["sources"]["eco_temperature"]["channel"] == "lan"


# -- commands ----------------------------------------------------------------------------------


def test_set_is_refused_while_writes_are_disabled() -> None:
    rig = started()
    assert rig.sharing
    rig.send("Hall 1/set", {"current_heating_setpoint": 21.0, "transaction": "t1"})
    assert rig.response("device/set") == {
        "status": "error",
        "data": {"device": "Hall 1", "results": []},
        "error": "writes are disabled",
        "transaction": "t1",
    }
    assert rig.sharing.sent == []
    assert rig.transport.published[-1][2] is False


def test_set_writes_reads_back_and_publishes_what_was_read() -> None:
    rig = started(writes_enabled=True)
    assert rig.sharing
    rig.transport.clear()
    rig.send("Hall 1/set", {"current_heating_setpoint": 21.5, "preset": "schedule"})
    assert rig.sharing.sent == [
        (T1, [{"code": "temp_set", "value": 215}]),
        (T1, [{"code": "mode", "value": "program"}]),
    ]
    state = rig.transport.last("tuyapilot2mqtt/Hall 1")
    assert state["current_heating_setpoint"] == 21.5 and state["preset"] == "schedule"
    assert rig.response("device/set") == {
        "status": "ok",
        "data": {
            "device": "Hall 1",
            "results": [
                {"property": "current_heating_setpoint", "value": 21.5, "confirmed": True},
                {"property": "preset", "value": "schedule", "confirmed": True},
            ],
        },
    }


def test_unconfirmed_set_keeps_the_state_read_from_the_device() -> None:
    rig = started(writes_enabled=True)
    assert rig.sharing
    rig.sharing.deaf = True
    rig.send("Hall 1/set", {"current_heating_setpoint": 22.0, "system_mode": "off"})
    assert len(rig.sharing.sent) == 1  # the second command is not sent after a failure
    assert rig.transport.last("tuyapilot2mqtt/Hall 1")["current_heating_setpoint"] == 20.0
    response = rig.response("device/set")
    assert response["status"] == "error"
    assert response["error"].startswith("current_heating_setpoint: not confirmed")
    assert response["data"]["results"] == [
        {"property": "current_heating_setpoint", "value": 22.0, "confirmed": False}
    ]


@pytest.mark.parametrize(
    ("name", "payload", "error"),
    [
        ("Hall 1", {"child_lock": "LOCK"}, "candidate"),
        ("Hall 1", {"eco_temperature": 16}, "no route"),
        ("Hall 1", {"preset": "frost_protection"}, "not one the device declares"),
        ("Hall 1", {"current_heating_setpoint": 20.0, "child_lock": "LOCK"}, "candidate"),
        ("Hall 1", {"current_heating_setpoint": 45}, "bounds"),
        ("Hall 1", {"local_temperature": 20}, "read-only"),
        ("Hall 1", {"unknown": 1}, "unknown property"),
        ("Hall 1", "not json", "not valid JSON"),
        ("Hall 1", "[1]", "not a JSON object"),
        ("Nowhere", {"system_mode": "off"}, "unknown device"),
        ("Hall 2", {"current_heating_setpoint": 20.0}, "frost-protection"),
    ],
)
def test_refused_sets_send_nothing(name: str, payload: Any, error: str) -> None:
    rig = started(writes_enabled=True)
    assert rig.sharing
    rig.send(f"{name}/set", payload)
    response = rig.response("device/set")
    assert response["status"] == "error" and error in response["error"]
    assert rig.sharing.sent == []


def test_a_trial_opens_one_candidate_on_one_device() -> None:
    rig = started(writes_enabled=True, trials={Operation.CHILD_LOCK: T1})
    assert rig.sharing
    rig.send("Hall 1/set", {"child_lock": "LOCK"})
    assert rig.sharing.sent == [(T1, [{"code": "child_lock", "value": True}])]
    assert rig.response("device/set")["status"] == "ok"
    rig.send("Hall 2/set", {"child_lock": "LOCK"})
    assert "candidate" in rig.response("device/set")["error"] and len(rig.sharing.sent) == 1


def test_schedule_requests() -> None:
    rig = started(writes_enabled=True)
    assert rig.developer
    rig.send("bridge/request/schedule/get", {"device": "Hall 1", "transaction": 7})
    response = rig.response("schedule/get")
    assert response["status"] == "ok" and response["transaction"] == 7
    assert response["data"]["monday"] == [{"time": "00:00", "temperature": 16.0}]
    day = [{"time": "00:00", "temperature": 16.0}, {"time": "07:30", "temperature": 19.5}]
    rig.send("bridge/request/schedule/set", {"device": "Hall 1", "days": {"monday": day}})
    response = rig.response("schedule/set")
    assert response["status"] == "ok" and response["data"]["monday"] == day
    assert len(rig.developer.issued) == 1


@pytest.mark.parametrize(
    ("action", "payload", "error"),
    [
        ("schedule/get", {"device": "Nowhere"}, "unknown device"),
        ("schedule/get", "nope", "not valid JSON"),
        ("schedule/set", {"device": "Hall 1"}, "at least one day"),
        ("schedule/set", {"device": "Hall 1", "days": {"funday": []}}, "unknown day"),
        ("rooms/set", {}, "unknown request"),
    ],
)
def test_refused_requests(action: str, payload: Any, error: str) -> None:
    rig = started(writes_enabled=True)
    assert rig.developer
    rig.send(f"bridge/request/{action}", payload)
    response = rig.response(action)
    assert response["status"] == "error" and error in response["error"]
    assert rig.developer.issued == []


def test_schedule_requests_without_their_channel() -> None:
    rig = started(channels="sl", writes_enabled=True)
    rig.send("bridge/request/schedule/get", {"device": "Hall 1"})
    assert "developer channel is not available" in rig.response("schedule/get")["error"]
    rig.send("bridge/request/schedule/set", {"device": "Hall 1", "days": {}})
    assert "developer channel is not available" in rig.response("schedule/set")["error"]
    down = started()
    assert down.developer
    down.developer.error = ChannelUnavailable("developer API: down")
    down.send("bridge/request/schedule/get", {"device": "Hall 1"})
    assert down.response("schedule/get")["error"] == "developer API: down"
    closed = started()
    closed.send("bridge/request/schedule/set", {"device": "Hall 1", "days": {"monday": []}})
    assert closed.response("schedule/set")["error"] == "writes are disabled"


def test_foreign_topics_are_ignored() -> None:
    rig = started()
    rig.transport.clear()
    for topic in (
        "other/Hall 1/set",
        "tuyapilot2mqtt/bridge/set",
        "tuyapilot2mqtt/Hall 1",
        "tuyapilot2mqtt/a/b/set",
    ):
        rig.bridge.handle_message(topic, "{}")
    assert rig.transport.published == []
