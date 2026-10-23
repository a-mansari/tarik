"""The guards every tool goes through: a real dossier or invoice id, Tarik's own user."""

import http.client
import threading

import httpx
import pytest

from engine.tools.core import experio
from engine.tools.core.app import ToolError
from engine.tools.core.experio import expected_uid, require_dossier, require_invoice


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
    from engine.tools.core import experio

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


@pytest.mark.technical
def test_the_login_cut_off_is_asked_again(monkeypatch):
    tries = []

    class Common:
        def authenticate(self, database, user, key, options):
            tries.append(user)
            if len(tries) == 1:
                raise ConnectionResetError("reset by peer")
            return 430

    monkeypatch.setattr(experio, "account", lambda: ("db-login-test", 430, "tarik", "key"))
    monkeypatch.setattr(experio, "rpc", lambda path: Common())
    monkeypatch.setattr(experio.time, "sleep", lambda seconds: None)
    assert experio.login() == 430 and len(tries) == 2


@pytest.mark.technical
def test_json_reads_are_asked_again_once_writes_and_timeouts_never(monkeypatch):
    answers, posted = [], []

    class Response:
        def __init__(self, status):
            self.status_code = status

        def json(self):
            return {"result": "ok"}

    def post(url, timeout, json):
        posted.append(json["params"]["args"][4])
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return Response(answer)

    monkeypatch.setattr(experio, "account", lambda: ("db", 430, "tarik", "key"))
    monkeypatch.setattr(experio, "login", lambda: 430)
    monkeypatch.setattr(experio.HTTP, "post", post)
    monkeypatch.setattr(experio.time, "sleep", lambda seconds: None)
    answers[:] = [502, 200]
    assert experio.odoo_json("exp.invoice", "onchange", [7]) == "ok" and posted == ["onchange", "onchange"]
    for failure in (httpx.RemoteProtocolError("cut"), 503):
        answers[:], posted[:] = [failure, 200], []
        with pytest.raises((httpx.RemoteProtocolError, experio.GatewayError)):
            experio.odoo_json("exp.invoice", "action_post", [7])
        assert posted == ["action_post"]  # posted or not: never asked again
    answers[:], posted[:] = [httpx.ReadTimeout("slow"), 200], []
    with pytest.raises(httpx.ReadTimeout):
        experio.odoo_json("exp.invoice", "onchange", [7])
    assert posted == ["onchange"]


@pytest.mark.technical
def test_one_xml_rpc_proxy_per_thread_and_path(monkeypatch):
    """Calls reuse their connection: the same proxy within a thread, another per path, URL or thread."""
    monkeypatch.setattr(experio, "ODOO_URL", "http://experio.invalid")
    common, other = experio.rpc("/xmlrpc/2/common"), experio.rpc("/xmlrpc/2/object")
    assert experio.rpc("/xmlrpc/2/common") is common and other is not common
    monkeypatch.setattr(experio, "ODOO_URL", "http://other.invalid")
    assert experio.rpc("/xmlrpc/2/common") is not common  # the tests point ODOO_URL elsewhere: never the old proxy
    elsewhere = []
    thread = threading.Thread(target=lambda: elsewhere.append(experio.rpc("/xmlrpc/2/common")))
    thread.start()
    thread.join()
    assert elsewhere[0] is not experio.rpc("/xmlrpc/2/common")


@pytest.mark.security
@pytest.mark.parametrize(
    ("changes", "onchange"),
    [({"partner_id": 8}, {}), ({"type": "sale"}, {"partner_id": False})],  # replaced, or cleared by an onchange
)
def test_a_tiers_already_set_in_experio_is_never_replaced(db_cabinet, monkeypatch, changes, onchange):
    written = []

    def odoo_call(model, method, *args, **kw):
        if method == "fields_get":
            fields = (("partner_id", "many2one"), ("type", "char"))
            return {name: {"type": kind, "store": True} for name, kind in fields}
        if method == "read":
            return [{"partner_id": [7, "ATLAS"], "type": "purchase"}]
        written.append(method)

    monkeypatch.setattr(experio, "odoo_call", odoo_call)
    monkeypatch.setattr(experio, "odoo_json", lambda *a, **k: {"value": onchange})
    monkeypatch.setattr(experio, "dry_run", lambda: False)
    monkeypatch.setattr(experio, "_forms", {})
    with pytest.raises(experio.ToolError, match="ne le remplace pas"):
        experio.fill_invoice(1, changes, {})
    assert written == []
