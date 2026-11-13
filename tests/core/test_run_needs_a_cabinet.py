"""A run names its cabinet or takes the stack's default; with neither it is refused, not run in a cabinet by chance."""

import asyncio

import pytest

from engine import runs


def test_a_run_with_no_cabinet_and_no_default_is_refused(monkeypatch):
    monkeypatch.setattr(runs, "DEFAULT_CABINET", "")
    with pytest.raises(ValueError, match="no cabinet"):
        asyncio.run(runs.start("agent", {"input": {}}))
