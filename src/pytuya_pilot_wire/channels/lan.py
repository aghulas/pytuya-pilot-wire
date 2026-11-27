"""LAN channel: Tuya local protocol through the gateway, with ``tinytuya``. **Read only.**

The thermostats are sub-devices: each is addressed by its node id through a persistent connection
to the gateway, opened with the gateway's local key (protocol 3.3, the only version verified on
these gateways). The gateway returns every data point by number, but not all of them on each
read; voltage and current exist only while heating.

No write was ever tried on this channel, so it has no write method.

Requires the ``lan`` extra.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .. import dps
from ..errors import ChannelUnavailable
from ..model import Channel, Device, Reading

PROTOCOL_VERSION = 3.3
DEFAULT_TIMEOUT = 9.0


@dataclass(frozen=True)
class Gateway:
    """A Tuya gateway on the LAN. ``repr`` hides the local key."""

    dev_id: str
    address: str
    local_key: str = field(repr=False)
    version: float = PROTOCOL_VERSION


def _tinytuya_device(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - real network
    try:
        import tinytuya
    except ImportError as err:
        raise ChannelUnavailable("tinytuya is not installed (extra 'lan')") from err
    return tinytuya.Device(*args, **kwargs)


class LanChannel:
    """Reads the sub-devices of one gateway."""

    channel = Channel.LAN

    def __init__(
        self,
        gateway: Gateway,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        device_factory: Callable[..., Any] = _tinytuya_device,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.gateway = gateway
        self.errors: dict[str, str] = {}
        self._timeout = timeout
        self._factory = device_factory
        self._clock = clock

    def read(self, devices: Sequence[Device]) -> list[Reading]:
        """One reading per sub-device that answered.

        Devices of another gateway, without a node id or of an unsupported product are skipped.
        A device that fails is left out and its error kept in :attr:`errors` (by device id); when
        none answers, :class:`ChannelUnavailable` is raised: the gateway is unreachable or the key
        is wrong.
        """
        targets = [
            device
            for device in devices
            if device.node_id
            and device.supported
            and device.gateway_id in (None, self.gateway.dev_id)
        ]
        self.errors = {}
        if not targets:
            return []
        gateway = self._factory(
            self.gateway.dev_id,
            self.gateway.address,
            self.gateway.local_key,
            version=self.gateway.version,
            persist=True,
        )
        readings: list[Reading] = []
        try:
            gateway.set_socketTimeout(self._timeout)
            for device in targets:
                try:
                    sub = self._factory(device.dev_id, cid=device.node_id, parent=gateway)
                    status = sub.status()
                except Exception as err:  # one device must not hide the others
                    self.errors[device.dev_id] = type(err).__name__
                    continue
                values = status.get("dps") if isinstance(status, dict) else None
                if not isinstance(values, dict) or not values:
                    error = status.get("Error") if isinstance(status, dict) else None
                    self.errors[device.dev_id] = str(error or "empty answer")[:60]
                    continue
                readings.append(
                    Reading(
                        dev_id=device.dev_id,
                        channel=Channel.LAN,
                        at=self._clock(),
                        values=dps.decode_lan(device.product_id, values),
                    )
                )
        finally:
            with contextlib.suppress(Exception):
                gateway.close()
        if not readings:
            raise ChannelUnavailable("gateway unreachable or wrong local key: no device answered")
        return readings
