"""The guards every tool goes through: a real dossier or invoice id, Tarik's own user."""

import http.client

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tarik.core import experio
from tarik.core.experio import expected_uid, require_dossier, require_invoice


@pytest.mark.security
@pytest.mark.parametrize("bad", [None, 0, -5, "778", True])
def test_a_dossier_tool_needs_a_real_dossier_id(bad):
    with pytest.raises(ToolError, match="find_dossier"):
        require_dossier(bad)


@pytest.mark.security
@pytest.mark.parametrize("bad", [None, 0, -1, "264417", True])
def test_an_invoice_tool_needs_a_real_invoice_id(bad):
    with pytest.raises(ToolError, match="invoice_id"):
        require_invoice(bad, ["id"])


@pytest.mark.security
def test_tarik_user_id_is_locked():
    assert expected_uid("430") == 430
    for bad in (None, "", "0", "-1", "abc", " 430"):
        with pytest.raises(SystemExit):
            expected_uid(bad)


@pytest.mark.security
def test_seeing_what_experio_would_fill_saves_nothing(db_cabinet, monkeypatch):
    from tarik.core import experio

    written = []

    def odoo_call(model, method, *args, **kw):
        if method == "fields_get":
            return {
                "partner_id": {"type": "many2one", "store": True},
                "account_id": {"type": "many2one", "store": True},
                "payment_method_id": {"type": "many2one", "store": True},
            }
        if method == "read":
            return [{"partner_id": [7, "ATLAS"], "account_id": False, "payment_method_id": [2, "Virement"]}]
        written.append(method)

    monkeypatch.setattr(experio, "odoo_call", odoo_call)
    monkeypatch.setattr(
        experio, "odoo_json", lambda *a, **k: {"value": {"account_id": 61450000, "payment_method_id": False}}
    )
    monkeypatch.setattr(experio, "dry_run", lambda: False)
    monkeypatch.setattr(experio, "_forms", {})  # this fake form only, never cached for other tests
    changed = experio.fill_invoice(1, {"partner_id": 7}, {}, save=False)
    assert changed["account_id"] == 61450000 and written == []  # the scan's payment method is not reset in Experio


@pytest.mark.technical
def test_a_read_cut_off_is_asked_again_once_a_write_never(monkeypatch):
    calls = []

    class Experio:
        def execute_kw(self, database, uid, key, model, method, args, kw):
            calls.append(method)
            if len(calls) == 1 or method == "write":
                raise http.client.IncompleteRead(b"<?xml", 9000)
            return [{"id": 7}]

    monkeypatch.setattr(experio, "account", lambda: ("db", 430, "tarik", "key"))
    monkeypatch.setattr(experio, "login", lambda: 430)
    monkeypatch.setattr(experio, "rpc", lambda path: Experio())
    monkeypatch.setattr(experio.time, "sleep", lambda seconds: None)
    assert experio.odoo_call("res.company", "search_read", []) == [{"id": 7}] and calls == ["search_read"] * 2
    calls.clear()
    with pytest.raises(http.client.IncompleteRead):
        experio.odoo_call("res.company", "write", [7], {"name": "X"})
    assert calls == ["write"]  # a cut answer doesn't say whether it was written
