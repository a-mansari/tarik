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


@pytest.mark.functionality
def test_a_warning_check_says_its_message_or_nothing_and_a_crash_is_skipped_as_experio_does(monkeypatch):
    raised = {}

    def odoo_json(model, method, ids, context):
        if raised:
            raise raised["error"]

    monkeypatch.setattr(experio, "odoo_json", odoo_json)
    assert experio.warns(3, "validate_warning_account_id", {}) is None
    raised["error"] = experio.ExperioRefusal("<p>Compte hors classes</p>", "odoo.exceptions.ValidationError")
    assert experio.warns(3, "validate_warning_account_id", {}) == "Compte hors classes"
    raised["error"] = experio.ExperioRefusal("'bool' object is not subscriptable", "builtins.TypeError")
    assert experio.warns(3, "validate_warning_account_id", {}) is None  # Experio's own list skips it too
    raised["error"] = TimeoutError("Experio is unreachable")
    with pytest.raises(TimeoutError):  # never taken for « no warning »
        experio.warns(3, "validate_warning_account_id", {})


def fake_settings(monkeypatch, **values):
    monkeypatch.setattr(experio, "current", lambda: "cabinet_x")
    monkeypatch.setattr(experio, "setting", lambda key, default=None: values.get(key) or default)


@pytest.mark.security
def test_the_api_key_is_used_before_the_password(monkeypatch):
    fake_settings(monkeypatch, ODOO_API_KEY="api", ODOO_PASSWORD="pw", ODOO_UID="430", ODOO_USER="tarik")
    assert experio.account() == ("cabinet_x", 430, "tarik", "api")
    fake_settings(monkeypatch, ODOO_PASSWORD="pw", ODOO_UID="430", ODOO_USER="tarik")
    assert experio.account()[3] == "pw"


@pytest.mark.functionality
def test_test_mode_is_the_default_and_marks_each_message(monkeypatch):
    fake_settings(monkeypatch)
    assert experio.dry_run() and experio.test_mode_note() == experio.TEST_MODE
    fake_settings(monkeypatch, DRY_RUN="0")
    assert not experio.dry_run() and experio.test_mode_note() == ""


@pytest.mark.communication
def test_a_failure_becomes_the_given_message_with_experio_reason_and_a_tool_error_passes_as_is():
    with pytest.raises(ToolError, match="^Refus : Compte bloque$"), experio.refused("log", "Refus : {reason}"):
        raise experio.ExperioRefusal("Compte\n bloque", "odoo.exceptions.UserError")
    with pytest.raises(ToolError, match="^own$"), experio.refused("log", "Refus : {reason}"):
        raise ToolError("own")
    with pytest.raises(ValueError, match="last line"), experio.refused("log", "{reason}", error=ValueError):
        raise RuntimeError("Traceback\n  File x\nlast line")


@pytest.mark.technical
def test_an_xml_rpc_connection_gives_up_after_the_timeout(monkeypatch):
    monkeypatch.setattr(experio, "ODOO_URL", "https://experio.invalid")
    transport = experio._timed_transport()
    assert isinstance(transport, experio.xmlrpc.client.SafeTransport)
    assert transport.make_connection("experio.invalid").timeout == experio.RPC_TIMEOUT


@pytest.mark.security
def test_a_login_refused_or_as_another_user_fails_and_a_good_one_is_kept(monkeypatch):
    answers = [False, 999, 430]

    class Common:
        def authenticate(self, database, user, key, options):
            return answers.pop(0)

    monkeypatch.setattr(experio, "account", lambda: ("db-login-kept", 430, "tarik", "key"))
    monkeypatch.setattr(experio, "rpc", lambda path: Common())
    monkeypatch.setattr(experio, "_logins", {})
    with pytest.raises(PermissionError, match="login refused"):
        experio.login()
    with pytest.raises(PermissionError, match="wrong cabinet"):
        experio.login()
    assert experio.login() == 430 and experio.login() == 430 and answers == []  # the second one from memory


@pytest.mark.communication
def test_a_json_refusal_carries_experio_message_and_exception_name(monkeypatch):
    errors = [
        {"message": "Odoo Server Error", "data": {"message": "Compte requis", "name": "odoo.exceptions.UserError"}},
        {"message": "Odoo Server Error"},
    ]

    class Response:
        status_code = 200

        def json(self):
            return {"error": errors.pop(0)}

    monkeypatch.setattr(experio, "account", lambda: ("db", 430, "tarik", "key"))
    monkeypatch.setattr(experio, "login", lambda: 430)
    monkeypatch.setattr(experio.HTTP, "post", lambda url, timeout, json: Response())
    with pytest.raises(experio.ExperioRefusal, match="Compte requis") as refusal:
        experio.odoo_json("exp.invoice", "action_post", [7])
    assert refusal.value.name == "odoo.exceptions.UserError"
    with pytest.raises(experio.ExperioRefusal, match="Odoo Server Error") as refusal:
        experio.odoo_json("exp.invoice", "action_post", [7])
    assert refusal.value.name == ""


@pytest.mark.functionality
def test_a_new_dossier_is_made_from_the_cabinet_main_one(monkeypatch):
    asked = []

    def odoo_call(model, method, domain, fields):
        asked.append((model, domain))
        return [{"id": 1, "res_id": 12}]

    monkeypatch.setattr(experio, "odoo_call", odoo_call)
    assert experio.main_dossier_context() == {"allowed_company_ids": [12]}
    assert asked == [("ir.model.data", [("module", "=", "base"), ("name", "=", "main_company")])]


@pytest.mark.security
def test_a_dossier_is_checked_in_experio_unknown_or_unreachable_is_refused(monkeypatch):
    atlas = {"name": "ATLAS", "user_ids": [2, 7]}
    answers = [[atlas], [], [{**atlas, "user_ids": [2]}], ConnectionRefusedError("down")]
    domains = []

    def odoo_call(model, method, domain, fields):
        domains.append(domain)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(experio, "odoo_call", odoo_call)
    monkeypatch.setattr(experio, "login", lambda: 7)
    assert experio.require_dossier(8) == "ATLAS"
    assert domains[0] == [("id", "=", 8), *experio.DOSSIER_DOMAIN]  # never a disabled, deleted or the main dossier
    with pytest.raises(ToolError, match="Aucun dossier n° 8"):
        experio.require_dossier(8)
    # not among its users: Experio would refuse « Access to unauthorized or invalid companies »
    with pytest.raises(ToolError, match="Tarik n'a pas accès au dossier ATLAS"):
        experio.require_dossier(8)
    answers.insert(0, [{**atlas, "user_ids": [2]}])
    assert experio.require_dossier(8, opened=False) == "ATLAS"  # a tool naming no dossier to Experio
    with pytest.raises(ToolError, match="injoignable"):
        experio.require_dossier(8)


@pytest.mark.security
def test_an_invoice_must_belong_to_a_dossier_of_the_cabinet_and_be_a_draft_already_read(monkeypatch):
    invoices = {}

    def odoo_call(model, method, domain, fields):
        assert ("company_id.enabled", "=", True) in domain
        if domain[0][2] == 9:
            raise ConnectionResetError("down")
        found = invoices.get(domain[0][2])
        return [found] if found else []

    monkeypatch.setattr(experio, "odoo_call", odoo_call)
    invoices[5] = {"id": 5, "state": "draft", "bluejay_state": "done"}
    invoices[6] = {"id": 6, "state": "posted", "bluejay_state": "done"}
    invoices[7] = {"id": 7, "state": "draft", "bluejay_state": "pending"}
    assert experio.require_draft_invoice(5, ["state"], read=True) == invoices[5]
    assert experio.require_draft_invoice(7, ["state"]) == invoices[7]  # not asked to be read: fine
    with pytest.raises(ToolError, match="encore en train de la lire"):
        experio.require_draft_invoice(7, ["state"], read=True)
    with pytest.raises(ToolError, match="seule une facture brouillon peut etre postee"):
        experio.require_draft_invoice(6, ["state"], what="etre postee")
    with pytest.raises(ToolError, match="Aucune facture n° 4"):
        experio.require_invoice(4, ["state"])
    with pytest.raises(ToolError, match="injoignable"):
        experio.require_invoice(9, ["state"])


@pytest.mark.functionality
def test_an_invoice_context_and_account_code_follow_its_own_dossier_and_exercice():
    invoice = {"company_id": [8, "ATLAS"], "exercice_id": [3, "2026"], "account_id": [1, "6136 Honoraires"]}
    assert experio.invoice_context(invoice) == {"allowed_company_ids": [8], "cron_fiscal_entry_id": 3}
    assert experio.invoice_context({**invoice, "exercice_id": False}) == {"allowed_company_ids": [8]}
    assert experio.account_code(invoice) == "6136" and experio.account_code({"account_id": False}) == ""


@pytest.mark.functionality
@pytest.mark.parametrize("test_mode", [True, False])
def test_experio_refresh_runs_only_outside_test_mode(monkeypatch, test_mode):
    calls = []
    monkeypatch.setattr(experio, "dry_run", lambda: test_mode)
    monkeypatch.setattr(experio, "odoo_json", lambda *args, **kw: calls.append((args, kw)))
    experio.refresh(7, {"allowed_company_ids": [8]})
    expected = [(("exp.invoice", "action_refresh", [7]), {"context": {"allowed_company_ids": [8]}})]
    assert calls == ([] if test_mode else expected)


@pytest.mark.functionality
def test_journal_settings_unset_or_of_another_dossier_are_misplaced(monkeypatch):
    def odoo_call(model, method, *args, fields):
        if model == "res.company":
            return [{"purchase": [1, "ACH"], "sale": [2, "VTE ATLAS"], "bank": False}]
        assert args[0] == [("id", "in", [1, 2])]
        return [{"id": 1, "company_id": [8, "ATLAS"]}, {"id": 2, "company_id": [3, "FIRST"]}]

    monkeypatch.setattr(experio, "odoo_call", odoo_call)
    assert experio.misplaced_journals(8, ["purchase", "sale", "bank"]) == {"sale": "VTE ATLAS", "bank": False}


@pytest.mark.functionality
def test_journal_settings_all_unset_ask_no_journal(monkeypatch):
    calls = []

    def odoo_call(model, method, *args, fields):
        calls.append(model)
        return [{"purchase": False}]

    monkeypatch.setattr(experio, "odoo_call", odoo_call)
    assert experio.misplaced_journals(8, ["purchase"]) == {"purchase": False} and calls == ["res.company"]


@pytest.mark.functionality
def test_experio_own_check_keeps_its_errors_and_settings_as_blocking_never_its_warnings(monkeypatch):
    messages = [["cgnc", "<b>Journal</b> manquant"], ["error", "Montant"], ["warning", "Doublon"]]
    monkeypatch.setattr(experio, "odoo_json", lambda *args, **kw: messages)
    assert experio.blocking(7, {}) == ["Journal manquant", "Montant"]
    monkeypatch.setattr(experio, "odoo_json", lambda *args, **kw: None)
    assert experio.checked(7, {}) == []


def fake_form(monkeypatch, record, onchange, test_mode):
    written = []

    def odoo_call(model, method, *args, **kw):
        if method == "fields_get":
            return {
                "ref": {"type": "char", "store": True},
                "amount": {"type": "float", "store": True},
                "total": {"type": "float", "store": True, "readonly": True},
                "line_ids": {"type": "one2many", "store": True},
                "computed": {"type": "char", "store": False},
                "__last_update": {"type": "datetime", "store": True},
                "write_date": {"type": "datetime", "store": True},
            }
        if method == "read":
            return [{**record, "id": 1}]
        written.append((args, kw))

    monkeypatch.setattr(experio, "odoo_call", odoo_call)
    monkeypatch.setattr(experio, "odoo_json", lambda *a, **k: onchange)
    monkeypatch.setattr(experio, "dry_run", lambda: test_mode)
    monkeypatch.setattr(experio, "_forms", {})
    monkeypatch.setattr(experio, "current", lambda: "cabinet_x")
    return written


@pytest.mark.functionality
def test_a_filled_invoice_is_written_with_what_experio_fills_outside_test_mode(monkeypatch):
    written = fake_form(monkeypatch, {"ref": "F1", "amount": 10.0, "total": 10.0}, {"value": {"total": 12.0}}, False)
    assert experio.fill_invoice(1, {"amount": 12.0}, {"c": 1}) == {"amount": 12.0}
    assert written == [(([1], {"amount": 12.0}), {"context": {"c": 1}})]  # a read-only total is never written
    assert set(experio._forms["cabinet_x"]["types"]) == {"ref", "amount", "total"}


@pytest.mark.functionality
@pytest.mark.parametrize(("test_mode", "changes"), [(True, {"amount": 12.0}), (False, {"amount": 10.0})])
def test_a_filled_invoice_is_not_written_in_test_mode_or_when_nothing_changes(monkeypatch, test_mode, changes):
    written = fake_form(monkeypatch, {"ref": "F1", "amount": 10.0, "total": 10.0}, None, test_mode)
    assert experio.fill_invoice(1, changes, {}) == ({} if changes["amount"] == 10.0 else changes)
    assert written == []


def connection(monkeypatch, version, count):
    class Common:
        def version(self):
            if isinstance(version, Exception):
                raise version
            return {"server_version": version}

    def odoo_call(*args):
        if isinstance(count, Exception):
            raise count
        return count

    monkeypatch.setattr(experio, "rpc", lambda path: Common())
    monkeypatch.setattr(experio, "odoo_call", odoo_call)
    monkeypatch.setattr(experio, "account", lambda: ("db", 430, "tarik", "key"))


@pytest.mark.communication
@pytest.mark.parametrize(
    ("version", "count", "said"),
    [
        ("17.0", 3, "Connecté au système Experio."),
        (ConnectionRefusedError("no route"), 3, "Experio est injoignable pour le moment."),
        ("17.0", PermissionError("refused"), "Experio répond, mais ma connexion y est refusée."),
    ],
)
def test_the_connection_check_says_reached_unreachable_or_refused_never_odoo(monkeypatch, version, count, said):
    connection(monkeypatch, version, count)
    answer = experio.experio_connection()
    assert answer == said and "Odoo" not in answer


@pytest.mark.functionality
def test_the_cabinets_own_company_is_found_by_its_xmlid(monkeypatch):
    monkeypatch.setattr(experio, "odoo_call", lambda model, method, domain, **kw: [{"res_id": 1}])
    assert experio.main_company_id() == 1
