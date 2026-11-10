"""A fake Experio over real XML-RPC (two cabinets, each with its users and keys) and the whole ASGI app in front of
it, so
what is tested is what a client's agent meets: the gateway, the session, the tools, Experio's refusals."""

import base64
import json
import os
import threading
import xmlrpc.client
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

os.environ["EXPERIO_CLIENT_MCP_ALLOW_INSECURE"] = "1"  # the fake speaks http: set before the config is read
os.environ["EXPERIO_CLIENT_MCP_HOSTS"] = ""

import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from experio_client_mcp import config, experio, gateway, server  # noqa: E402

# Each cabinet is a database: its users (login -> key, uid, the companies they may open) and its dossiers.
DATABASES = {
    "cab_a": {
        "users": {"alice": ("ka", 11, [5, 6]), "carol": ("kc", 13, [5])},
        "dossiers": {5: "ALPHA", 6: "BETA", 7: "GAMMA"},
    },
    "cab_b": {"users": {"bob": ("kb", 22, [9])}, "dossiers": {9: "OMEGA"}},
}


class Experio:
    """The fake: records every call (who, what) and answers by model and method, never more than the user may see."""

    def __init__(self):
        self.calls, self.authentications, self.fault, self.auth_fault = [], [], None, None
        self.invoices = [{"id": 1, "invoice_number": "F1", "partner_id": [3, "ATLAS"], "state": "post"}]
        self.lines, self.invoice_groups = [], []
        # account code -> (debit, credit): what the books of every dossier hold
        self.accounts = {"71110000": (0.0, 100.0), "51410000": (0.0, 50.0), "34550000": (50.0, 0.0),
                         "34560000": (30.0, 0.0), "44550000": (0.0, 200.0), "34210000": (120.0, 0.0)}  # fmt: skip

    def user(self, db, uid):
        users = DATABASES[db]["users"]
        return next((login, data) for login, data in users.items() if data[1] == uid)

    def authenticate(self, db, login, key, _context):
        self.authentications.append((db, login))
        if self.auth_fault:
            raise xmlrpc.client.Fault(1, self.auth_fault)
        if db not in DATABASES:
            raise xmlrpc.client.Fault(1, "database does not exist")
        found = DATABASES[db]["users"].get(login)
        return found[1] if found and found[0] == key else False

    def execute_kw(self, db, uid, key, model, method, args, kw):
        login, (secret, _, companies) = self.user(db, uid)
        if key != secret:
            raise xmlrpc.client.Fault(3, "Access Denied")
        self.calls.append({"db": db, "login": login, "model": model, "method": method, "args": args, "kw": kw})
        if self.fault:
            raise xmlrpc.client.Fault(1, self.fault)
        dossiers = DATABASES[db]["dossiers"]
        domain = args[0] if args else []
        if (model, method) == ("res.users", "read"):
            return [{"id": uid, "name": login.title(), "company_ids": companies}]
        if (model, method) == ("res.company", "search_read"):
            wanted = next((t[2] for t in domain if t[0] == "id" and t[1] == "="), None)
            ids = [wanted] if wanted else next((t[2] for t in domain if t[0] == "id" and t[1] == "in"), [])
            return [{"id": i, "name": dossiers[i]} for i in ids if i in dossiers]
        if (model, method) == ("exp.fiscal.entries", "search_read"):
            return [{"name": "Exercice 2026", "date_start": "2026-01-01", "date_end": "2026-12-31", "state": "open"}]
        if (model, method) == ("exp.invoice", "search_read"):
            return self.invoices
        if (model, method) == ("exp.invoice", "read_group"):
            return self.invoice_groups
        if (model, method) == ("exp.account", "search"):
            return [1] if domain[0][2].startswith(("342", "441", "442")) else []
        if method == "search":
            return []
        if (model, method) == ("exp.move.line", "read_group") and "amount_residual:sum" in args[1]:
            return (
                [{"partner_id": [3, "ATLAS"], "amount_residual": 30.0, "__count": 1}]
                if args[2]
                else [{"amount_residual": 30.0}]
            )
        if (model, method) == ("exp.move.line", "read_group") and args[2] == ["account_id"]:
            wanted = tuple(t[2][:-1] for t in domain if t[0] == "account_id.code") or ("",)
            return [{"account_id": [n, f"{code} Compte"], "debit": d, "credit": c, "__count": 1}
                    for n, (code, (d, c)) in enumerate(self.accounts.items(), 1)
                    if code.startswith(wanted)]  # fmt: skip
        if (model, method) == ("exp.move.line", "read_group"):
            return [{"account_id": [1, "71110000 Ventes"], "debit": 0.0, "credit": 100.0, "__count": 2,
                     "date:month": "janvier 2026", "__range": {"date:month": {"from": "2026-01-01"}}}]  # fmt: skip
        if (model, method) == ("exp.move.line", "search_read"):
            return self.lines
        return []


@pytest.fixture(scope="session")
def fake_server():
    """The fake Experio listening on a local port, shared by the run: yields (url, the Experio)."""
    experio_fake = Experio()

    class Handler(SimpleXMLRPCRequestHandler):
        rpc_paths = ("/xmlrpc/2/common", "/xmlrpc/2/object")

    server_ = SimpleXMLRPCServer(("127.0.0.1", 0), requestHandler=Handler, logRequests=False, allow_none=True)
    server_.register_function(experio_fake.authenticate, "authenticate")
    server_.register_function(experio_fake.execute_kw, "execute_kw")
    threading.Thread(target=server_.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server_.server_address[1]}", experio_fake
    server_.shutdown()


@pytest.fixture
def fake(fake_server):
    url, experio_fake = fake_server
    experio_fake.calls.clear()
    experio_fake.authentications.clear()
    experio_fake.fault, experio_fake.auth_fault = None, None
    experio_fake.lines, experio_fake.invoice_groups = [], []
    return experio_fake


@pytest.fixture
def app(fake_server, fake):
    """The whole application serving cab_a and cab_b, with fresh limits and no remembered login."""
    url, _ = fake_server
    experio._local.__dict__.clear()
    return gateway.Gateway(server.create_mcp().streamable_http_app(), {"cab_a": url, "cab_b": url})


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://mcp.test", headers={"accept": "application/json, text/event-stream"}) as c:
        yield c


def token(login, key):
    return base64.b64encode(f"{login}:{key}".encode()).decode()


def auth(login, key, scheme="Bearer"):
    return {"authorization": f"{scheme} {token(login, key)}"}


ALICE = auth("alice", "ka")


def rpc(client, cabinet, method, params=None, headers=ALICE, id_=1):
    """POST one JSON-RPC message to /<cabinet>/mcp and return the response."""
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    return client.post(
        f"/{cabinet}/mcp", content=json.dumps(body), headers={"content-type": "application/json", **headers}
    )


def call(client, cabinet, tool, arguments=None, headers=ALICE):
    """Call a tool; return (is_error, the text or the data)."""
    answer = rpc(client, cabinet, "tools/call", {"name": tool, "arguments": arguments or {}}, headers)
    assert answer.status_code == 200, answer.text
    result = answer.json()["result"]
    text = result["content"][0]["text"]
    return result.get("isError", False), (text if result.get("isError") else json.loads(text))


@pytest.fixture(autouse=True)
def small_limits(monkeypatch):
    monkeypatch.setattr(config, "FAILED_MAX", 3)
    monkeypatch.setattr(config, "RATE_PER_MINUTE", 1000)
