"""A cabinet sees only its own settings, and the shared ones."""

import pytest

from engine.tools.core import experio
from engine.tools.core.cabinets import current, setting, using
from engine.tools.emails import sending
from shared import A, B


@pytest.mark.security
def test_settings_are_the_cabinets_own(two_cabinets, monkeypatch):
    monkeypatch.setenv("ODOO_UID", "999")  # an unprefixed cabinet setting: never read
    with using(A):
        assert experio.account() == (A, 11, f"tarik@{A}", f"secret-{A}")
        assert sending.allowed_recipients() == [f"client@{A}.ma"]
    with using(B):
        assert experio.account() == (B, 22, f"tarik@{B}", f"secret-{B}")
        assert sending.allowed_recipients() == [f"client@{B}.ma"]


@pytest.mark.security
def test_a_missing_setting_never_falls_back_to_another_cabinet(two_cabinets, monkeypatch):
    monkeypatch.delenv(f"{B.upper()}__ODOO_USER")
    monkeypatch.setenv("ODOO_USER", "tarik@unprefixed")
    with using(B), pytest.raises(KeyError):
        setting("ODOO_USER")
    with using(B):
        assert sending.recipient_allowed(f"client@{A}.ma") is False  # A's whitelist is not B's


@pytest.mark.security
def test_dry_run_is_per_cabinet_and_on_by_default(two_cabinets, monkeypatch):
    monkeypatch.setenv(f"{A.upper()}__DRY_RUN", "0")
    with using(A):
        assert experio.dry_run() is False
    with using(B):
        assert experio.dry_run() is True


@pytest.mark.security
def test_the_email_signature_is_the_cabinets_own(two_cabinets, monkeypatch):
    monkeypatch.setattr(sending, "_names", {})
    monkeypatch.setattr(sending, "odoo_call", lambda *args, **kw: [{"name": f"Cabinet {current()}"}])
    with using(A):
        assert sending.cabinet_name() == f"Cabinet {A}"
    with using(B):
        assert sending.cabinet_name() == f"Cabinet {B}"  # not A's, though A's is kept


@pytest.mark.security
def test_the_invoice_form_is_read_per_cabinet(two_cabinets, monkeypatch):
    calls = []

    def fake(model, method, *args, **kw):
        calls.append((current(), method))
        if method == "fields_get":
            return {f"field_{current()}": {"type": "char", "store": True, "readonly": False}}
        if method == "read":
            return [{f"field_{current()}": "x"}]
        return {}

    monkeypatch.setattr(experio, "odoo_call", fake)
    monkeypatch.setattr(experio, "odoo_json", lambda *a, **k: {})
    monkeypatch.setattr(experio, "_forms", {})
    with using(A):
        assert experio.fill_invoice(1, {f"field_{A}": "y"}, {}) == {f"field_{A}": "y"}
    with using(B):
        assert experio.fill_invoice(1, {f"field_{B}": "y"}, {}) == {f"field_{B}": "y"}
    assert (B, "fields_get") in calls  # B read its own form, not A's cached one


@pytest.mark.security
def test_a_cabinet_setting_is_only_ever_the_cabinets_own(two_cabinets, monkeypatch):
    monkeypatch.setenv("IMAP_PASSWORD", "unprefixed")  # nobody's
    with using(A), pytest.raises(KeyError):
        setting("IMAP_PASSWORD")
    monkeypatch.setenv(f"{A.upper()}__IMAP_PASSWORD", "a's own")
    with using(A):
        assert setting("IMAP_PASSWORD") == "a's own"
    with using(B), pytest.raises(KeyError):
        setting("IMAP_PASSWORD")


@pytest.mark.technical
def test_a_shared_setting_is_the_same_for_every_cabinet(two_cabinets, monkeypatch):
    monkeypatch.setenv("IMAP_HOST", "mail.example.ma")
    monkeypatch.setenv(f"{A.upper()}__IMAP_HOST", "ignored.example.ma")
    for name in (A, B):
        with using(name):
            assert setting("IMAP_HOST") == "mail.example.ma"
    with pytest.raises(RuntimeError):  # still only inside a cabinet's call
        setting("IMAP_HOST")


@pytest.mark.technical
def test_an_unknown_setting_is_an_error(two_cabinets):
    with using(A), pytest.raises(KeyError):
        setting("POSTGRES_PASSWORD")
