"""The invoices' tests run their tools mostly outside a cabinet: the dossier's lock (keyed by cabinet) is a plain one,
and the test mode's wording follows intake's dry_run as each test sets it."""

import threading

import pytest

from engine.tools.invoices import intake


@pytest.fixture(autouse=True)
def plain_dossier_lock(monkeypatch):
    """intake's dossier_lock without a cabinet: one lock per call, as no two calls of a test race."""
    monkeypatch.setattr(intake, "dossier_lock", lambda dossier_id: threading.Lock())
    monkeypatch.setattr(intake, "would", lambda done: f"serait {done}" if intake.dry_run() else done)
