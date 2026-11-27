"""MQTT transport, against a fake paho client. Synthetic data only."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pytuya_pilot_wire.mqtt import PahoTransport, topic_matches


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.on_connect: Any = None
        self.on_disconnect: Any = None
        self.on_message: Any = None

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: self.calls.append((name, *args, kwargs))


@pytest.mark.parametrize(
    ("pattern", "topic", "expected"),
    [
        ("p/+/set", "p/Hall 1/set", True),
        ("p/+/set", "p/Hall 1/availability", False),
        ("p/+/set", "p/a/b/set", False),
        ("p/+/set", "p/a", False),
        ("p/bridge/request/#", "p/bridge/request/schedule/get", True),
        ("p/bridge/request/#", "p/bridge/response/schedule/get", False),
        ("p/a", "p/a/b", False),
    ],
)
def test_topic_matches(pattern: str, topic: str, expected: bool) -> None:
    assert topic_matches(pattern, topic) is expected


def test_transport_life_cycle() -> None:
    client = FakeClient()
    announced: list[int] = []
    transport = PahoTransport(
        "broker.invalid",
        username="user",
        password="synthetic",
        prefix="site",
        on_connect=lambda: announced.append(1),
        client=client,
    )
    assert ("username_pw_set", "user", "synthetic", {}) in client.calls
    assert client.calls[-1] == (
        "will_set",
        "site/bridge/state",
        '{"state": "offline"}',
        {"qos": 1, "retain": True},
    )
    received: list[tuple[str, str]] = []
    transport.subscribe("site/+/set", lambda topic, payload: received.append((topic, payload)))
    assert not any(call[0] == "subscribe" for call in client.calls)  # not connected yet
    transport.connect()
    assert ("connect", "broker.invalid", 1883, 60, {}) in client.calls

    client.on_connect(client, None, None, SimpleNamespace(is_failure=True), None)
    assert not transport.connected and announced == []
    client.on_connect(client, None, None, SimpleNamespace(is_failure=False), None)
    assert transport.connected and announced == [1]
    assert ("subscribe", "site/+/set", {"qos": 1}) in client.calls
    transport.subscribe("site/bridge/request/#", lambda topic, payload: 1 / 0)
    assert ("subscribe", "site/bridge/request/#", {"qos": 1}) in client.calls

    transport.publish("site/x", "{}", retain=True)
    assert client.calls[-1] == ("publish", "site/x", "{}", {"qos": 1, "retain": True})

    client.on_message(client, None, SimpleNamespace(topic="site/Hall 1/set", payload=b'{"a": 1}'))
    client.on_message(client, None, SimpleNamespace(topic="site/Hall 1/set", payload=b"\xff"))
    client.on_message(client, None, SimpleNamespace(topic="site/bridge/request/x", payload=b"{}"))
    assert received == [("site/Hall 1/set", '{"a": 1}')]

    client.on_disconnect(client, None, None, None, None)
    assert not transport.connected
    transport.disconnect()
    assert client.calls[-2][0] == "loop_stop" and client.calls[-1][0] == "disconnect"


def test_anonymous_connection_sets_no_credentials() -> None:
    client = FakeClient()
    PahoTransport("broker.invalid", client=client)
    assert not any(call[0] == "username_pw_set" for call in client.calls)
