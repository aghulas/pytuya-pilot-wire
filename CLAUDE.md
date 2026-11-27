# pytuya-pilot-wire — guide for agent sessions

Tuya-facing library of the pilot-wire project. Scope, architecture and roadmap live in
`~/dev/ha-tuya-pilot-wire/docs/` (read `scope.md` first); the MQTT contract in
`~/dev/tuyapilot2mqtt/docs/mqtt-contract.md`.

## Rules

- **Public repository.** No secret (session token, local key, access id / secret), no real device
  id, no site data — in code, tests, docs, commit messages or logs. Tests use synthetic data.
- **No Home Assistant import.** The integration and the bridge depend on this library, never the
  reverse.
- **Proven routes only, zero optimistic state.** An operation is routed to the channel proven for
  it on real hardware; a new write is first a closed candidate, opened by its own supervised trial
  on one designated device, read back from an independent source. Never a fallback to a channel
  that merely accepts the call.
- **A data point not in the table is ignored**, never guessed; an absent value is unknown, not
  zero.
- A write to real hardware needs the owner's agreement on the exact operation, every time.

## Reuse (do not rewrite)

The discovery work is in the private tool `~/dev/tuya-mcp` (`client.py` sharing API, `local.py`
LAN, `programme.py` schedule and developer API, `ecritures.py` write guard). Port from it,
restated generically and in English, with its tests.

## Quality

```
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/pytest -q --cov=pytuya_pilot_wire --cov-fail-under=95
```

All green before a commit; these are the commands CI runs. Commit messages in English. Commit and
push only when the owner asks.
