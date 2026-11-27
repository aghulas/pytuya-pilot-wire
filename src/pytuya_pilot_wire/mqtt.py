"""MQTT transport for :class:`pytuya_pilot_wire.bridge.Bridge`, on ``paho-mqtt``.

The last will puts ``<prefix>/bridge/state`` to offline if the process dies. After each
(re)connection the subscriptions are restored and ``on_connect`` is called, so the bridge can
announce itself again.

Requires the ``mqtt`` extra.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from .errors import ChannelUnavailable

_LOGGER = logging.getLogger(__name__)


def topic_matches(pattern: str, topic: str) -> bool:
    """Whether ``topic`` matches an MQTT subscription ``pattern`` (``+`` and ``#`` wildcards)."""
    wanted, given = pattern.split("/"), topic.split("/")
    for index, level in enumerate(wanted):
        if level == "#":
            return True
        if index >= len(given) or (level != "+" and level != given[index]):
            return False
    return len(wanted) == len(given)


def _paho_client(client_id: str) -> Any:  # pragma: no cover - needs paho
    try:
        import paho.mqtt.client as paho
        from paho.mqtt.enums import CallbackAPIVersion
    except ImportError as err:
        raise ChannelUnavailable("paho-mqtt is not installed (extra 'mqtt')") from err
    return paho.Client(CallbackAPIVersion.VERSION2, client_id=client_id)


class PahoTransport:
    """``publish`` / ``subscribe`` over a paho client with a background network loop."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        *,
        username: str | None = None,
        password: str | None = None,
        client_id: str = "tuyapilot2mqtt",
        prefix: str = "tuyapilot2mqtt",
        on_connect: Callable[[], None] | None = None,
        client: Any = None,
    ) -> None:
        self.host, self.port = host, port
        self.on_connect = on_connect
        self.connected = False
        self._subscriptions: dict[str, Callable[[str, str], None]] = {}
        self._client = client if client is not None else _paho_client(client_id)
        if username:
            self._client.username_pw_set(username, password)
        self._client.will_set(
            f"{prefix}/bridge/state", json.dumps({"state": "offline"}), qos=1, retain=True
        )
        self._client.on_connect = self._connected
        self._client.on_disconnect = self._disconnected
        self._client.on_message = self._message

    def connect(self, keepalive: int = 60) -> None:
        """Connect and start the network loop. Reconnection is paho's."""
        self._client.connect(self.host, self.port, keepalive)
        self._client.loop_start()

    def disconnect(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()
        self.connected = False

    def publish(self, topic: str, payload: str, *, retain: bool = False) -> None:
        self._client.publish(topic, payload, qos=1, retain=retain)

    def subscribe(self, topic: str, callback: Callable[[str, str], None]) -> None:
        self._subscriptions[topic] = callback
        if self.connected:
            self._client.subscribe(topic, qos=1)

    def _connected(self, client: Any, userdata: Any, flags: Any, reason: Any, *extra: Any) -> None:
        if getattr(reason, "is_failure", False):
            _LOGGER.error("MQTT broker refused the connection: %s", reason)
            return
        self.connected = True
        for topic in self._subscriptions:
            client.subscribe(topic, qos=1)
        if self.on_connect is not None:
            self.on_connect()

    def _disconnected(self, *args: Any) -> None:
        self.connected = False

    def _message(self, client: Any, userdata: Any, message: Any) -> None:
        try:
            payload = message.payload.decode("utf-8")
        except UnicodeDecodeError:
            return
        for pattern, callback in list(self._subscriptions.items()):
            if topic_matches(pattern, message.topic):
                try:
                    callback(message.topic, payload)
                except Exception:  # a handler must not kill the network loop
                    _LOGGER.exception("MQTT handler failed")
