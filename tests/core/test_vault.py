"""Settings from Vault: the AppRole login, the shared and per-cabinet paths, the refresh, an outage,
and settings found where they don't belong. Against a stand-in Vault (HTTP)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from tarik.core import cabinets

SECRETS = {
    "shared": {"IMAP_HOST": "mail.example.ma", "N8N_MCP_API_TOKEN": "t"},
    "cabinets/experio_cabinet_8": {"ODOO_UID": "430", "IMAP_PASSWORD": "p8"},
    "cabinets/experio_cabinet_60": {"ODOO_UID": "12", "IMAP_PASSWORD": "p60"},
}


class FakeVault(BaseHTTPRequestHandler):
    secrets, logins, down, token_ttl = SECRETS, [], [False], [3600]

    def log_message(self, *args):
        pass

    def answer(self, code, body=None):
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body or {}).encode())

    def do_POST(self):
        ids = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.path != "/v1/auth/approle/login" or ids != {"role_id": "role", "secret_id": "secret"}:
            return self.answer(400)
        self.logins.append(1)
        self.answer(200, {"auth": {"client_token": f"token-{len(self.logins)}", "lease_duration": self.token_ttl[0]}})

    def allowed(self):
        if self.down[0]:
            self.answer(503)
            return False
        if self.headers.get("X-Vault-Token") != f"token-{len(self.logins)}":
            self.answer(403)
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        path = self.path.removeprefix("/v1/secret/data/tarik/")
        if path not in self.secrets:
            return self.answer(404)
        self.answer(200, {"data": {"data": self.secrets[path]}})

    def do_LIST(self):
        if not self.allowed():
            return
        names = [path.removeprefix("cabinets/") for path in self.secrets if path.startswith("cabinets/")]
        self.answer(200, {"data": {"keys": names}})


@pytest.fixture
def fake(monkeypatch, tmp_path):
    (tmp_path / "role_id").write_text("role\n")
    (tmp_path / "secret_id").write_text("secret\n")
    FakeVault.secrets = {path: dict(values) for path, values in SECRETS.items()}
    FakeVault.logins, FakeVault.down, FakeVault.token_ttl = [], [False], [3600]
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeVault)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(cabinets, "VAULT_ADDRESS", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(cabinets, "VAULT_ENABLED", True)
    monkeypatch.setattr(cabinets, "VAULT_APPROLE", str(tmp_path))
    monkeypatch.setattr(cabinets, "_vault_state", {"token": "", "token_until": 0.0, "values": None, "next_read": 0.0})
    monkeypatch.setattr(cabinets, "CABINETS", frozenset({"experio_cabinet_8", "experio_cabinet_60"}))
    yield FakeVault
    server.shutdown()


@pytest.mark.security
def test_each_cabinet_reads_its_own_path_and_the_shared_one(fake):
    with cabinets.using("experio_cabinet_8"):
        assert (cabinets.setting("ODOO_UID"), cabinets.setting("IMAP_PASSWORD"), cabinets.setting("IMAP_HOST")) == (
            "430",
            "p8",
            "mail.example.ma",
        )
    with cabinets.using("experio_cabinet_60"):
        assert (cabinets.setting("ODOO_UID"), cabinets.setting("IMAP_PASSWORD"), cabinets.setting("IMAP_HOST")) == (
            "12",
            "p60",
            "mail.example.ma",
        )


@pytest.mark.security
def test_the_environment_is_not_read_with_vault(fake, monkeypatch):
    monkeypatch.setenv("EXPERIO_CABINET_8__SMTP_USER", "from-env")
    with cabinets.using("experio_cabinet_8"), pytest.raises(KeyError):
        cabinets.setting("SMTP_USER")


@pytest.mark.technical
def test_the_cabinets_are_the_vaults_paths(fake):
    assert cabinets.cabinet_names(",".join(cabinets.vault_settings()[1])) == {"experio_cabinet_8", "experio_cabinet_60"}


@pytest.mark.technical
def test_a_changed_secret_applies_at_the_next_read(fake, monkeypatch):
    with cabinets.using("experio_cabinet_8"):
        assert cabinets.setting("IMAP_PASSWORD") == "p8"
        fake.secrets["cabinets/experio_cabinet_8"]["IMAP_PASSWORD"] = "rotated"
        assert cabinets.setting("IMAP_PASSWORD") == "p8"  # read once, kept 5 minutes
        cabinets._vault_state["next_read"] = 0.0
        assert cabinets.setting("IMAP_PASSWORD") == "rotated"


@pytest.mark.technical
def test_one_login_per_token_then_a_new_one_when_it_expires(fake):
    cabinets.vault_settings()
    cabinets._vault_state["next_read"] = 0.0
    cabinets.vault_settings()
    assert len(fake.logins) == 1  # the token lives on: no login per read
    cabinets._vault_state.update(token_until=0.0, next_read=0.0)  # expired
    cabinets.vault_settings()
    assert len(fake.logins) == 2


@pytest.mark.technical
def test_an_outage_keeps_the_last_values(fake):
    cabinets.vault_settings()
    fake.down[0] = True
    cabinets._vault_state["next_read"] = 0.0
    with cabinets.using("experio_cabinet_8"):
        assert cabinets.setting("IMAP_PASSWORD") == "p8"


@pytest.mark.technical
def test_no_vault_at_start_stops_the_server(fake):
    fake.down[0] = True
    with pytest.raises(httpx.HTTPStatusError):
        cabinets.vault_settings(wait=0)


@pytest.mark.security
def test_a_wrong_approle_is_refused(fake, tmp_path):
    (tmp_path / "secret_id").write_text("guessed")
    with pytest.raises(httpx.HTTPStatusError):
        cabinets.vault_settings()


@pytest.mark.security
def test_settings_in_the_wrong_place_stop_the_server():
    good = ({"IMAP_HOST": "h"}, {"experio_cabinet_8": {"ODOO_UID": "1"}}, {"ODOO_URL": "u", "POSTGRES_DB": "x"})
    assert cabinets.misplaced_in_vault(*good) == []
    bad = (
        {"IMAP_HOST": "h", "ODOO_PASSWORD": "shared?"},
        {"experio_cabinet_8": {"ODOO_UID": "1", "IMAP_HOST": "own?", "TYPO": "x"}},
        {"ODOO_URL": "u", "IMAP_PASSWORD": "left", "EXPERIO_CABINET_8__ODOO_UID": "left", "TARIK_CABINETS": "x"},
    )
    assert cabinets.misplaced_in_vault(*bad) == sorted(
        [
            "tarik/shared:ODOO_PASSWORD",
            "tarik/cabinets/experio_cabinet_8:IMAP_HOST",
            "tarik/cabinets/experio_cabinet_8:TYPO",
            "IMAP_PASSWORD",
            "EXPERIO_CABINET_8__ODOO_UID",
            "TARIK_CABINETS",
        ]
    )
