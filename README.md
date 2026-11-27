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

Pre-alpha, not on PyPI. What exists is small and tested:

| Module | Content | State |
|---|---|---|
| `dps` | data-point table of the "FP Thermostat" (product `fu1bfwza`) and decoding of a LAN reading | done, checked against real readings |
| `schedule` | weekly-schedule codec (28 data points, 15-minute step), with the read-modify-write rule on headers | done, format checked on real hardware (read, then written and restored) |
| channels (sharing API, LAN, developer API), router, write gate, Zigbee2MQTT dialect codec | — | milestone M1 of the [roadmap](https://github.com/aghulas/ha-tuya-pilot-wire/blob/main/docs/roadmap.md) |

## Use

```python
from pytuya_pilot_wire import dps, schedule

dps.decode_lan(dps.PRODUCT_FP_THERMOSTAT, {"16": 195, "50": 200, "124": "closed"})
# {'temperature': 19.5, 'setpoint': 20.0, 'work_state': 'closed', 'heating': False, 'power': 0.0}

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

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy src && .venv/bin/pytest -q
```

Tests use synthetic data only: no real device id, key or reading in this repository.

## License

MIT — see [`LICENSE`](LICENSE).
