# Changelog

Versions follow the bridge that uses them (`tuyapilot2mqtt`); every one is a tag of this
repository. Unreleased changes are at the top.

## Unreleased

- Packaging for PyPI: a publication workflow (`.github/workflows/publish.yml`, Trusted
  Publishing, on a published GitHub release), `python -m build` and `twine check` in CI.

## 0.3.0 — 2026-11-27

- The write gate writes a mode only to `manual` or `program`. `eco` and `holiday` are declared by
  the device but refused: the device then follows a setpoint with no proven write route.
- The dialect codec speaks the contract's names: preset `program` and `antifrost`,
  `antifrost_temperature`.

## 0.2.2 — 2026-11-27

- A `sign invalid` answer of the sharing API (another client logged in with the same
  application) is an `AuthenticationError`, like an expired token, instead of a retried channel
  error: a daemon asks for a new login rather than retrying forever.

## 0.2.1 — 2026-11-27

- The gate refuses a setpoint, from the fresh read made just before sending, in any mode but
  `manual` (in `program` the running schedule slot overwrites it, seen on real hardware), with a
  reason per mode.

## 0.2.0 — 2026-11-27

- Router (each operation to the channel proven for it, or refused), write gate (verified
  products, the device's own bounds and step, fresh read, one send, read-back; disabled by
  default) and the reusable bridge daemon core with a paho MQTT transport.

## 0.1.0 — 2026-11-27

- Data-point table of the "FP Thermostat" (`fu1bfwza`) and its decoding (LAN and cloud).
- Weekly-schedule codec (28 data points, 15-minute step, header kept on write).
- Device model, Zigbee2MQTT dialect codec, and read clients for the three channels (sharing API,
  LAN 3.3 through the gateway, developer API).
