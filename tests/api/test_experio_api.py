"""The Experio app's API (experio_api.py): only its key opens it, a new cabinet needs every setting Tarik uses, an
existing one is never created twice, a deactivated one comes back with a create, and no answer ever holds a secret."""

import asyncio
import json

import pytest
from starlette.requests import Request

from server import cabinet_settings, experio_api, server

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
