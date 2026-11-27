"""Errors raised by the library."""

from __future__ import annotations


class PilotWireError(Exception):
    """Base class of every error raised by this library."""


class ChannelError(PilotWireError):
    """A channel answered with an error. ``code`` is the vendor's error code when there is one."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class ChannelUnavailable(ChannelError):
    """The channel cannot be used now: dependency missing, network down, gateway silent."""


class AuthenticationError(ChannelError):
    """The channel refused the credentials: the caller has to supply a new session or key."""


class DialectError(PilotWireError):
    """A command payload that the Zigbee2MQTT dialect codec cannot accept."""


class RouteRefused(PilotWireError):
    """No proven route for the operation: nothing is sent."""


class WriteRefused(PilotWireError):
    """The write gate refused the write before sending anything."""
