"""Reusable daemon core of the bridge: read loop, publication, command handling.

:class:`Bridge` ties the channels, the router, the write gate and the dialect codec to an MQTT
transport, and speaks the contract of ``tuyapilot2mqtt/docs/mqtt-contract.md``. It imports
neither Home Assistant nor an MQTT library: the transport is anything with ``publish`` and
``subscribe`` (:class:`pytuya_pilot_wire.mqtt.PahoTransport`, or a double in tests).

What the caller (the bridge daemon) keeps for itself: configuration, secrets, building the
channels, and the process life cycle. Then::

    bridge = Bridge(transport, sharing=..., lan=..., developer=..., config=BridgeConfig())
    bridge.run(stop_event)

Rules it enforces:

- every Tuya call runs on the thread that calls :meth:`Bridge.run`; MQTT callbacks only queue;
- a reading is trusted for the keys proven for its channel (the router decides), a property that
  was not read is absent, and a state changes only from a reading, never from a request;
- a ``/set`` payload is validated as a whole before anything is sent; a command without a proven
  route is refused and answered on ``bridge/response/device/set``;
- a channel that refuses its credentials is left alone until the caller replaces it; another
  failure is retried with a growing delay. The account is never hammered.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import queue
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from . import __version__, dialect, dps
from .errors import AuthenticationError, ChannelError, PilotWireError, RouteRefused
from .gate import PlannedWrite, ScheduleWriter, SharingWriter, WriteGate
from .model import Channel, Device, DeviceSpec, DeviceState, Reading
from .router import Operation, Router

_LOGGER = logging.getLogger(__name__)

DEFAULT_PREFIX = "tuyapilot2mqtt"
MAX_BACKOFF = 900.0


class MqttTransport(Protocol):
    """The MQTT client the bridge needs."""

    def publish(self, topic: str, payload: str, *, retain: bool = False) -> None: ...

    def subscribe(self, topic: str, callback: Callable[[str, str], None]) -> None: ...


class SharingSource(SharingWriter, Protocol):
    def discover(self) -> list[Device]: ...

    def spec(self, dev_id: str) -> DeviceSpec | None: ...


class LanSource(Protocol):
    def read(self, devices: Sequence[Device]) -> list[Reading]: ...


class DeveloperSource(ScheduleWriter, Protocol):
    def read(self, device: Device) -> Reading: ...

    def read_schedule(self, dev_id: str) -> dict[str, Any]: ...

    def lan_inventory(self) -> Any: ...


@dataclass(frozen=True)
class BridgeConfig:
    """Runtime settings of the bridge. Intervals are in seconds."""

    prefix: str = DEFAULT_PREFIX
    sharing_interval: float = 60.0
    lan_interval: float = 60.0
    developer_interval: float = 3600.0
    discovery_interval: float = 6 * 3600.0
    stale_after: float = 600.0
    writes_enabled: bool = False
    names: Mapping[str, str] = field(default_factory=dict)
    # Trial designations by write name (``router.write_name``): one device per candidate.
    trials: Mapping[str, str] = field(default_factory=dict)
    bridge_version: str | None = None


@dataclass
class ChannelStatus:
    """Health of one channel, as published in ``bridge/info``."""

    state: str = "idle"  # idle, ok, error, unauthenticated
    error: str | None = None
    last_ok: float | None = None
    failures: int = 0
    next_try: float = 0.0


def _iso(at: float | None) -> str | None:
    return None if at is None else datetime.fromtimestamp(at, tz=UTC).isoformat(timespec="seconds")


class Bridge:
    """The daemon core. See the module documentation."""

    def __init__(
        self,
        transport: MqttTransport,
        *,
        sharing: SharingSource | None = None,
        lan: LanSource | None = None,
        developer: DeveloperSource | None = None,
        config: BridgeConfig | None = None,
        gate: WriteGate | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config or BridgeConfig()
        self.transport = transport
        self.sharing, self.lan, self.developer = sharing, lan, developer
        present = [
            channel
            for channel, client in (
                (Channel.SHARING, sharing),
                (Channel.LAN, lan),
                (Channel.DEVELOPER, developer),
            )
            if client is not None
        ]
        self.router = Router(present, self.config.trials)
        self.gate = gate or WriteGate(
            self.router, sharing=sharing, developer=developer, enabled=self.config.writes_enabled
        )
        self.status = {channel: ChannelStatus() for channel in present}
        self.devices: dict[str, Device] = {}
        self.states: dict[str, DeviceState] = {}
        self.names: dict[str, str] = {}
        self._clock = clock
        self._inbox: queue.Queue[tuple[str, str]] = queue.Queue()
        self._last_read: dict[str, float] = {}
        self._availability: dict[str, bool] = {}
        self._due: dict[str, float] = {}
        self._info: str | None = None

    # -- topics ----------------------------------------------------------------------------

    def _topic(self, *levels: str) -> str:
        return "/".join((self.config.prefix, *levels))

    def _publish(self, topic: str, payload: Any, *, retain: bool = True) -> None:
        self.transport.publish(topic, json.dumps(payload, ensure_ascii=False), retain=retain)

    def _respond(
        self,
        action: str,
        *,
        data: Any = None,
        error: str | None = None,
        transaction: Any = None,
    ) -> None:
        payload: dict[str, Any] = {"status": "error" if error else "ok", "data": data or {}}
        if error:
            payload["error"] = error
        if transaction is not None:
            payload["transaction"] = transaction
        self._publish(self._topic("bridge", "response", action), payload, retain=False)

    # -- life cycle ------------------------------------------------------------------------

    def start(self) -> None:
        """Subscribe to commands and requests, and announce the bridge."""
        self.transport.subscribe(self._topic("+", "set"), self._enqueue)
        self.transport.subscribe(self._topic("bridge", "request", "#"), self._enqueue)
        self.announce()
        self.publish_info()

    def announce(self) -> None:
        """Say the bridge is online. Call it again after each MQTT reconnection."""
        self._publish(self._topic("bridge", "state"), {"state": "online"})

    def stop(self) -> None:
        """Say the bridge is gone. The broker's last will covers a crash."""
        self._publish(self._topic("bridge", "state"), {"state": "offline"})

    def run(self, stop: threading.Event, *, idle: float = 1.0) -> None:
        """Serve until ``stop`` is set: handle queued messages, poll when due."""
        self.start()
        try:
            while not stop.is_set():
                self.step(idle)
        finally:
            self.stop()

    def step(self, timeout: float = 0.0) -> None:
        """One turn of the loop: the queued messages, then whatever read is due."""
        try:
            topic, payload = (
                self._inbox.get(timeout=timeout) if timeout else self._inbox.get_nowait()
            )
        except queue.Empty:
            pass
        else:
            self.handle_message(topic, payload)
            while True:
                try:
                    topic, payload = self._inbox.get_nowait()
                except queue.Empty:
                    break
                self.handle_message(topic, payload)
        self.poll()

    def _enqueue(self, topic: str, payload: str) -> None:
        self._inbox.put((topic, payload))

    # -- channel health --------------------------------------------------------------------

    def _usable(self, channel: Channel) -> bool:
        status = self.status.get(channel)
        return (
            status is not None
            and status.state != "unauthenticated"
            and self._clock() >= status.next_try
        )

    def _attempt(self, channel: Channel, interval: float, call: Callable[[], Any]) -> Any:
        """Run a channel call, recording its outcome. Returns ``None`` when it failed."""
        status = self.status[channel]
        try:
            result = call()
        except AuthenticationError as err:
            status.state, status.error = "unauthenticated", str(err)
            _LOGGER.error("%s channel: credentials refused, channel paused", channel.value)
            return None
        except ChannelError as err:
            status.failures += 1
            status.state, status.error = "error", str(err)
            status.next_try = self._clock() + min(interval * 2**status.failures, MAX_BACKOFF)
            _LOGGER.warning("%s channel: %s", channel.value, err)
            return None
        status.state, status.error, status.failures = "ok", None, 0
        status.last_ok, status.next_try = self._clock(), 0.0
        return result

    def replace_channel(
        self, channel: Channel, client: SharingSource | LanSource | DeveloperSource
    ) -> None:
        """Swap in a rebuilt channel client (new session or key) and resume it."""
        if channel not in self.status:
            raise ValueError(f"the bridge was started without a {channel.value} channel")
        if channel is Channel.SHARING:
            self.sharing = client  # type: ignore[assignment]
            self.gate.replace(sharing=self.sharing)
        elif channel is Channel.LAN:
            self.lan = client  # type: ignore[assignment]
        else:
            self.developer = client  # type: ignore[assignment]
            self.gate.replace(developer=self.developer)
        self.status[channel] = ChannelStatus()
        self._due.pop(channel.value, None)

    def publish_info(self) -> None:
        """Publish ``bridge/info`` when it changed."""
        info = {
            "version": self.config.bridge_version,
            "library_version": __version__,
            "contract_version": dialect.CONTRACT_VERSION,
            "writes_enabled": self.gate.enabled,
            "channels": {
                channel.value: (
                    {"state": "absent"}
                    if channel not in self.status
                    else {
                        "state": self.status[channel].state,
                        "error": self.status[channel].error,
                        "last_ok": _iso(self.status[channel].last_ok),
                    }
                )
                for channel in Channel
            },
            "routes": self.router.describe(dps.PRODUCT_FP_THERMOSTAT),
        }
        # ``last_ok`` moves at every read: it is published, but does not by itself make the
        # bridge publish again.
        signature = json.dumps(
            [info["writes_enabled"], [(s.state, s.error) for s in self.status.values()]]
        )
        if signature != self._info:
            self._info = signature
            self._publish(self._topic("bridge", "info"), info)

    # -- discovery -------------------------------------------------------------------------

    def discover(self) -> bool:
        """List the supported thermostats and publish ``bridge/devices``. False when it failed."""
        found: list[Device] | None = None
        inventory: dict[str, Device] = {}
        if self.developer is not None and self._usable(Channel.DEVELOPER):
            listed = self._attempt(
                Channel.DEVELOPER, self.config.developer_interval, self.developer.lan_inventory
            )
            if listed is not None:
                inventory = {device.dev_id: device for device in listed.devices}
        if self.sharing is not None:
            if self._usable(Channel.SHARING):
                found = self._attempt(
                    Channel.SHARING, self.config.sharing_interval, self.sharing.discover
                )
        elif inventory:
            found = list(inventory.values())
        if found is None:
            return False
        devices: dict[str, Device] = {}
        for device in found:
            if not device.supported:
                continue
            known = inventory.get(device.dev_id)
            if known is not None:
                device = dataclasses.replace(
                    device,
                    gateway_id=device.gateway_id or known.gateway_id,
                    node_id=device.node_id or known.node_id,
                )
            devices[device.dev_id] = device
        for dev_id in set(self.devices) - set(devices):
            self._set_availability(dev_id, False)
            self.states.pop(dev_id, None)
        self.devices = devices
        names = dialect.friendly_names(list(devices.values()))
        for dev_id, name in self.config.names.items():
            if dev_id in names:
                names[dev_id] = dialect.friendly_name(name, names[dev_id])
        if len(set(names.values())) != len(names):
            names = dialect.friendly_names(
                [dataclasses.replace(d, name=names[d.dev_id]) for d in devices.values()]
            )
        self.names = names
        for dev_id, device in devices.items():
            state = self.states.setdefault(dev_id, DeviceState(device))
            state.device = device
        self._publish(
            self._topic("bridge", "devices"),
            [
                dialect.encode_descriptor(device, names[dev_id])
                for dev_id, device in sorted(devices.items(), key=lambda item: names[item[0]])
            ],
        )
        return True

    def _spec(self, dev_id: str) -> DeviceSpec | None:
        return None if self.sharing is None else self.sharing.spec(dev_id)

    def _device_named(self, name: Any) -> Device:
        for dev_id, known in self.names.items():
            if known == name:
                return self.devices[dev_id]
        raise PilotWireError(f"unknown device {name!r}")

    # -- reads -----------------------------------------------------------------------------

    def _is_due(self, what: str, interval: float) -> bool:
        now = self._clock()
        if now < self._due.get(what, 0.0):
            return False
        self._due[what] = now + interval
        return True

    def poll(self) -> None:
        """Run the reads that are due, then publish what they brought."""
        now = self._clock()
        if now >= self._due.get("discovery", 0.0):
            retry = min(self.config.sharing_interval, self.config.discovery_interval)
            self._due["discovery"] = now + (
                self.config.discovery_interval if self.discover() else retry
            )
        read: set[str] = set()
        devices = list(self.devices.values())
        if devices:
            if (
                self.sharing is not None
                and self._usable(Channel.SHARING)
                and self._is_due("sharing", self.config.sharing_interval)
            ):
                sharing = self.sharing
                read |= self._apply(
                    Channel.SHARING,
                    self._attempt(
                        Channel.SHARING,
                        self.config.sharing_interval,
                        lambda: sharing.read([device.dev_id for device in devices]),
                    ),
                )
            if (
                self.lan is not None
                and self._usable(Channel.LAN)
                and self._is_due("lan", self.config.lan_interval)
            ):
                lan = self.lan
                read |= self._apply(
                    Channel.LAN,
                    self._attempt(Channel.LAN, self.config.lan_interval, lambda: lan.read(devices)),
                )
            if (
                self.developer is not None
                and self._usable(Channel.DEVELOPER)
                and self._is_due("developer", self.config.developer_interval)
            ):
                developer = self.developer
                read |= self._apply(
                    Channel.DEVELOPER,
                    self._attempt(
                        Channel.DEVELOPER,
                        self.config.developer_interval,
                        lambda: [developer.read(device) for device in devices],
                    ),
                )
        for dev_id in sorted(read):
            self.publish_state(dev_id)
        self._refresh_availability()
        self.publish_info()

    def _apply(self, channel: Channel, readings: list[Reading] | None) -> set[str]:
        """Merge readings, keeping from each only the keys proven for its channel."""
        applied: set[str] = set()
        for reading in readings or []:
            state = self.states.get(reading.dev_id)
            if state is None:
                continue
            keys = self.router.read_keys(state.device, channel)
            trusted = {key: value for key, value in reading.values.items() if key in keys}
            if not trusted and reading.online is None:
                continue
            state.apply(dataclasses.replace(reading, values=trusted))
            self._last_read[reading.dev_id] = reading.at
            applied.add(reading.dev_id)
        return applied

    def publish_state(self, dev_id: str) -> None:
        """Publish the state object of one device."""
        state = self.states[dev_id]
        self._publish(
            self._topic(self.names[dev_id]), dialect.encode_state(state, self._spec(dev_id))
        )

    def _set_availability(self, dev_id: str, online: bool) -> None:
        if self._availability.get(dev_id) is online or dev_id not in self.names:
            return
        self._availability[dev_id] = online
        self._publish(
            self._topic(self.names[dev_id], "availability"), dialect.encode_availability(online)
        )

    def _refresh_availability(self) -> None:
        now = self._clock()
        for dev_id, state in self.states.items():
            last = self._last_read.get(dev_id)
            fresh = last is not None and now - last <= self.config.stale_after
            # Without the sharing API nothing tells whether the device is online: a fresh
            # reading is the only evidence.
            online = (
                state.online is True
                if Channel.SHARING in self.status
                else state.online is not False
            )
            self._set_availability(dev_id, fresh and online)

    # -- commands --------------------------------------------------------------------------

    def handle_message(self, topic: str, payload: str) -> None:
        """Dispatch one inbound message. Failures are answered on the response topic."""
        levels = topic.split("/")
        prefix = self.config.prefix.split("/")
        if levels[: len(prefix)] != prefix:
            return
        rest = levels[len(prefix) :]
        if len(rest) >= 3 and rest[:2] == ["bridge", "request"]:
            self.handle_request("/".join(rest[2:]), payload)
        elif len(rest) == 2 and rest[1] == "set" and rest[0] != "bridge":
            self.handle_set(rest[0], payload)

    @staticmethod
    def _object(raw: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as err:
            raise PilotWireError("payload is not valid JSON") from err
        if not isinstance(payload, dict):
            raise PilotWireError("payload is not a JSON object")
        return payload

    def handle_set(self, name: str, raw: str) -> None:
        """Execute a ``<name>/set`` payload through the write gate and answer."""
        transaction: Any = None
        results: list[dict[str, Any]] = []
        data: dict[str, Any] = {"device": name, "results": results}
        try:
            payload = self._object(raw)
            transaction = payload.pop("transaction", None)
            device = self._device_named(name)
            commands = dialect.decode_command(payload)
            spec = self._spec(device.dev_id)
            planned: list[tuple[dialect.Command, PlannedWrite]] = [
                (command, self.gate.plan(device, spec, command.key, command.value))
                for command in commands
            ]
        except PilotWireError as err:
            self._respond("device/set", data=data, error=str(err), transaction=transaction)
            return
        error: str | None = None
        touched = False
        for command, plan in planned:
            entry: dict[str, Any] = {
                "property": command.property,
                "value": payload[command.property],
            }
            results.append(entry)
            try:
                result = self.gate.execute(plan)
            except PilotWireError as err:
                entry["confirmed"] = False
                error = f"{command.property}: {err}"
                break
            entry["confirmed"] = result.confirmed
            if result.reading is not None:
                touched = bool(self._apply(result.channel, [result.reading])) or touched
            if not result.confirmed:
                error = f"{command.property}: not confirmed, {result.detail}"
                break
        if touched:
            self.publish_state(device.dev_id)
            self._refresh_availability()
        self._respond("device/set", data=data, error=error, transaction=transaction)

    def handle_request(self, action: str, raw: str) -> None:
        """Serve ``bridge/request/<action>``: ``schedule/get`` and ``schedule/set``."""
        transaction: Any = None
        try:
            payload = self._object(raw)
            transaction = payload.get("transaction")
            if action == "schedule/get":
                device = self._device_named(payload.get("device"))
                self.router.route(device, Operation.READ_SCHEDULE)
                assert self.developer is not None
                week = self.developer.read_schedule(device.dev_id)
                self._respond(action, data=week, transaction=transaction)
            elif action == "schedule/set":
                device = self._device_named(payload.get("device"))
                result = self.gate.write_schedule(device, payload.get("days"))  # type: ignore[arg-type]
                self._respond(
                    action,
                    data=result.week,
                    error=None if result.confirmed else f"not confirmed: {result.detail}",
                    transaction=transaction,
                )
            else:
                raise RouteRefused(f"unknown request {action!r}")
        except PilotWireError as err:
            with contextlib.suppress(Exception):
                self._respond(action, error=str(err), transaction=transaction)
