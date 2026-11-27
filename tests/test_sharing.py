"""Device-sharing API channel, against a fake SDK. Synthetic data only."""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest

from pytuya_pilot_wire import dps
from pytuya_pilot_wire.channels import sharing
from pytuya_pilot_wire.channels.sharing import GuardedSession, SharingChannel, SharingSession
from pytuya_pilot_wire.errors import AuthenticationError, ChannelError, ChannelUnavailable
from pytuya_pilot_wire.model import Bounds, Channel

T1, T2, GW = "devsynthetic0001", "devsynthetic0002", "gwsynthetic0001"
SESSION = {
    "user_code": "usercode",
    "terminal_id": "terminal",
    "endpoint": "https://example.invalid",
    "token_info": {
        "t": 1,
        "uid": "u",
        "expire_time": 7200,
        "access_token": "a",
        "refresh_token": "r",
    },
}


class ApiError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        self.error_code, self.error_message = code, message


class FakeApi:
    """Stands in for the SDK's CustomerApi: goes through ``session`` like the real one."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.session: Any = None
        self.answers = answers
        self.calls: list[tuple[str, str, Any]] = []

    def _request(self, method: str, path: str, payload: Any) -> Any:
        self.session.request(method, "https://example.invalid" + path)
        self.calls.append((method, path, payload))
        answer = self.answers.get(path)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get(self, path: str, params: Any = None) -> Any:
        return self._request("GET", path, params)

    def post(self, path: str, params: Any = None, body: Any = None) -> Any:
        return self._request("POST", path, body)


def raw_device(dev_id: str, product: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=dev_id,
        name=name,
        product_id=product,
        category="wk",
        online=True,
        node_id="node" + dev_id[-2:],
        local_key="never-exposed",
        ip="address.invalid",
        function={
            "temp_set": SimpleNamespace(
                code="temp_set", type="Integer", values='{"min":50,"max":400,"scale":1,"step":5}'
            )
        },
        status_range={
            "fault": SimpleNamespace(code="fault", type="Bitmap", values='{"label":["e1"]}')
        },
    )


class FakeManager:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.customer_api = FakeApi(answers)
        self.device_map: dict[str, Any] = {}
        self.listener: Any = None

    def update_device_cache(self) -> None:
        self.customer_api.get("/v1.0/m/life/users/homes")
        self.device_map = {
            T1: raw_device(T1, dps.PRODUCT_FP_THERMOSTAT, " Room 1 "),
            GW: raw_device(GW, "gatewayproduct", "Gateway"),
        }


def channel(
    answers: dict[str, Any] | None = None, **kwargs: Any
) -> tuple[SharingChannel, FakeManager]:
    manager = FakeManager(answers or {})

    def factory(session: SharingSession, on_token: Any) -> FakeManager:
        manager.listener = on_token
        return manager

    made = SharingChannel(
        SharingSession.from_dict(SESSION),
        manager_factory=factory,
        transport=lambda method, url, kwargs: None,
        min_interval=0,
        clock=lambda: 1000.0,
        **kwargs,
    )
    return made, manager


def detail(*items: dict[str, Any]) -> dict[str, Any]:
    return {sharing.PATH_DETAIL: {"success": True, "result": list(items)}}


def item(dev_id: str, product: str = dps.PRODUCT_FP_THERMOSTAT, **extra: Any) -> dict[str, Any]:
    return {
        "id": dev_id,
        "product_id": product,
        "online": True,
        "status": [{"code": "temp_current", "value": 195}, {"code": "mode", "value": "manual"}],
        **extra,
    }


def test_session_validation_and_repr() -> None:
    session = SharingSession.from_dict(SESSION)
    assert session.to_dict() == SESSION
    assert "usercode" not in repr(session) and "terminal" not in repr(session)
    for broken in (
        {**SESSION, "user_code": ""},
        {**SESSION, "token_info": {"access_token": "a"}},
        {**SESSION, "endpoint": "http://example.invalid"},
    ):
        with pytest.raises(AuthenticationError):
            SharingSession.from_dict(broken)


def test_allow_list() -> None:
    sharing.check_route("GET", "/v1.0/m/life/ha/devices/detail")
    sharing.check_route("GET", f"/v1.1/m/life/{T1}/specifications")
    sharing.check_route("POST", f"/v1.1/m/thing/{T1}/commands", writing=True)
    for method, path in (
        ("POST", f"/v1.1/m/thing/{T1}/commands"),
        ("GET", "/v1.0/m/scene/list"),
        ("DELETE", "/v1.0/m/life/ha/devices/detail"),
    ):
        with pytest.raises(ChannelError) as refused:
            sharing.check_route(method, path)
        assert T1 not in str(refused.value)


def test_anonymise() -> None:
    assert sharing.anonymise(f"/v1.1/m/thing/{T1}/commands") == "/v1.1/m/thing/{id}/commands"
    assert sharing.anonymise("/v1.0/m/token/secretrefresh") == "/v1.0/m/token/{refresh}"
    assert sharing.anonymise("/v1.0/m/life/users/homes") == "/v1.0/m/life/users/homes"


def test_guarded_session_counts_and_paces(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(sharing.time, "sleep", slept.append)
    counters: Counter[str] = Counter()
    seen: list[str] = []
    session = GuardedSession(counters, lambda method, url, kwargs: seen.append(url), 60)
    session.request("get", f"https://example.invalid/v1.1/m/life/{T1}/specifications")
    session.request("get", f"https://example.invalid/v1.1/m/life/{T2}/specifications")
    assert counters == {"GET /v1.1/m/life/{id}/specifications": 2}
    assert len(seen) == 2 and len(slept) == 1 and 0 < slept[0] <= 60
    with pytest.raises(ChannelError):
        session.request("POST", f"https://example.invalid/v1.1/m/thing/{T1}/commands")
    session.close()


def test_discover_keeps_only_model_fields() -> None:
    made, _ = channel()
    devices = made.discover()
    assert [(d.dev_id, d.name, d.supported, d.node_id) for d in devices] == [
        (T1, "Room 1", True, "node01"),
        (GW, "Gateway", False, "node01"),
    ]
    assert "never-exposed" not in repr(devices) and "address.invalid" not in repr(devices)
    spec = made.spec(T1)
    assert spec is not None and spec.bounds["setpoint"] == Bounds(5.0, 40.0, 0.5)
    assert spec.fault_labels == ("e1",)
    assert made.spec(T2) is None
    assert made.counters == {"GET /v1.0/m/life/users/homes": 1}


def test_read_decodes_supported_devices_only() -> None:
    made, manager = channel(detail(item(T1), item(GW, "gatewayproduct"), item("devunrequested01")))
    readings = made.read([T1, GW])
    assert len(readings) == 1
    assert readings[0].dev_id == T1 and readings[0].channel is Channel.SHARING
    assert readings[0].values == {"temperature": 19.5, "mode": "manual"}
    assert readings[0].online is True and readings[0].at == 1000.0
    assert manager.customer_api.calls == [("GET", sharing.PATH_DETAIL, {"devIds": f"{T1},{GW}"})]
    assert made.last_error is None


def test_read_accepts_a_status_mapping_and_batches() -> None:
    ids = [f"devsynthetic{n:04d}" for n in range(25)]
    made, manager = channel(detail(item(ids[0], status={"temp_current": 180}, online="yes")))
    readings = made.read(ids)
    assert [len(call[2]["devIds"].split(",")) for call in manager.customer_api.calls] == [20, 5]
    assert readings[0].values == {"temperature": 18.0} and readings[0].online is None


def test_read_failures() -> None:
    with pytest.raises(ChannelError, match="invalid device id"):
        channel()[0].read(["bad id"])
    made, _ = channel({sharing.PATH_DETAIL: None})
    with pytest.raises(ChannelError, match="HTTP error"):
        made.read([T1])
    assert made.last_error is not None


@pytest.mark.parametrize(
    ("error", "expected", "text"),
    [
        (ApiError("1010"), AuthenticationError, "log in again"),
        (ApiError("1011"), AuthenticationError, "expired"),
        (ApiError("-9999999", "sign invalid"), AuthenticationError, "refused (sign invalid"),
        (ApiError("-9999999", "other"), ChannelError, "other (code -9999999)"),
        (ApiError("1106"), ChannelError, "permission denied"),
        (ApiError("9999", "odd"), ChannelError, "odd (code 9999)"),
        (ConnectionError("down"), ChannelUnavailable, "network"),
        (KeyError("result"), ChannelError, "unexpected answer"),
    ],
)
def test_error_translation(error: Exception, expected: type[Exception], text: str) -> None:
    made, _ = channel({sharing.PATH_DETAIL: error})
    with pytest.raises(expected, match=text.replace("(", r"\(").replace(")", r"\)")) as raised:
        made.read([T1])
    assert type(raised.value) is expected
    assert made.last_error == str(raised.value)


def test_route_refusal_passes_through() -> None:
    made, manager = channel()
    manager.update_device_cache = lambda: manager.customer_api.get("/v1.0/m/scene/list")  # type: ignore[method-assign]
    with pytest.raises(ChannelError, match="allow-list"):
        made.discover()


def test_token_renewal_reaches_the_caller() -> None:
    saved: list[SharingSession] = []
    made, manager = channel(on_token=saved.append)
    made.discover()
    manager.listener(
        {
            "t": 2,
            "uid": "u",
            "expire_time": 7200,
            "access_token": "a2",
            "refresh_token": "r2",
            "x": 1,
        }
    )
    assert saved[0].token_info == {
        "t": 2,
        "uid": "u",
        "expire_time": 7200,
        "access_token": "a2",
        "refresh_token": "r2",
    }
    assert made.session is saved[0] and saved[0].user_code == "usercode"
    quiet, manager = channel()
    quiet.discover()
    manager.listener({"access_token": "a3", "refresh_token": "r3"})
    assert quiet.session.token_info["access_token"] == "a3"


def test_send_opens_the_command_route_only_for_the_call() -> None:
    path = f"/v1.1/m/thing/{T1}/commands"
    made, manager = channel({path: {"success": True}})
    made.send(T1, [{"code": "temp_set", "value": 195}])
    assert manager.customer_api.calls == [
        ("POST", path, {"commands": [{"code": "temp_set", "value": 195}]})
    ]
    assert manager.customer_api.session.writing is False
    with pytest.raises(ChannelError):
        manager.customer_api.post(path, None, {})
    made.close()


def test_send_failures() -> None:
    path = f"/v1.1/m/thing/{T1}/commands"
    command = [{"code": "temp_set", "value": 195}]
    with pytest.raises(ChannelError, match="invalid device id"):
        channel()[0].send("bad id", command)
    for malformed in ([], [{"code": "temp_set"}], [{"code": "a", "value": 1, "extra": 2}]):
        with pytest.raises(ChannelError, match="malformed"):
            channel()[0].send(T1, malformed)
    with pytest.raises(ChannelError, match="HTTP error"):
        channel({path: None})[0].send(T1, command)
    with pytest.raises(ChannelError, match="refused by Tuya: nope") as refused:
        channel({path: {"success": False, "msg": "nope", "code": 2008}})[0].send(T1, command)
    assert refused.value.code == "2008"
    made, manager = channel({path: ApiError("2008")})
    with pytest.raises(ChannelError, match="not supported"):
        made.send(T1, command)
    assert manager.customer_api.session.writing is False


class FakeLogin:
    def __init__(self, qr: dict[str, Any], result: tuple[bool, dict[str, Any]]) -> None:
        self.qr, self.result = qr, result
        self.asked: list[tuple[Any, ...]] = []

    def qr_code(self, client_id: str, schema: str, user_code: str) -> dict[str, Any]:
        self.asked.append((client_id, schema, user_code))
        return self.qr

    def login_result(
        self, token: str, client_id: str, user_code: str
    ) -> tuple[bool, dict[str, Any]]:
        self.asked.append((token, client_id, user_code))
        return self.result


def test_qr_code_login() -> None:
    info = {
        "terminal_id": "terminal",
        "endpoint": "https://example.invalid",
        **SESSION["token_info"],
    }  # type: ignore[dict-item]
    control = FakeLogin({"success": True, "result": {"qrcode": "tok"}}, (True, info))
    text = sharing.request_qr_code("usercode", control=control)
    assert text == "tuyaSmart--qrLogin?token=tok"
    session = sharing.poll_login(text, "usercode", control=control)
    assert session is not None and session.to_dict() == SESSION
    assert control.asked[1] == ("tok", sharing.CLIENT_ID, "usercode")
    waiting = FakeLogin({"success": False, "msg": "bad user code", "code": 1}, (False, {}))
    assert sharing.poll_login(text, "usercode", control=waiting) is None
    with pytest.raises(AuthenticationError, match="bad user code"):
        sharing.request_qr_code("usercode", control=waiting)
