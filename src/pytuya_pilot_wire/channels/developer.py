"""Developer API channel: a project of the Tuya IoT platform, device shadow properties.

The only channel that returns the weekly schedule; it also gives the eco, frost-protection and
holiday setpoints and the night mode, and the LAN inventory (local key of the gateway, node id
of each sub-device). It needs the access id and secret of the user's developer project, with the
Smart Life account linked to it and an active IoT Core subscription.

Writes are limited to the weekly-schedule data points, the only ones proven on this channel
(2026-11-14), and are reserved to the write gate.

Requires the ``developer`` extra.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .. import dps, schedule
from ..errors import AuthenticationError, ChannelError, ChannelUnavailable
from ..model import Channel, Device, Reading

PATH_PROPERTIES = "/v2.0/cloud/thing/{dev_id}/shadow/properties"
PATH_ISSUE = "/v2.0/cloud/thing/{dev_id}/shadow/properties/issue"

_ID = re.compile(r"[A-Za-z0-9]{6,40}")
_SCHEDULE_CODE = re.compile(r"week_program_18_[1-7]_[1-4]")
# Tuya platform codes that mean the project's credentials or subscription are the problem.
_AUTH_CODES = frozenset({"1004", "1010", "1011", "1100", "1106", "2406", "28841002", "28841101"})


@dataclass(frozen=True)
class DeveloperProject:
    """Credentials of a Tuya IoT platform project. ``repr`` hides them."""

    access_id: str = field(repr=False)
    access_secret: str = field(repr=False)
    region: str = "eu"


@dataclass(frozen=True)
class GatewayKey:
    """Local key of a gateway, as the developer project returns it. ``repr`` hides the key."""

    dev_id: str
    name: str
    local_key: str = field(repr=False)


@dataclass(frozen=True)
class LanInventory:
    """What the LAN channel needs: gateway keys, and the sub-devices with their node ids."""

    gateways: tuple[GatewayKey, ...]
    devices: tuple[Device, ...]


def _tinytuya_cloud(project: DeveloperProject) -> Any:  # pragma: no cover - real network
    try:
        import tinytuya
    except ImportError as err:
        raise ChannelUnavailable("tinytuya is not installed (extra 'developer')") from err
    return tinytuya.Cloud(
        apiRegion=project.region, apiKey=project.access_id, apiSecret=project.access_secret
    )


def _schedule_parts(properties: Mapping[str, Any]) -> dict[str, str]:
    return {
        code: value
        for code, value in properties.items()
        if _SCHEDULE_CODE.fullmatch(code) and isinstance(value, str) and value
    }


def _check_id(dev_id: str) -> str:
    if not _ID.fullmatch(dev_id):
        raise ChannelError("invalid device id")
    return dev_id


class DeveloperChannel:
    """Shadow properties of a device, through the developer project."""

    channel = Channel.DEVELOPER

    def __init__(
        self,
        project: DeveloperProject,
        *,
        cloud_factory: Callable[[DeveloperProject], Any] = _tinytuya_cloud,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._project = project
        self._factory = cloud_factory
        self._clock = clock
        self._cloud: Any = None

    def _client(self) -> Any:
        if self._cloud is None:
            cloud = self._factory(self._project)
            error = getattr(cloud, "error", None)
            if error:
                raise AuthenticationError(
                    "the developer project refused the connection (access id, secret or region)"
                )
            self._cloud = cloud
        return self._cloud

    @staticmethod
    def _checked(response: Any) -> Mapping[str, Any]:
        if not isinstance(response, Mapping):
            raise ChannelUnavailable("no usable answer from the developer API")
        if "Error" in response:  # tinytuya's own error envelope (network, bad JSON)
            raise ChannelUnavailable(f"developer API: {str(response.get('Error'))[:80]}")
        if not response.get("success", True):
            code, message = str(response.get("code")), str(response.get("msg"))[:120]
            if code in _AUTH_CODES:
                raise AuthenticationError(f"developer API: {message}", code)
            raise ChannelError(f"developer API: {message}", code)
        return response

    # -- reads -----------------------------------------------------------------------------

    def properties(self, dev_id: str) -> dict[str, Any]:
        """Raw shadow properties of a device, ``{code: value}``."""
        response = self._checked(
            self._client().cloudrequest(PATH_PROPERTIES.format(dev_id=_check_id(dev_id)))
        )
        result = response.get("result")
        listed = result.get("properties") if isinstance(result, Mapping) else None
        return {
            entry["code"]: entry["value"]
            for entry in listed or []
            if isinstance(entry, Mapping) and "code" in entry and "value" in entry
        }

    def read(self, device: Device) -> Reading:
        """Decoded properties of a supported device.

        The shadow holds the last value the cloud received for each data point, however old:
        prefer the other channels for what changes often.
        """
        return self.read_with_schedule(device)[0]

    def read_with_schedule(
        self, device: Device
    ) -> tuple[Reading, dict[str, list[schedule.Transition]]]:
        """The decoded properties and the weekly schedule, from **one** call to the API.

        The shadow returns both in the same answer: a reader that wants the schedule along with
        the settings pays one request, not two. A day with a missing part is absent.
        """
        properties = self.properties(device.dev_id)
        reading = Reading(
            dev_id=device.dev_id,
            channel=Channel.DEVELOPER,
            at=self._clock(),
            values=dps.decode_codes(device.product_id, properties),
        )
        return reading, schedule.decode_week(_schedule_parts(properties))

    def schedule_parts(self, dev_id: str) -> dict[str, str]:
        """The raw weekly-schedule data points of a device, ``{code: base64}``."""
        return _schedule_parts(self.properties(dev_id))

    def read_schedule(self, dev_id: str) -> dict[str, list[schedule.Transition]]:
        """The weekly schedule, ``{day name: transitions}``. A day with a missing part is absent."""
        return schedule.decode_week(self.schedule_parts(dev_id))

    def lan_inventory(self) -> LanInventory:
        """Gateway keys and sub-device node ids, for the LAN channel.

        The gateway's address is not part of it: the cloud only knows the public one.
        """
        listed = self._client().getdevices(verbose=False)
        if isinstance(listed, Mapping):
            self._checked(listed)
            raise ChannelError("developer API: unexpected device list")
        if not isinstance(listed, list):
            raise ChannelError("developer API: unexpected device list")
        gateways: list[GatewayKey] = []
        devices: list[Device] = []
        for item in listed:
            if not isinstance(item, Mapping) or not item.get("id"):
                continue
            if item.get("sub") and item.get("node_id"):
                devices.append(
                    Device(
                        dev_id=str(item["id"]),
                        name=str(item.get("name") or "").strip(),
                        product_id=str(item.get("product_id") or ""),
                        category=str(item.get("category") or ""),
                        gateway_id=item.get("gateway_id") or item.get("parent") or None,
                        node_id=str(item["node_id"]),
                    )
                )
            elif not item.get("sub") and item.get("key"):
                gateways.append(
                    GatewayKey(
                        str(item["id"]), str(item.get("name") or "").strip(), str(item["key"])
                    )
                )
        return LanInventory(tuple(gateways), tuple(devices))

    # -- write (used by the write gate only) -----------------------------------------------

    def issue_schedule_parts(self, dev_id: str, parts: Mapping[str, str]) -> None:
        """Write weekly-schedule data points ``{code: base64}``. No retry, ever.

        Reserved to :class:`~pytuya_pilot_wire.gate.WriteGate`. Any code other than a schedule
        part is refused here again, whatever the caller validated.
        """
        if not parts:
            raise ChannelError("nothing to write")
        for code, value in parts.items():
            if not _SCHEDULE_CODE.fullmatch(code):
                raise ChannelError(f"property {code!r} is not a weekly-schedule part")
            schedule.decode_part(value)
        response = self._client().cloudrequest(
            PATH_ISSUE.format(dev_id=_check_id(dev_id)),
            post={"properties": json.dumps(dict(parts))},
        )
        self._checked(response)
