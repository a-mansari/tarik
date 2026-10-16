"""The guards every tool goes through: a real dossier or invoice id, Tarik's own user."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

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
            return {"partner_id": {"type": "many2one", "store": True}, "account_id": {"type": "many2one", "store": True},
                    "payment_method_id": {"type": "many2one", "store": True}}
        if method == "read":
            return [{"partner_id": [7, "ATLAS"], "account_id": False, "payment_method_id": [2, "Virement"]}]
        written.append(method)
    monkeypatch.setattr(experio, "odoo_call", odoo_call)
    monkeypatch.setattr(experio, "odoo_json", lambda *a, **k: {"value": {"account_id": 61450000, "payment_method_id": False}})
    monkeypatch.setattr(experio, "dry_run", lambda: False)
    monkeypatch.setattr(experio, "_forms", {})  # this fake form only, never cached for other tests
    changed = experio.fill_invoice(1, {"partner_id": 7}, {}, save=False)
    assert changed["account_id"] == 61450000 and written == []  # the scan's payment method is not reset in Experio
