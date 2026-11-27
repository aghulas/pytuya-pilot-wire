"""Tuya pilot-wire thermostats: data points, codecs, device model and channels.

No Home Assistant import. The channel clients need optional dependencies (extras ``sharing``,
``lan``, ``developer``), imported only when a channel is used.
"""

from .errors import (
    AuthenticationError,
    ChannelError,
    ChannelUnavailable,
    DialectError,
    PilotWireError,
    RouteRefused,
    WriteRefused,
)
from .model import Bounds, Channel, Device, DeviceSpec, DeviceState, Reading, Sourced

__version__ = "0.2.0"

__all__ = [
    "AuthenticationError",
    "Bounds",
    "Channel",
    "ChannelError",
    "ChannelUnavailable",
    "Device",
    "DeviceSpec",
    "DeviceState",
    "DialectError",
    "PilotWireError",
    "Reading",
    "RouteRefused",
    "Sourced",
    "WriteRefused",
    "__version__",
]
