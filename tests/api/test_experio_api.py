"""The Experio app's API (experio_api.py): only its key opens it, a new cabinet needs every setting Tarik uses, an
existing one is never created twice, a deactivated one comes back with a create, and no answer ever holds a secret."""

import asyncio
import json

import pytest
from starlette.requests import Request

from server import cabinet_settings, experio_api, server
from server.api import settings as settings_page

FULL = {
    "ODOO_USER": "tarik@cab.ma",
    "ODOO_UID": "12",
    "ODOO_PASSWORD": "p w",
    "IMAP_USER": "tarik@cab.ma",
    "IMAP_PASSWORD": "imap",
    "SMTP_USER": "tarik@cab.ma",
    "SMTP_PASSWORD": "smtp",
}


def call(handler, method, body=None, name=None):
    raw = json.dumps(body).encode() if body is not None else b""

    async def receive():
        return {"type": "http.request", "body": raw}

    scope = {
        "type": "http",
        "method": method,
        "path": "/api/cabinets",
        "headers": [],
        "query_string": b"",
        "path_params": {"name": name} if name else {},
    }
    answer = asyncio.run(handler(Request(scope, receive)))
    return answer.status_code, json.loads(answer.body)


@pytest.fixture
def vault(monkeypatch):
    """A Vault of cabinets in memory, written as the page's AppRole would (cas 0 creates only)."""
    paths = {"cab_old": {**FULL, "ACTIVE": "0", "AGENT": "kept"}}

    def answer(method, path, **kwargs):
        name = path.rsplit("/", 1)[1]
        if method == "GET":
            return {"data": {"data": dict(paths[name]), "metadata": {"version": 1}}} if name in paths else None
        if kwargs["json"]["options"]["cas"] == 0 and name in paths:
            raise cabinet_settings.httpx.HTTPStatusError("cas", request=None, response=None)
        paths[name] = kwargs["json"]["data"]
        return {"data": {"version": 2}}

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(cabinet_settings, "_vault", answer)
    monkeypatch.setattr(cabinet_settings.cabinets, "reread_vault", lambda: None)
    monkeypatch.setattr(experio_api.cabinets, "reload_cabinets", lambda: (set(), set()))
    monkeypatch.setattr(experio_api.cabinets, "create_schema", lambda name: None)
    monkeypatch.setattr(experio_api.mail, "start_one", lambda name: True)
    monkeypatch.setattr(experio_api.audit, "record", nothing)

    class Pool:
        def connection(self):
            class Db:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return False

                async def execute(self, *args):
                    return None

            return Db()

    monkeypatch.setattr(experio_api.runs, "pool", Pool())
    return paths


def test_a_new_cabinet_needs_every_setting_tarik_uses(vault):
    status, body = call(experio_api.create, "POST", {"name": "cab_new", "settings": {"ODOO_USER": "tarik@cab.ma"}})
    assert status == 400 and "manquants" in body["error"] and "cab_new" not in vault
    status, body = call(experio_api.create, "POST", {"name": "cab_new", "settings": {**FULL, "NOPE": "x"}})
    assert status == 400 and "inconnus" in body["error"]


def test_a_new_cabinet_is_written_in_simulation_unless_told_and_never_twice(vault):
    status, body = call(experio_api.create, "POST", {"name": "cab_new", "settings": FULL})
    assert status == 201 and vault["cab_new"] == {**FULL, "DRY_RUN": "1"} and body["mailbox_watched"]
    assert "p w" not in json.dumps(body)
    status, body = call(experio_api.create, "POST", {"name": "Bad-Name", "settings": FULL})
    assert status == 400
    assert call(experio_api.create, "POST", {"name": "cab_new", "settings": FULL})[0] == 409


@pytest.mark.parametrize("name", ["public", "orchestrator", "knowledge", "hindsight", "information_schema", "pg_temp"])
def test_a_cabinet_cannot_bear_the_name_of_a_schema_the_stack_uses(vault, name):
    status, body = call(experio_api.create, "POST", {"name": name, "settings": FULL})
    assert status == 400 and name not in vault


def test_a_deactivated_cabinet_comes_back_with_its_other_keys_kept(vault):
    status, body = call(experio_api.create, "POST", {"name": "cab_old", "settings": {"DRY_RUN": "0"}})
    assert status == 200 and vault["cab_old"]["ACTIVE"] == "1" and vault["cab_old"]["AGENT"] == "kept"
    assert vault["cab_old"]["DRY_RUN"] == "0"


def test_a_cabinet_read_shows_whether_a_secret_is_set_never_its_value(vault):
    status, body = call(experio_api.one, "GET", name="cab_old")
    assert status == 200 and body["active"] is False and body["settings"]["ODOO_PASSWORD"] is True
    assert "p w" not in json.dumps(body)
    assert call(experio_api.one, "GET", name="nobody")[0] == 404


def test_a_delete_deactivates_and_keeps_the_settings(vault):
    call(experio_api.create, "POST", {"name": "cab_old", "settings": {}})
    status, body = call(experio_api.one, "DELETE", name="cab_old")
    assert status == 200 and vault["cab_old"]["ACTIVE"] == "0" and vault["cab_old"]["ODOO_PASSWORD"] == "p w"


@pytest.mark.security
def test_only_the_experio_key_opens_the_api(monkeypatch):
    monkeypatch.setattr(server, "VAULT_ENABLED", False)
    reached = []

    async def app(scope, receive, send):
        reached.append(1)

    def status(authorization, key):
        monkeypatch.setenv("EXPERIO_API_KEY", key)
        sent = []

        async def send(message):
            sent.append(message)

        headers = [(b"authorization", authorization.encode())] if authorization else []
        scope = {"type": "http", "method": "GET", "path": "/api/cabinets", "headers": headers, "query_string": b""}
        asyncio.run(server.Guard(app)(scope, None, send))
        return sent[0]["status"] if sent else 200

    assert status("Bearer the-key", "the-key") == 200
    assert status("Bearer wrong", "the-key") == 401
    assert status("", "the-key") == 401
    assert status("Bearer ", "") == 401  # no key set: nobody gets in


def page_call(method, body=None):
    """Call the add-cabinet page as an admin of the pages."""
    raw = json.dumps(body).encode() if body is not None else b""

    async def receive():
        return {"type": "http.request", "body": raw}

    scope = {
        "type": "http",
        "method": method,
        "path": "/ui/api/settings/new",
        "headers": [],
        "query_string": b"",
        "state": {"user": {"email": "admin@cab.ma"}},
    }
    answer = asyncio.run(settings_page.new_cabinet_page(Request(scope, receive)))
    return answer.status_code, json.loads(answer.body)


@pytest.fixture
def served(vault, monkeypatch):
    """The cabinets whose mailbox was started: a cabinet is served only when its login worked."""
    started = []
    monkeypatch.setattr(experio_api.mail, "start_one", lambda name: started.append(name) or True)
    return started


def login_says(monkeypatch, sentence):
    async def verdict(cabinet):
        return sentence

    monkeypatch.setattr(settings_page, "checked_login", verdict)


def test_the_add_cabinet_page_offers_every_setting_empty(vault):
    status, body = page_call("GET")
    keys = [f["key"] for _, items in body["sections"] for f in items]
    assert status == 200 and sorted(keys) == sorted(cabinet_settings.FIELDS) and "ODOO_PASSWORD" in keys
    assert all(f["value"] in ("", False) for _, items in body["sections"] for f in items)


def test_a_cabinet_added_on_the_page_is_checked_then_switched_on_served_and_started_in_simulation(
    vault, served, monkeypatch
):
    login_says(monkeypatch, "")
    status, body = page_call("POST", {"name": " cab_new ", **FULL, "DRY_RUN": "1"})
    assert status == 201 and body == {"name": "cab_new", "mailbox_watched": True} and served == ["cab_new"]
    assert vault["cab_new"]["DRY_RUN"] == "1" and vault["cab_new"]["ACTIVE"] == "1"


def test_a_cabinet_whose_login_fails_is_kept_deactivated_and_never_served(vault, served, monkeypatch):
    login_says(monkeypatch, "Experio a refusé ce login.")
    status, body = page_call("POST", {"name": "cab_new", **FULL})
    assert status == 400 and "refusé" in body["error"] and "désactivé" in body["error"]
    assert vault["cab_new"]["ACTIVE"] == "0" and served == []  # nothing of it ran: no mailbox, no schema
    assert "p w" not in json.dumps(body)
    login_says(monkeypatch, "")
    status, body = page_call("POST", {"name": "cab_new", **FULL, "ODOO_UID": "13"})
    assert status == 201 and vault["cab_new"]["ACTIVE"] == "1" and vault["cab_new"]["ODOO_UID"] == "13"
    assert served == ["cab_new"]


def test_a_deactivated_cabinet_added_again_keeps_what_the_form_leaves_empty(vault, served, monkeypatch):
    vault["cab_old"] |= {"IMAP_USER": "old@cab.ma", "DRY_RUN": "0", "MAIL_MANAGER": "boss@cab.ma"}
    login_says(monkeypatch, "")
    empty = dict.fromkeys(cabinet_settings.FIELDS, "")
    status, _ = page_call("POST", {"name": "cab_old", **empty, "ODOO_USER": "tarik@cab.ma"})
    assert status == 201 and vault["cab_old"]["ACTIVE"] == "1"
    assert vault["cab_old"]["IMAP_USER"] == "old@cab.ma" and vault["cab_old"]["MAIL_MANAGER"] == "boss@cab.ma"
    assert vault["cab_old"]["ODOO_PASSWORD"] == "p w"  # an empty secret is kept too


def test_a_serving_that_fails_after_the_write_deactivates_the_cabinet_and_is_recorded(vault, monkeypatch):
    recorded = []

    async def record(actor, action, cabinet, **kwargs):
        recorded.append(action)

    def broken(name):
        raise RuntimeError("postgres down")

    monkeypatch.setattr(experio_api.audit, "record", record)
    monkeypatch.setattr(experio_api.cabinets, "create_schema", broken)
    login_says(monkeypatch, "")
    with pytest.raises(RuntimeError):
        page_call("POST", {"name": "cab_new", **FULL})
    assert vault["cab_new"]["ACTIVE"] == "0" and recorded[0] == "cabinet_created"


def test_the_add_cabinet_page_refuses_a_bad_name_a_missing_setting_and_a_cabinet_already_active(
    vault, served, monkeypatch
):
    login_says(monkeypatch, "")
    assert page_call("POST", {"name": "Bad-Name", **FULL})[0] == 400
    status, body = page_call("POST", {"name": "orchestrator", **FULL})
    assert status == 400 and "réservé" in body["error"] and "orchestrator" not in vault
    status, body = page_call("POST", {"name": "cab_new", "ODOO_USER": "tarik@cab.ma"})
    assert status == 400 and "manquants" in body["error"] and "cab_new" not in vault
    assert page_call("POST", {"name": "cab_new", **FULL})[0] == 201
    status, body = page_call("POST", {"name": "cab_new", **FULL})
    assert status == 400 and "existe déjà" in body["error"]


STORED = {"ODOO_USER": "tarik@cab.ma", "ODOO_UID": "12", "ODOO_PASSWORD": "p w", "ODOO_API_KEY": "k"}


@pytest.mark.parametrize(
    ("stored", "raised", "word"),
    [
        (STORED, PermissionError("login refused for bob on db secret_db"), "refusé"),
        (STORED, settings_page.xmlrpc.client.Fault(1, "Traceback secret_db"), "base"),
        (STORED, OSError("dns secret_db"), "répond"),
        (STORED, TimeoutError("secret_db"), "répond"),
        ({**STORED, "EXPERIO_URL": "https://exp.example"} | {"ODOO_UID": "x"}, None, "répond"),
        ({k: v for k, v in STORED.items() if k != "ODOO_USER"}, None, "ODOO_USER"),
    ],
)
def test_a_failed_login_is_told_for_what_it_is_without_experio_s_own_words(vault, monkeypatch, stored, raised, word):
    vault["cab_new"] = stored

    def check(*args):
        raise raised

    monkeypatch.setattr(settings_page.experio, "check_login", check)
    said = asyncio.run(settings_page.checked_login("cab_new"))
    assert word in said and "secret_db" not in said and "Traceback" not in said


def test_the_login_is_tried_with_the_key_when_there_is_one_on_the_cabinets_own_address(vault, monkeypatch):
    vault["cab_new"] = {**STORED, "EXPERIO_URL": "https://exp.example"}
    seen = []
    monkeypatch.setattr(settings_page.experio, "check_login", lambda *args: seen.append(args) or 12)
    assert asyncio.run(settings_page.checked_login("cab_new")) == ""
    assert seen == [("cab_new", "https://exp.example", "tarik@cab.ma", "k", 12)]


def test_check_login_refuses_a_refused_login_and_another_uid_and_remembers_nothing(monkeypatch):
    answers = iter([0, 99, 12])
    closed = []

    class Proxy:
        def __init__(self, url, transport, allow_none):
            self.transport = transport

        def authenticate(self, *args):
            return next(answers)

    monkeypatch.setattr(settings_page.experio.xmlrpc.client, "ServerProxy", Proxy)
    monkeypatch.setattr(settings_page.experio.xmlrpc.client.SafeTransport, "close", lambda self: closed.append(1))
    for expected in (12, 12):
        with pytest.raises(PermissionError):
            settings_page.experio.check_login("db", "https://exp.example", "u", "k", expected)
    assert settings_page.experio.check_login("db", "https://exp.example", "u", "k", 12) == 12
    assert len(closed) == 3 and settings_page.experio.CHECK_TIMEOUT <= 30


def test_check_login_takes_the_logins_uid_when_none_is_expected(monkeypatch):
    class Proxy:
        def __init__(self, url, transport, allow_none):
            pass

        def authenticate(self, *args):
            return 77

    monkeypatch.setattr(settings_page.experio.xmlrpc.client, "ServerProxy", Proxy)
    assert settings_page.experio.check_login("db", "https://exp.example", "u", "k", 0) == 77


@pytest.mark.security
@pytest.mark.parametrize("settings", [{"LLM_CHOICE": "custom"}, {"LLM_URL": "https://x.example"}, {"LLM_KEY": "k"}])
def test_the_api_does_not_set_a_cabinet_s_own_llm_whose_address_gets_its_documents(settings):
    class Request:
        async def json(self):
            return {"settings": settings}

    with pytest.raises(ValueError, match="page Réglages"):
        asyncio.run(experio_api.payload(Request()))


def test_the_api_may_pick_a_global_llm():
    class Request:
        async def json(self):
            return {"settings": {"LLM_CHOICE": "minimax"}}

    assert asyncio.run(experio_api.payload(Request()))[1] == {"LLM_CHOICE": "minimax"}
