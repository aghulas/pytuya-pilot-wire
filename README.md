# pytuya-pilot-wire

Pure-Python library for **Tuya pilot-wire thermostats** driving electric radiators (category
`wk`). It is the Tuya-facing layer of a three-part project:

| Repository | Role |
|---|---|
| **`pytuya-pilot-wire`** (this one) | data points, codecs, Tuya channels, guarded writes; no Home Assistant dependency |
| [`tuyapilot2mqtt`](https://github.com/aghulas/tuyapilot2mqtt) | bridge and Home Assistant add-on: Tuya gateway → MQTT, Zigbee2MQTT dialect |
| [`ha-tuya-pilot-wire`](https://github.com/aghulas/ha-tuya-pilot-wire) | Home Assistant integration (Companion); holds the [scope](https://github.com/aghulas/ha-tuya-pilot-wire/blob/main/docs/scope.md), architecture and roadmap |

Unofficial: not affiliated with Tuya.

## Status

Pre-alpha. Made for PyPI (`pip install "pytuya-pilot-wire[all]"`), not published there yet;
until then, install a tagged version from git, with the extras of the channels used:

```bash
pip install "pytuya-pilot-wire[all] @ git+https://github.com/aghulas/pytuya-pilot-wire@v0.3.0"
```

Changes: [`CHANGELOG.md`](https://github.com/aghulas/pytuya-pilot-wire/blob/main/CHANGELOG.md).

| Module | Content | State |
|---|---|---|
| `dps` | data-point table of the "FP Thermostat" (product `fu1bfwza`); decoding of a LAN reading (by number) and of a cloud reading (by code) | done, checked against real readings |
| `schedule` | weekly-schedule codec (28 data points, 15-minute step), with the read-modify-write rule on headers | done, format checked on real hardware (read, then written and restored) |
| `model` | `Device`, `DeviceSpec` (the device's own bounds, modes, fault names), `Reading`, `DeviceState` (values merged across channels, each with its channel and time) | done |
| `dialect` | codec to the Zigbee2MQTT dialect of the [MQTT contract](https://github.com/aghulas/tuyapilot2mqtt/blob/main/docs/mqtt-contract.md): state, descriptor, availability, `/set` commands | done; property names provisional, all in `dialect.PROPERTIES` |
| `channels.sharing` | device-sharing API (Smart Life account, QR-code login): discovery, instruction sets, status reads | reads checked on real hardware (2026-11-27) |
| `channels.lan` | LAN protocol 3.3 through the gateway: every data point of each sub-device, read only | reads checked on real hardware (2026-11-27) |
| `channels.developer` | developer API (shadow properties): settings, weekly schedule, LAN inventory (gateway key, node ids) | reads checked on real hardware (2026-11-27) |
| `router` | each operation goes to the channel proven for it, or is refused; candidates are closed | done |
| `gate` | write gate: verified products, the device's own bounds and step, fresh read, one send, read-back; disabled by default | done on synthetic tests; **no write was sent to real hardware by this library yet** (milestone M4) |
| `bridge`, `mqtt` | reusable daemon core (read loop, publication of the contract's topics, `/set` and `bridge/request` handling) and a paho transport | read loop checked on real hardware (2026-11-27), writes disabled |

The three channels were read on the reference install on 2026-11-27 and agree with each other on
every value they share. The comparison with the vendor app is the owner's to make.

## Use

### Data points and schedule

```python
from pytuya_pilot_wire import dps, schedule

dps.decode_lan(dps.PRODUCT_FP_THERMOSTAT, {"16": 195, "50": 200, "124": "closed"})
# {'temperature': 19.5, 'setpoint': 20.0, 'work_state': 'closed', 'heating': False, 'power': 0.0}

dps.decode_codes(dps.PRODUCT_FP_THERMOSTAT, {"temp_current": 195, "mode": "manual"})
# {'temperature': 19.5, 'mode': 'manual'}

schedule.encode_day(
    1,
    [
        {"time": "00:00", "temperature": 16.0},
        {"time": "07:30", "temperature": 19.5},
        {"time": "17:00", "temperature": 16.0},
    ],
    current=values_read_from_the_device,
)
# {'week_program_18_1_1': '…', … four parts, each keeping the header it had}
```

A data point that is not in the table is ignored; a value that was not read is absent, never
zero.

### Channels

Each channel returns `Reading`s: the decoded values of one device, the channel and the time.

```python
from pytuya_pilot_wire.channels.sharing import (
    SharingChannel,
    SharingSession,
    request_qr_code,
    poll_login,
)
from pytuya_pilot_wire.channels.lan import Gateway, LanChannel
from pytuya_pilot_wire.channels.developer import DeveloperChannel, DeveloperProject

# Device-sharing API. The caller owns the session: log in once by QR code, store the session,
# and store it again each time the token is renewed (the refresh token rotates).
qr_text = request_qr_code(user_code)  # show as a QR code, scan with the Smart Life app
session = poll_login(qr_text, user_code)  # None until the user confirmed in the app
sharing = SharingChannel(session, on_token=store_session)
devices = sharing.discover()  # list[Device]; about five requests per device
spec = sharing.spec(devices[0].dev_id)  # DeviceSpec: bounds, modes, writable keys
readings = sharing.read([d.dev_id for d in devices if d.supported])  # one request

# Developer API: settings, weekly schedule, and what the LAN channel needs.
developer = DeveloperChannel(DeveloperProject(access_id, access_secret, region="eu"))
inventory = developer.lan_inventory()  # gateway keys, sub-devices with node ids
developer.read(device)  # eco / frost / holiday setpoints, night mode, …
developer.read_schedule(device.dev_id)  # {"monday": [{"time": "00:00", "temperature": 16.0}], …}

# LAN: sub-devices through the gateway, read only. The gateway address comes from the caller.
lan = LanChannel(Gateway(gateway.dev_id, "gateway-address", gateway.local_key))
lan.read(inventory.devices)  # voltage, current, energy, load state, …
```

Errors derive from `PilotWireError`: `AuthenticationError` (new session or key needed),
`ChannelUnavailable` (network, gateway silent, dependency missing), `ChannelError` (anything else
the channel answered).

The sharing channel replaces the SDK's HTTP session with a guarded one: allow-list of routes,
request counters (`sharing.counters`), minimum interval between requests. Sessions, keys and
project credentials are hidden from `repr`, and the local key and addresses the sharing API
returns never leave the channel.

### Device model and Zigbee2MQTT dialect

```python
from pytuya_pilot_wire import DeviceState, dialect

state = DeviceState(device)
for reading in readings:
    state.apply(reading)  # returns the keys that changed

dialect.encode_state(state, spec)
# {"local_temperature": 19.5, "current_heating_setpoint": 20.0, "system_mode": "heat",
#  "preset": "manual", "running_state": "idle", "power": 0.0, "fault": [],
#  "tuya": {"mode": "manual", "sources": {"local_temperature": {"channel": "sharing", "at": "…"}}}}

names = dialect.friendly_names(devices)  # topic-safe and unique
dialect.encode_descriptor(device, names[device.dev_id])  # entry of bridge/devices
dialect.decode_command({"current_heating_setpoint": 19.5})
# [Command(key='setpoint', value=19.5, property='current_heating_setpoint')]
```

`decode_command` only translates. Whether a command has a proven route and an acceptable value is
the business of the router and the write gate.

### Router

```python
from pytuya_pilot_wire import Channel
from pytuya_pilot_wire.router import Operation, Router

router = Router([Channel.SHARING, Channel.LAN])  # the channels that are configured
router.route(device, Operation.SETPOINT)  # Channel.SHARING
router.route(device, Operation.CHILD_LOCK)  # RouteRefused: candidate route, closed …
router.route(device, Operation.SCHEDULE)  # RouteRefused: the developer channel is not available
router.read_keys(device, Channel.LAN)  # the keys a LAN reading is trusted for
```

| Operation | Route | State |
|---|---|---|
| read state | sharing API | proven |
| read power and energy | LAN | proven |
| read eco / frost setpoints, night mode | developer API, else LAN | proven |
| read weekly schedule | developer API | proven |
| on / off, mode (`manual` and `program` only), manual setpoint (in `manual` mode only) | sharing API | proven (2026-10-31) |
| weekly schedule | developer API | proven (2026-11-14) |
| mode `eco`, `holiday`, frost protection (`hors`, reported outside the declared list) | sharing API | candidates, closed, one route per value |
| holiday setpoint, temperature correction, window detection, child lock | sharing API | candidate, closed |
| eco / frost setpoints | — | no route: the sharing API does not declare them writable |
| night mode | — | no route |

There is no fallback: when the proven channel is missing the operation is refused. A candidate
opens only for the single device designated for its supervised trial, named after the write
(`Router(channels, trials={"child_lock": dev_id, "mode:holiday": dev_id})`, see
`router.write_name`); it becomes proven by a change to the table that records the trial. The
mode has a route per value (`router.MODE_ROUTES`): `router.route(device, Operation.MODE, "eco")`.

### Write gate

```python
from pytuya_pilot_wire.gate import WriteGate

gate = WriteGate(router, sharing=sharing, developer=developer, enabled=True)  # disabled by default
result = gate.write(device, spec, "setpoint", 19.5)
result.confirmed  # True once the device reports 19.5; False after 20 s without it
result.reading  # what was read back: update the state from this, never from the request

gate.write_schedule(device, {"monday": [{"time": "00:00", "temperature": 16.0}]})
```

A write is refused (`WriteRefused`, `RouteRefused`) before anything is sent when: the gate is
disabled; the product is not verified for writes; there is no proven route; the device does not
declare the setting writable; the value is outside the device's own bounds or step, above the
30 °C setpoint ceiling, or not one of its declared modes (frost protection excepted, once its
route is open); the device is offline; or, for a
setpoint, the device is in any mode but `manual` (in `program` the running schedule slot
overwrites a written setpoint; in frost protection the device stores it and does not apply it;
in `eco` and `holiday` nothing shows it would be kept). Switch the device to `manual` first.
The command is sent once and never retried. A schedule write is read-modify-write: each part
keeps the header read from the device, only the parts that differ are written, and the schedule
is read back.

### Daemon core

`Bridge` is everything a bridge daemon does between the channels and MQTT; the daemon itself
only loads configuration and secrets and builds the channels.

```python
import threading
from pytuya_pilot_wire.bridge import Bridge, BridgeConfig
from pytuya_pilot_wire.mqtt import PahoTransport

config = BridgeConfig(prefix="tuyapilot2mqtt", sharing_interval=60, writes_enabled=False)
transport = PahoTransport("localhost", 1883, username=user, password=password, prefix=config.prefix)
bridge = Bridge(transport, sharing=sharing, lan=lan, developer=developer, config=config)
transport.on_connect = bridge.announce  # bridge/state back to online after a reconnection
transport.connect()
bridge.run(threading.Event())  # serves until the event is set
```

It publishes `bridge/state`, `bridge/info` (versions, state of each channel, routing table),
`bridge/devices`, each device's state and availability, and answers `<name>/set` (on
`bridge/response/device/set`) and `bridge/request/schedule/get` / `schedule/set`. Any channel may
be absent. Rules it keeps:

- every Tuya call runs on the thread that calls `run`; MQTT callbacks only queue;
- a reading is trusted for the keys proven for its channel, a property that was not read is
  absent, and a state changes only from a reading;
- a `/set` payload is validated as a whole before anything is sent;
- a channel whose credentials are refused is paused until `bridge.replace_channel(...)`; any
  other failure is retried with a growing delay (15 minutes at most).

`BridgeConfig`: `prefix`, `sharing_interval` (60 s), `lan_interval` (60 s), `developer_interval`
(1 h), `discovery_interval` (6 h), `stale_after` (600 s without a reading makes a device
unavailable), `writes_enabled` (false), `names` (`{dev_id: friendly name}`), `trials`,
`bridge_version`. The transport is anything with `publish(topic, payload, *, retain)` and
`subscribe(topic, callback)`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy src && .venv/bin/pytest -q --cov=pytuya_pilot_wire --cov-fail-under=95
```

Tests use synthetic data only: no real device id, key or reading in this repository.

## License

MIT — see [`LICENSE`](https://github.com/aghulas/pytuya-pilot-wire/blob/main/LICENSE).
