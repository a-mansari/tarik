"""No tool ever shows a setting, whatever Experio, the mailbox or the model do."""

import contextlib
import threading
import time

import httpx
import pytest
import xmlrpc.client
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

from server import server  # its import registers every tool
from tarik.core import connection, experio
from tarik.core.cabinets import POSTGRES, cabinet_db, using

from shared import A, call


LEAKS = {  # a marker in every setting: none may show in anything a tool returns
    "ODOO_USER": "leak-odoo-user", "ODOO_PASSWORD": "leak-odoo-password", "ODOO_API_KEY": "leak-odoo-key",
    "IMAP_USER": "leak-imap-user@leak.invalid", "IMAP_PASSWORD": "leak-imap-password",
    "SMTP_USER": "leak-smtp-user@leak.invalid", "SMTP_PASSWORD": "leak-smtp-password", "SMTP_FROM": "leak-from@leak.invalid",
    "MAIL_MANAGER": "leak-manager@leak.invalid", "MAIL_ALLOWED_RECIPIENTS": "leak-allowed@leak.invalid",
    "AGENT_ID": "leak-agent-id",
}
SHARED_LEAKS = {"IMAP_HOST": "leak-imap-host.invalid", "SMTP_HOST": "leak-smtp-host.invalid", "IMAP_FOLDER": "LeakFolder",
                "MODEL_API_URL": "http://leak-model.invalid", "MODEL_API_KEY": "leak-model-key", "MODEL_API_NAME": "leak-model",
                "N8N_MCP_API_URL": "http://leak-n8n.invalid/mcp-server/http", "N8N_MCP_API_TOKEN": "leak-n8n-token",
                "MAIL_WEBHOOK_SECRET": "leak-webhook-secret"}


def example(schema):
    """A value of the type a tool's argument asks for."""
    kind = schema.get("type") or next((option.get("type") for option in schema.get("anyOf", []) if option.get("type") != "null"), "string")
    return {"integer": 1, "number": 1, "boolean": False, "object": {}, "array": []}.get(kind, "x")


class AnyPath(SimpleXMLRPCRequestHandler):
    rpc_paths = ()  # Odoo's /xmlrpc/2/common and /xmlrpc/2/object


@contextlib.contextmanager
def fake_experio(mode):
    """A stand-in Experio (XML-RPC): ``empty`` answers every read with nothing, ``fault`` refuses
    every call with a message of its own, ``wrong_uid`` logs Tarik in as another user."""
    def authenticate(database, user, key, context):
        return 999 if mode == "wrong_uid" else 11

    def execute_kw(database, uid, key, model, method, args, kw=None):
        if mode == "fault":
            raise xmlrpc.client.Fault(1, f"leak-fault-text {database} {model}")
        return 0 if method == "search_count" else []
    rpc = SimpleXMLRPCServer(("127.0.0.1", 0), requestHandler=AnyPath, logRequests=False, allow_none=True)
    rpc.register_function(lambda: {"server_version": "17.0"}, "version")
    rpc.register_function(authenticate, "authenticate")
    rpc.register_function(execute_kw, "execute_kw")
    thread = threading.Thread(target=rpc.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{rpc.server_address[1]}"
    finally:
        rpc.shutdown()


@pytest.mark.security
@pytest.mark.parametrize("experio_mode", ["unreachable", "empty", "fault", "wrong_uid"])
def test_no_tool_ever_shows_a_setting(http, monkeypatch, experio_mode):
    """Every tool, called with plausible arguments while the mailbox and the model are unreachable
    and Experio is unreachable, empty, refusing or logging in the wrong user, so each runs its error
    paths: no setting, nor the cabinet's database name, nor Postgres' password, nor Experio's own
    error text, appears in anything it answers."""
    for key, value in LEAKS.items():
        monkeypatch.setenv(f"{A.upper()}__{key}", value)
    for key, value in SHARED_LEAKS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)  # the waiting steps, without the wait
    with using(A), cabinet_db() as db:  # an email thread "x", so ask_agent goes as far as calling n8n
        db.execute("insert into tarik_mail_threads (thread_id, session_id, client_address, subject) values ('t', 'x', 'c', 's') "
                   "on conflict do nothing")
    markers = [*LEAKS.values(), *SHARED_LEAKS.values(), "leak-odoo-url", "leak-fault-text", "127.0.0.1", A, POSTGRES["password"]]
    with contextlib.ExitStack() as stack:
        url = "http://leak-odoo-url.invalid" if experio_mode == "unreachable" else stack.enter_context(fake_experio(experio_mode))
        for module in (experio, connection):
            monkeypatch.setattr(module, "ODOO_URL", url)
        expected = {"unreachable": "injoignable", "empty": "Connecte", "fault": "refusee", "wrong_uid": "refusee"}[experio_mode]
        assert expected in call(http, f"/c/{A}/mcp", "experio_connection", {})  # each mode really reached its path
        check_every_tool(http, markers)


def check_every_tool(http, markers):
    tools = [tool for tool in server._tool_manager.list_tools() if tool.name != "which_cabinet"]
    assert len(tools) > 30  # every tool the server has, not a few
    for tool in tools:
        arguments = {name: example(spec) for name, spec in tool.parameters.get("properties", {}).items()
                     if name in tool.parameters.get("required", [])}
        answer = httpx.post(f"{http}/c/{A}/mcp", timeout=120, headers={
            "accept": "application/json, text/event-stream", "content-type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool.name, "arguments": arguments}})
        assert answer.status_code == 200, tool.name
        shown = [marker for marker in markers if marker and marker.lower() in answer.text.lower()]
        assert not shown, f"{tool.name} shows {shown}"
