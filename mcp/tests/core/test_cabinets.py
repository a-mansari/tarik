"""Cabinets never mix: every call runs for the one cabinet its address names, sees only that
cabinet's settings and Postgres schema, and fails when it names none. Runs against the real
Postgres with two throwaway cabinets (tarik_test_a, tarik_test_b), and a real HTTP server."""

import base64
import concurrent.futures
import contextlib
import http.client as http_client
import json
import socket
import threading
import time

import httpx
import psycopg
import pytest
import uvicorn
import xmlrpc.client
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

import server as mcp_server
from tarik.core import cabinets, connection, experio
from tarik.core.app import server
from tarik.core.cabinets import POSTGRES, cabinet_db, cabinet_names, current, setting, using
from tarik.emails import sending
from tarik.invoices import pipeline

A, B = "tarik_test_a", "tarik_test_b"


@pytest.fixture
def two_cabinets(monkeypatch):
    names = frozenset({A, B})
    monkeypatch.setattr(cabinets, "CABINETS", names)
    monkeypatch.setattr(mcp_server, "CABINETS", names)
    for name, uid in ((A, "11"), (B, "22")):
        monkeypatch.setenv(f"{name.upper()}__ODOO_UID", uid)
        monkeypatch.setenv(f"{name.upper()}__ODOO_USER", f"tarik@{name}")
        monkeypatch.setenv(f"{name.upper()}__ODOO_PASSWORD", f"secret-{name}")
        monkeypatch.setenv(f"{name.upper()}__MAIL_ALLOWED_RECIPIENTS", f"client@{name}.ma")
    return names


@pytest.fixture
def schemas(two_cabinets):
    cabinets.create_schemas()
    yield
    with psycopg.connect(**POSTGRES) as admin:
        for name in (A, B):
            admin.execute(f"drop schema if exists {name} cascade")


# --- names and scope ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["Experio_Cabinet_8", "experio cabinet", "cabinet-8", "8cabinet", "public", "pg_temp",
                                 "a;drop schema x", "a,b,a", "x" * 64, "café"])
def test_only_exact_lowercase_names_are_cabinets(bad):
    with pytest.raises(SystemExit):
        cabinet_names(bad)


def test_cabinet_names_are_read_exactly():
    assert cabinet_names("experio_cabinet_8, experio_cabinet_12") == {"experio_cabinet_8", "experio_cabinet_12"}
    assert cabinet_names("") == frozenset()


def test_nothing_runs_without_a_cabinet():
    with pytest.raises(RuntimeError):
        current()
    with pytest.raises(RuntimeError):
        setting("ODOO_UID")
    with pytest.raises(RuntimeError):
        cabinet_db()
    with pytest.raises(RuntimeError):
        experio.account()


def test_an_unknown_cabinet_is_refused(two_cabinets):
    for name in ("experio_cabinet_999", "", None, A.upper(), A + " "):
        with pytest.raises(PermissionError):
            with using(name):
                pass


def test_scopes_nest_and_restore(two_cabinets):
    with using(A):
        with using(B):
            assert current() == B
        assert current() == A
    with pytest.raises(RuntimeError):
        current()


def test_a_scope_is_restored_after_an_error(two_cabinets):
    with pytest.raises(ValueError):
        with using(A):
            raise ValueError
    with pytest.raises(RuntimeError):
        current()


# --- settings ----------------------------------------------------------------------------------

def test_settings_are_the_cabinets_own(two_cabinets, monkeypatch):
    monkeypatch.setenv("ODOO_UID", "999")  # the old, unprefixed setting: never read
    with using(A):
        assert experio.account() == (A, 11, f"tarik@{A}", f"secret-{A}")
        assert sending.allowed_recipients() == [f"client@{A}.ma"]
    with using(B):
        assert experio.account() == (B, 22, f"tarik@{B}", f"secret-{B}")
        assert sending.allowed_recipients() == [f"client@{B}.ma"]


def test_a_missing_setting_never_falls_back_to_another_cabinet(two_cabinets, monkeypatch):
    monkeypatch.delenv(f"{B.upper()}__ODOO_USER")
    monkeypatch.setenv("ODOO_USER", "tarik@unprefixed")
    with using(B), pytest.raises(KeyError):
        setting("ODOO_USER")
    with using(B):
        assert sending.recipient_allowed(f"client@{A}.ma") is False  # A's whitelist is not B's


def test_dry_run_is_per_cabinet_and_on_by_default(two_cabinets, monkeypatch):
    monkeypatch.setenv(f"{A.upper()}__DRY_RUN", "0")
    with using(A):
        assert experio.dry_run() is False
    with using(B):
        assert experio.dry_run() is True


def test_the_email_signature_is_the_cabinets_own(two_cabinets, monkeypatch):
    sending._cabinet_name.cache_clear()
    monkeypatch.setattr(experio, "odoo_call", lambda *args, **kw: [{"name": f"Cabinet {current()}"}])
    with using(A):
        assert sending.cabinet_name() == f"Cabinet {A}"
    with using(B):
        assert sending.cabinet_name() == f"Cabinet {B}"  # not A's, though A's is cached
    sending._cabinet_name.cache_clear()


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
    assert ("tarik_test_b", "fields_get") in calls  # B read its own form, not A's cached one


# --- threads -----------------------------------------------------------------------------------

def test_threads_keep_their_own_cabinet(two_cabinets):
    """Many calls at once, each in its cabinet, each checking it stays there."""
    def work(name):
        with using(name):
            for _ in range(200):
                if current() != name:
                    return False
                time.sleep(0)
        return True
    with concurrent.futures.ThreadPoolExecutor(32) as pool:
        assert all(pool.map(work, [A, B] * 200))


def test_a_new_thread_starts_with_no_cabinet(two_cabinets):
    seen = []
    with using(A):
        thread = threading.Thread(target=lambda: seen.append(cabinets._current.get(None)))
        thread.start()
        thread.join()
    assert seen == [None]


# --- Postgres ----------------------------------------------------------------------------------

def payload(session_id, *files):
    return base64.b64encode(json.dumps({"session_id": session_id, "files": [
        {"id": source, "file_name": name, "mime_type": "application/pdf", "data": base64.b64encode(data).decode()}
        for source, name, data in files]}).encode()).decode()


def test_each_cabinet_has_its_own_tables(schemas):
    with using(A):
        pipeline.save_files(payload("same-session", ("x1", "a.pdf", b"A")))
    with using(B):
        pipeline.save_files(payload("same-session", ("x1", "b.pdf", b"B")))
    with using(A), cabinet_db() as db:
        assert db.execute("select file_name, data from tarik_files").fetchall() == [("a.pdf", b"A")]
    with using(B), cabinet_db() as db:
        assert db.execute("select file_name, data from tarik_files").fetchall() == [("b.pdf", b"B")]


def test_same_ids_in_two_cabinets_are_two_files(schemas):
    """Ids restart in each schema: file 1 of A and file 1 of B are each cabinet's own."""
    with using(A):
        a = pipeline.save_files(payload("s", (None, "a.pdf", b"A")))["files"][0]["id"]
    with using(B):
        b = pipeline.save_files(payload("s", (None, "b.pdf", b"B")))["files"][0]["id"]
    assert a == b
    with using(B), cabinet_db() as db:
        assert db.execute("select data from tarik_files where id = %s", (a,)).fetchone() == (b"B",)


def test_one_cabinets_session_is_invisible_to_another(schemas):
    with using(A):
        pipeline.save_files(payload("session-of-a", (None, "facture.pdf", b"%PDF")))
        assert len(pipeline.files_to_process("session-of-a")["files"]) == 1
    with using(B):
        assert pipeline.files_to_process("session-of-a")["files"] == []
        assert pipeline.pending_questions("session-of-a")["pending"] == []


def test_the_search_path_holds_only_the_cabinet(schemas):
    with psycopg.connect(**POSTGRES) as admin:
        admin.execute("create table if not exists public.tarik_test_public_only (id int)")
    try:
        with using(A), cabinet_db() as db:
            assert db.execute("show search_path").fetchone() == (A,)
            assert db.execute("select current_schemas(false)").fetchone() == ([A],)
            # Nothing of B's, and nothing in public, is reachable by an unqualified name.
            with pytest.raises(psycopg.errors.UndefinedTable):
                db.execute("select * from tarik_test_public_only")
    finally:
        with psycopg.connect(**POSTGRES) as admin:
            admin.execute("drop table if exists public.tarik_test_public_only")


def test_a_saved_file_is_saved_once(schemas):
    with using(A):
        assert len(pipeline.save_files(payload("s", ("att-1", "a.pdf", b"A")))["files"]) == 1
        assert pipeline.save_files(payload("s", ("att-1", "a.pdf", b"A")))["files"] == []
    with using(B):  # the same attachment id in B is B's own
        assert len(pipeline.save_files(payload("s", ("att-1", "a.pdf", b"A")))["files"]) == 1


def test_the_clients_earlier_words_count_and_stay_in_their_cabinet(schemas):
    """Asked in one message, confirmed in a later one: the copy is the client's own request, in A only."""
    from tarik.dossiers.onboarding import client_messages, copy_asked, remember_client_message
    with using(A):
        remember_client_message("s", "Ouvrez VEGA avec la meme configuration que LYJA.")
        assert copy_asked(client_messages("s", "oui"), "LYJA (Test)")
    with using(B):
        assert not copy_asked(client_messages("s", "oui"), "LYJA (Test)")


# --- HTTP --------------------------------------------------------------------------------------

def which_cabinet(delay: float = 0.0) -> str:
    """Test tool: the cabinet this call runs for, after a pause that makes calls overlap."""
    time.sleep(delay)
    return current()


@contextlib.contextmanager
def serving(app):
    """``app`` on a free local port, in a thread: its base URL."""
    if "which_cabinet" not in {tool.name for tool in server._tool_manager.list_tools()}:
        server.tool()(which_cabinet)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    running = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    while not running.started:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        running.should_exit = True
        thread.join(5)


@pytest.fixture
def http(schemas):
    with serving(mcp_server.app()) as base:
        yield base


def call(base, path, tool, arguments, client=None, headers=None):
    """One tools/call over the streamable HTTP transport; the tool's result, or the HTTP status."""
    answer = (client or httpx).post(f"{base}{path}", timeout=60, headers={
        "accept": "application/json, text/event-stream", "content-type": "application/json", **(headers or {})},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}})
    if answer.status_code != 200:
        return answer.status_code
    data = next(line[5:] for line in answer.text.splitlines() if line.startswith("data:"))
    result = json.loads(data)["result"]
    assert not result.get("isError"), result
    structured = result.get("structuredContent")
    if structured is None:
        text = result["content"][0]["text"]
        return json.loads(text) if text.startswith("{") else text
    return structured["result"] if set(structured) == {"result"} else structured


@pytest.mark.parametrize("path", ["/mcp", "/mcp/", "/c//mcp", "/c/experio_cabinet_999/mcp", f"/c/{A.upper()}/mcp",
                                  f"/c/{A}/../{B}/mcp", f"/c/{A}/{B}/mcp", f"/c/{A}%2F../{B}/mcp", "/", f"/c/{A}"])
def test_only_a_known_cabinets_address_answers(http, path):
    """Sent as written: httpx would resolve "..", the server must refuse it itself."""
    host, port = http.removeprefix("http://").split(":")
    connection = http_client.HTTPConnection(host, int(port), timeout=10)
    connection.request("POST", path, body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                      "params": {"name": "which_cabinet", "arguments": {}}}),
                       headers={"accept": "application/json, text/event-stream", "content-type": "application/json"})
    assert connection.getresponse().status == 404


def test_each_address_runs_its_own_cabinet(http):
    assert call(http, f"/c/{A}/mcp", "which_cabinet", {}) == A
    assert call(http, f"/c/{B}/mcp", "which_cabinet", {}) == B


def test_overlapping_calls_never_cross(http):
    """200 calls at once, alternating cabinets, each held long enough to overlap the others."""
    jobs = [A, B] * 100
    with httpx.Client(limits=httpx.Limits(max_connections=200)) as client, \
            concurrent.futures.ThreadPoolExecutor(64) as pool:
        seen = list(pool.map(lambda name: call(http, f"/c/{name}/mcp", "which_cabinet", {"delay": 0.2}, client), jobs))
    assert seen == jobs


def test_a_session_header_cannot_carry_a_cabinet(http):
    """Stateless: no session is handed out, and one sent along changes nothing."""
    answer = httpx.post(f"{http}/c/{A}/mcp", headers={"accept": "application/json, text/event-stream"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
    assert answer.status_code == 200 and "mcp-session-id" not in answer.headers
    assert call(http, f"/c/{B}/mcp", "which_cabinet", {}, headers={"mcp-session-id": "anything"}) in (B, 400, 404)


def test_real_tools_over_http_stay_in_their_cabinet(http):
    """The same session id, saved in A over HTTP, is A's only."""
    call(http, f"/c/{A}/mcp", "save_files", {"payload": payload("shared-id", (None, "a.pdf", b"A"))})
    assert len(call(http, f"/c/{A}/mcp", "files_to_process", {"session_id": "shared-id"})["files"]) == 1
    assert call(http, f"/c/{B}/mcp", "files_to_process", {"session_id": "shared-id"})["files"] == []


# --- shared and per-cabinet settings -----------------------------------------------------------

def test_a_cabinet_setting_is_only_ever_the_cabinets_own(two_cabinets, monkeypatch):
    monkeypatch.setenv("IMAP_PASSWORD", "unprefixed")  # nobody's
    with using(A), pytest.raises(KeyError):
        setting("IMAP_PASSWORD")
    monkeypatch.setenv(f"{A.upper()}__IMAP_PASSWORD", "a's own")
    with using(A):
        assert setting("IMAP_PASSWORD") == "a's own"
    with using(B), pytest.raises(KeyError):
        setting("IMAP_PASSWORD")


def test_a_shared_setting_is_the_same_for_every_cabinet(two_cabinets, monkeypatch):
    monkeypatch.setenv("IMAP_HOST", "mail.example.ma")
    monkeypatch.setenv(f"{A.upper()}__IMAP_HOST", "ignored.example.ma")
    for name in (A, B):
        with using(name):
            assert setting("IMAP_HOST") == "mail.example.ma"
    with pytest.raises(RuntimeError):  # still only inside a cabinet's call
        setting("IMAP_HOST")


def test_an_unknown_setting_is_an_error(two_cabinets):
    with using(A), pytest.raises(KeyError):
        setting("POSTGRES_PASSWORD")


def test_misplaced_settings_stop_the_server(two_cabinets):
    good = {"IMAP_HOST": "h", f"{A.upper()}__IMAP_USER": "u", f"{B.upper()}__ODOO_UID": "2", "POSTGRES_DB": "x"}
    assert cabinets.misplaced_settings(good) == []
    bad = {**good, "IMAP_USER": "nobody's", f"{A.upper()}__SMTP_HOST": "a's", "EXPERIO_CABINET_99__ODOO_UID": "9"}
    assert cabinets.misplaced_settings(bad) == sorted(["EXPERIO_CABINET_99__ODOO_UID", "IMAP_USER", f"{A.upper()}__SMTP_HOST"])


# --- call caps ---------------------------------------------------------------------------------

def timed(base, jobs):
    """Run every (cabinet, delay) call at once: each cabinet's seconds until its last answer."""
    start, done = time.monotonic(), {}
    with httpx.Client(limits=httpx.Limits(max_connections=100)) as client, concurrent.futures.ThreadPoolExecutor(len(jobs)) as pool:
        def one(job):
            assert call(base, f"/c/{job[0]}/mcp", "which_cabinet", {"delay": job[1]}, client) == job[0]
            done[job[0]] = max(done.get(job[0], 0), time.monotonic() - start)
        list(pool.map(one, jobs))
    return done


def test_a_busy_cabinet_does_not_hold_up_another(schemas):
    """A sends 9 slow calls, 3 at a time; B's one call still runs at once."""
    with serving(mcp_server.by_cabinet(server.streamable_http_app(stateless_http=True), per_cabinet=3, total=5)) as base:
        done = timed(base, [(A, 1.0)] * 9 + [(B, 1.0)])
    assert done[A] >= 3.0 - 0.1  # three rounds of three
    assert done[B] < 2.0  # not queued behind A


def test_the_server_never_runs_more_than_its_total(schemas):
    """3 per cabinet, 4 in all: 6 calls need two rounds."""
    with serving(mcp_server.by_cabinet(server.streamable_http_app(stateless_http=True), per_cabinet=3, total=4)) as base:
        done = timed(base, [(A, 1.0)] * 3 + [(B, 1.0)] * 3)
    assert max(done.values()) >= 2.0 - 0.1


def test_more_calls_than_threads_still_all_answer(schemas):
    """The tools' thread pool is sized to the total cap: 60 overlapping calls, none lost."""
    with serving(mcp_server.by_cabinet(server.streamable_http_app(stateless_http=True), per_cabinet=60, total=60)) as base:
        done = timed(base, [(A, 1.0)] * 30 + [(B, 1.0)] * 30)
    assert max(done.values()) < 2.5  # 60 at once, not 40 then 20


# --- settings never reach the agent ------------------------------------------------------------

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
    assert len(tools) > 40
    for tool in tools:
        arguments = {name: example(spec) for name, spec in tool.parameters.get("properties", {}).items()
                     if name in tool.parameters.get("required", [])}
        answer = httpx.post(f"{http}/c/{A}/mcp", timeout=120, headers={
            "accept": "application/json, text/event-stream", "content-type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool.name, "arguments": arguments}})
        assert answer.status_code == 200, tool.name
        shown = [marker for marker in markers if marker and marker.lower() in answer.text.lower()]
        assert not shown, f"{tool.name} shows {shown}"
