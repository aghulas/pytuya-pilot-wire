"""Device-sharing API channel: the Smart Life account, through ``tuya-device-sharing-sdk``.

This is the API Home Assistant's own ``tuya`` integration uses. The session comes from a QR-code
login (:func:`request_qr_code`, :func:`poll_login`) and is **supplied by the caller**, who also
persists the token each time the SDK renews it (the refresh token rotates: a renewal that is not
saved kills the session).

Guards:

- the SDK's HTTP session is replaced by :class:`GuardedSession`: allow-list of routes (the
  command route is open only during :meth:`SharingChannel.send`), request counters, minimum
  interval between requests;
- nothing but the fields of :class:`~pytuya_pilot_wire.model.Device` leaves the channel: the
  local key, address and account ids the API returns are dropped;
- the SDK logger, whose debug output contains decrypted responses, is capped at ``WARNING``.

Requires the ``sharing`` extra.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .. import dps
from ..errors import AuthenticationError, ChannelError, ChannelUnavailable
from ..model import Channel, Device, DeviceSpec, Reading

CLIENT_ID = "HA_3y9q4ak7g4ephrvke"  # application id of the sharing SDK (public, Home Assistant's)
SCHEMA = "haauthorize"
QR_PREFIX = "tuyaSmart--qrLogin?token="

_ID = r"[A-Za-z0-9]{6,40}"
PATH_DETAIL = "/v1.0/m/life/ha/devices/detail"
ALLOWED_GET = (
    re.compile(r"^/v1\.0/m/life/users/homes$"),
    re.compile(r"^/v1\.0/m/life/ha/home/devices$"),
    re.compile(r"^/v1\.0/m/life/ha/devices/detail$"),
    re.compile(rf"^/v1\.1/m/life/{_ID}/specifications$"),
    re.compile(rf"^/v1\.0/m/life/devices/{_ID}/status$"),
    re.compile(rf"^/v1\.0/m/life/ha/{_ID}/code/custom-type$"),
    re.compile(rf"^/v1\.0/m/life/ha/{_ID}/dp-report-types$"),
    re.compile(r"^/v1\.0/m/token/[A-Za-z0-9._-]{6,200}$"),  # token renewal by the SDK
)
COMMANDS = re.compile(rf"^/v1\.1/m/thing/{_ID}/commands$")
_ID_SEGMENT = re.compile(
    rf"/{_ID}(?=/(?:specifications|status|code/custom-type|dp-report-types|commands)$)"
)
MIN_INTERVAL = 0.25
DETAIL_BATCH = 20

_AUTH_CODES = {"1010": "token refused", "1011": "token expired"}
_MESSAGES = {
    "1013": "timestamp refused: the clock of this machine is off",
    "1106": "permission denied by Tuya",
    "2008": "command not supported by this device",
}

logging.getLogger("tuya_sharing").setLevel(logging.WARNING)

Transport = Callable[[str, str, dict[str, Any]], Any]


def check_route(method: str, path: str, writing: bool = False) -> None:
    """Raise :class:`ChannelError` unless the request is on the allow-list."""
    if method == "GET" and any(pattern.match(path) for pattern in ALLOWED_GET):
        return
    if writing and method == "POST" and COMMANDS.match(path):
        return
    raise ChannelError(f"route refused by the allow-list: {method} {anonymise(path)}")


def anonymise(path: str) -> str:
    """A path with no device id and no token, for counters and messages."""
    if path.startswith("/v1.0/m/token/"):
        return "/v1.0/m/token/{refresh}"
    return _ID_SEGMENT.sub("/{id}", path)


@dataclass(frozen=True)
class SharingSession:
    """A Smart Life session, as the QR-code login returns it. Never logged: ``repr`` hides it."""

    user_code: str = field(repr=False)
    terminal_id: str = field(repr=False)
    endpoint: str
    token_info: Mapping[str, Any] = field(repr=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SharingSession:
        """Validate a stored session. Raises :class:`AuthenticationError` when it is unusable."""
        missing = [
            k for k in ("user_code", "terminal_id", "endpoint", "token_info") if not data.get(k)
        ]
        token = data.get("token_info") or {}
        if missing or not (token.get("access_token") and token.get("refresh_token")):
            raise AuthenticationError(
                f"sharing session incomplete ({', '.join(missing) or 'token_info'}): log in again"
            )
        if not str(data["endpoint"]).startswith("https://"):
            raise AuthenticationError("sharing session: unexpected endpoint")
        return cls(data["user_code"], data["terminal_id"], data["endpoint"], dict(token))

    def to_dict(self) -> dict[str, Any]:
        """The session as a JSON-ready mapping, for the caller to store with care."""
        return {
            "user_code": self.user_code,
            "terminal_id": self.terminal_id,
            "endpoint": self.endpoint,
            "token_info": dict(self.token_info),
        }


_TOKEN_KEYS = ("t", "uid", "expire_time", "access_token", "refresh_token")


class GuardedSession:
    """The HTTP session imposed on the SDK: allow-list, counters, pacing.

    It offers the two methods the SDK calls, ``request`` and ``close``. ``transport`` stands in
    for the network in tests.
    """

    def __init__(
        self,
        counters: Counter[str],
        transport: Transport | None = None,
        min_interval: float = MIN_INTERVAL,
    ) -> None:
        self.counters = counters
        self.writing = False
        self._transport = transport
        self._min_interval = min_interval
        self._last: float | None = None
        self._lock = threading.Lock()
        self._inner: Any = None

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        verb, path = method.upper(), urlsplit(url).path
        check_route(verb, path, self.writing)
        self.counters[f"{verb} {anonymise(path)}"] += 1
        with self._lock:
            if self._last is not None:
                wait = self._min_interval - (time.monotonic() - self._last)
                if wait > 0:
                    time.sleep(wait)
            self._last = time.monotonic()
        if self._transport is not None:
            return self._transport(verb, url, kwargs)
        if self._inner is None:  # pragma: no cover - real network
            import requests

            self._inner = requests.Session()
        return self._inner.request(method, url, **kwargs)  # pragma: no cover - real network

    def close(self) -> None:
        if self._inner is not None:  # pragma: no cover - real network
            self._inner.close()


def _sdk_manager(
    session: SharingSession, on_token: Callable[[dict[str, Any]], None]
) -> Any:  # pragma: no cover - needs the SDK's signed transport
    try:
        from tuya_sharing import Manager, SharingTokenListener
    except ImportError as err:
        raise ChannelUnavailable(
            "tuya-device-sharing-sdk is not installed (extra 'sharing')"
        ) from err

    class _Listener(SharingTokenListener):  # type: ignore[misc]
        def update_token(self, token_info: dict[str, Any]) -> None:
            on_token(token_info)

    return Manager(
        CLIENT_ID,
        session.user_code,
        session.terminal_id,
        session.endpoint,
        dict(session.token_info),
        _Listener(),
    )


def _device(item: Mapping[str, Any]) -> Device:
    return Device(
        dev_id=str(item["id"]),
        name=str(item.get("name") or "").strip(),
        product_id=str(item.get("product_id") or ""),
        category=str(item.get("category") or ""),
        gateway_id=item.get("gateway_id") or None,
        node_id=item.get("node_id") or None,
        online=item.get("online") if isinstance(item.get("online"), bool) else None,
    )


def _status(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    return {
        entry["code"]: entry["value"]
        for entry in raw or []
        if isinstance(entry, Mapping) and "code" in entry and "value" in entry
    }


class SharingChannel:
    """Reads, and guarded sends, through the device-sharing API."""

    channel = Channel.SHARING

    def __init__(
        self,
        session: SharingSession,
        *,
        on_token: Callable[[SharingSession], None] | None = None,
        transport: Transport | None = None,
        manager_factory: Callable[
            [SharingSession, Callable[[dict[str, Any]], None]], Any
        ] = _sdk_manager,
        min_interval: float = MIN_INTERVAL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.session = session
        self.counters: Counter[str] = Counter()
        self.last_error: str | None = None
        self._on_token = on_token
        self._transport = transport
        self._factory = manager_factory
        self._min_interval = min_interval
        self._clock = clock
        self._manager: Any = None
        self._guard: GuardedSession | None = None
        self._specs: dict[str, DeviceSpec] = {}
        self._write_lock = threading.Lock()

    # -- session ---------------------------------------------------------------------------

    def _token_renewed(self, token_info: dict[str, Any]) -> None:
        self.session = SharingSession(
            self.session.user_code,
            self.session.terminal_id,
            self.session.endpoint,
            {key: token_info.get(key) for key in _TOKEN_KEYS},
        )
        if self._on_token is not None:
            self._on_token(self.session)

    def _api(self) -> Any:
        if self._manager is None:
            manager = self._factory(self.session, self._token_renewed)
            self._guard = GuardedSession(self.counters, self._transport, self._min_interval)
            manager.customer_api.session = self._guard
            self._manager = manager
        return self._manager

    def _call(self, function: Callable[..., Any], *args: Any) -> Any:
        try:
            result = function(*args)
        except ChannelError as err:
            self.last_error = str(err)
            raise
        except Exception as err:
            failure = self._translate(err)
            self.last_error = str(failure)
            raise failure from err
        self.last_error = None
        return result

    @staticmethod
    def _translate(err: Exception) -> ChannelError:
        code = getattr(err, "error_code", None)
        if code is not None:
            code = str(code)
            if code in _AUTH_CODES:
                return AuthenticationError(f"{_AUTH_CODES[code]}: log in again", code)
            message = _MESSAGES.get(code) or getattr(err, "error_message", None) or "error"
            return ChannelError(f"Tuya: {message} (code {code})", code)
        if type(err).__module__.split(".")[0] in ("requests", "urllib3") or isinstance(
            err, OSError
        ):
            return ChannelUnavailable(f"network: {type(err).__name__}")
        return ChannelError(f"unexpected answer from Tuya ({type(err).__name__})")

    # -- reads -----------------------------------------------------------------------------

    def discover(self) -> list[Device]:
        """Every device of the account, with its instruction set kept for :meth:`spec`.

        Costly (about five requests per device): call it at start and rarely afterwards.
        """
        manager = self._api()
        self._call(manager.update_device_cache)
        devices: list[Device] = []
        for raw in manager.device_map.values():
            device = _device(vars(raw))
            devices.append(device)
            self._specs[device.dev_id] = DeviceSpec.from_instruction_set(
                device.product_id,
                [vars(f) for f in (getattr(raw, "function", None) or {}).values()],
                [vars(s) for s in (getattr(raw, "status_range", None) or {}).values()],
            )
        return devices

    def spec(self, dev_id: str) -> DeviceSpec | None:
        """Instruction set of a device seen by :meth:`discover`, else ``None``."""
        return self._specs.get(dev_id)

    def read(self, dev_ids: Sequence[str]) -> list[Reading]:
        """Current status of the given devices: one request per batch of twenty.

        A device the account no longer lists, or whose product has no table, gives no reading.
        """
        for dev_id in dev_ids:
            if not re.fullmatch(_ID, dev_id):
                raise ChannelError("invalid device id")
        api = self._api().customer_api
        readings: list[Reading] = []
        for start in range(0, len(dev_ids), DETAIL_BATCH):
            batch = dev_ids[start : start + DETAIL_BATCH]
            response = self._call(api.get, PATH_DETAIL, {"devIds": ",".join(batch)})
            if not isinstance(response, Mapping) or not response.get("success"):
                failure = ChannelError("no usable answer from Tuya (HTTP error)")
                self.last_error = str(failure)
                raise failure
            at = self._clock()
            for item in response.get("result") or []:
                product_id = str(item.get("product_id") or "")
                if product_id not in dps.PRODUCTS or item.get("id") not in batch:
                    continue
                readings.append(
                    Reading(
                        dev_id=str(item["id"]),
                        channel=Channel.SHARING,
                        at=at,
                        values=dps.decode_codes(product_id, _status(item.get("status"))),
                        online=item.get("online") if isinstance(item.get("online"), bool) else None,
                    )
                )
        return readings

    # -- write (used by the write gate only) -----------------------------------------------

    def send(self, dev_id: str, commands: Sequence[Mapping[str, Any]]) -> None:
        """POST raw ``{"code", "value"}`` commands to one device. No retry, ever.

        Reserved to :class:`~pytuya_pilot_wire.gate.WriteGate`, which validates before calling. The
        API is called directly, not through the SDK's duplicate filter, which silently drops an
        identical command sent within ten seconds (restoring a value would fall to it).
        """
        if not re.fullmatch(_ID, dev_id):
            raise ChannelError("invalid device id")
        if not commands or any(set(command) != {"code", "value"} for command in commands):
            raise ChannelError("malformed command")
        path = f"/v1.1/m/thing/{dev_id}/commands"
        api = self._api().customer_api
        assert self._guard is not None
        with self._write_lock:
            self._guard.writing = True
            try:
                response = self._call(
                    api.post, path, None, {"commands": [dict(command) for command in commands]}
                )
            finally:
                self._guard.writing = False
        if not isinstance(response, Mapping):
            raise ChannelError("no usable answer from Tuya (HTTP error)")
        if not response.get("success"):
            raise ChannelError(
                f"command refused by Tuya: {response.get('msg')}", str(response.get("code"))
            )

    def close(self) -> None:
        if self._guard is not None:
            self._guard.close()


# -- QR-code login -----------------------------------------------------------------------------


def _login_control() -> Any:  # pragma: no cover - real network
    try:
        from tuya_sharing import LoginControl
    except ImportError as err:
        raise ChannelUnavailable(
            "tuya-device-sharing-sdk is not installed (extra 'sharing')"
        ) from err
    return LoginControl()


def request_qr_code(user_code: str, *, control: Any = None) -> str:
    """Ask Tuya for a login token; returns the text to show as a QR code.

    ``user_code`` is the "user code" of the Smart Life app (Me > Settings > Account and security).
    The user scans the QR code with the app, then :func:`poll_login` returns the session.
    """
    control = control or _login_control()
    response = control.qr_code(CLIENT_ID, SCHEMA, user_code)
    if not response.get("success"):
        raise AuthenticationError(
            f"Tuya refused the QR-code request: {response.get('msg')}", str(response.get("code"))
        )
    return QR_PREFIX + str(response["result"]["qrcode"])


def poll_login(qr_text: str, user_code: str, *, control: Any = None) -> SharingSession | None:
    """One poll of a QR-code login: the session once the user confirmed, else ``None``."""
    control = control or _login_control()
    done, info = control.login_result(qr_text.removeprefix(QR_PREFIX), CLIENT_ID, user_code)
    if not done:
        return None
    return SharingSession.from_dict(
        {
            "user_code": user_code,
            "terminal_id": info.get("terminal_id"),
            "endpoint": info.get("endpoint"),
            "token_info": {key: info.get(key) for key in _TOKEN_KEYS},
        }
    )
