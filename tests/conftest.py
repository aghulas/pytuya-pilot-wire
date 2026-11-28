"""Shared fixtures. Synthetic data only."""

from __future__ import annotations

import pytest

from pytuya_pilot_wire import dps
from pytuya_pilot_wire.model import Channel
from pytuya_pilot_wire.router import ROUTES, Operation, Route, RouteState

SYNTHETIC_CANDIDATE = Route((Channel.SHARING,), RouteState.CANDIDATE, "synthetic candidate")


@pytest.fixture
def candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every write proven so far is a candidate again: the candidate machinery, under test."""
    for operation in (Operation.CHILD_LOCK, Operation.WINDOW_DETECTION):
        monkeypatch.setitem(ROUTES[dps.PRODUCT_FP_THERMOSTAT], operation, SYNTHETIC_CANDIDATE)
