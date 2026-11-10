"""Who gets in, and into what: the cabinet first, then the client's own login in that cabinet's database, then the
limits."""

import json

import pytest

from conftest import ALICE, auth, rpc, token


@pytest.mark.security
def test_a_cabinet_that_is_not_served_is_a_plain_404_and_the_credentials_are_never_tried(client, fake):
    for path in ("/cab_zzz/mcp", "/mcp", "/cab_a/other", "/../etc/mcp", "/CAB_A/mcp", "/cab_a/mcp/extra"):
        assert client.post(path, content="{}", headers=ALICE).status_code == 404, path
    assert fake.authentications == []


@pytest.mark.security
def test_a_clients_login_works_in_its_own_cabinet_only(client, fake):
    assert rpc(client, "cab_a", "tools/list").status_code == 200
    changed_url = rpc(client, "cab_b", "tools/list")  # alice does not exist in cab_b's database
    assert changed_url.status_code == 401
    assert ("cab_b", "alice") in fake.authentications
    assert rpc(client, "cab_b", "tools/list", headers=auth("bob", "kb")).status_code == 200
    assert rpc(client, "cab_a", "tools/list", headers=auth("bob", "kb")).status_code == 401


@pytest.mark.security
@pytest.mark.parametrize(
    "headers",
    [{}, {"authorization": "Bearer"}, {"authorization": "Bearer !!!"}, {"authorization": "Token abc"},
     {"authorization": f"Bearer {token('alice', '')}"}, {"authorization": f"Bearer {token('', 'ka')}"},
     {"authorization": "Bearer " + "QQ" * 400}, {"authorization": "Bearer YWxpY2U="}],
)  # fmt: skip
def test_a_missing_or_malformed_credential_is_refused_without_asking_experio(client, fake, headers):
    answer = client.post("/cab_a/mcp", content="{}", headers=headers)
    assert answer.status_code == 401 and answer.headers["www-authenticate"].startswith("Bearer")
    assert fake.authentications == []


@pytest.mark.functionality
def test_bearer_basic_and_the_url_safe_form_all_carry_the_same_credential(client):
    assert rpc(client, "cab_a", "tools/list", headers=auth("alice", "ka", "Basic")).status_code == 200
    assert rpc(client, "cab_a", "tools/list", headers=auth("alice", "ka", "bearer")).status_code == 200
    odd = auth("alice", "k??>>a")  # a key whose base64 holds + and /: only the right key opens
    assert rpc(client, "cab_a", "tools/list", headers=odd).status_code == 401


@pytest.mark.security
def test_wrong_keys_pause_the_login_and_the_address_even_for_the_right_key(client, fake):
    for _ in range(3):
        assert rpc(client, "cab_a", "tools/list", headers=auth("alice", "wrong")).status_code == 401
    paused = rpc(client, "cab_a", "tools/list", headers=ALICE)  # the right key, but the login is paused
    assert paused.status_code == 429 and "retry-after" in paused.headers
    calls = len(fake.authentications)
    rpc(client, "cab_a", "tools/list", headers=ALICE)
    assert len(fake.authentications) == calls  # a paused request never reaches Experio: no oracle, no lockout


@pytest.mark.security
def test_guessing_many_logins_from_one_address_pauses_the_address(client):
    for login in ("a", "b", "c"):
        assert rpc(client, "cab_a", "tools/list", headers=auth(login, "x")).status_code == 401
    assert rpc(client, "cab_a", "tools/list", headers=auth("carol", "kc")).status_code == 429


@pytest.mark.security
def test_a_right_login_clears_its_failures(client):
    rpc(client, "cab_a", "tools/list", headers=auth("carol", "bad"))
    rpc(client, "cab_a", "tools/list", headers=auth("carol", "bad"))
    assert rpc(client, "cab_a", "tools/list", headers=auth("carol", "kc")).status_code == 200


@pytest.mark.security
def test_a_credential_has_a_rate(client, monkeypatch):
    from experio_client_mcp import config

    monkeypatch.setattr(config, "RATE_PER_MINUTE", 3)
    codes = [rpc(client, "cab_a", "tools/list").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert rpc(client, "cab_a", "tools/list", headers=auth("carol", "kc")).status_code == 200  # another user's own rate


@pytest.mark.security
def test_a_login_is_remembered_for_a_few_minutes_only(client, app, fake):
    rpc(client, "cab_a", "tools/list")
    rpc(client, "cab_a", "tools/list")
    assert len(fake.authentications) == 1
    app.logins = {mark: (session, 0) for mark, (session, _) in app.logins.items()}  # all expired
    rpc(client, "cab_a", "tools/list")
    assert len(fake.authentications) == 2
    assert "ka" not in repr(list(app.logins))  # the fingerprints hold no key


@pytest.mark.security
def test_only_post_is_served_and_https_is_required_behind_a_proxy(client, monkeypatch):
    from experio_client_mcp import config

    assert client.get("/cab_a/mcp", headers=ALICE).status_code == 405
    assert client.delete("/cab_a/mcp", headers=ALICE).status_code == 405
    monkeypatch.setattr(config, "TRUST_PROXY", True)
    plain = rpc(client, "cab_a", "tools/list", headers={**ALICE, "x-forwarded-proto": "http"})
    assert plain.status_code == 403
    assert rpc(client, "cab_a", "tools/list", headers={**ALICE, "x-forwarded-proto": "https"}).status_code == 200


@pytest.mark.security
def test_the_health_check_tells_nothing(client):
    answer = client.get("/healthz")
    assert answer.status_code == 200 and answer.json() == {"status": "ok"}


@pytest.mark.security
def test_a_host_the_server_is_not_known_by_is_refused(fake_server, monkeypatch):
    from starlette.testclient import TestClient

    from experio_client_mcp import config, gateway, server

    url, _ = fake_server
    monkeypatch.setattr(config, "PUBLIC_HOSTS", ["mcp.example.com"])
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json", **ALICE}
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    for host, accepted in (("evil.example", False), ("mcp.example.com", True)):
        app = gateway.Gateway(server.create_mcp().streamable_http_app(), {"cab_a": url})  # one lifespan per MCP app
        with TestClient(app, base_url=f"http://{host}") as c:
            status = c.post("/cab_a/mcp", content=body, headers=headers).status_code
            assert (status == 200) is accepted, (host, status)


@pytest.mark.security
def test_a_bad_cabinets_file_stops_the_server_before_it_starts(tmp_path, monkeypatch):
    from experio_client_mcp import config

    monkeypatch.setattr(config, "ALLOW_INSECURE", False)
    for text in (
        "",
        "[]",
        "{}",
        '{"Bad-Name": {"url": "https://x"}}',
        '{"cab_a": {"url": "http://x"}}',
        '{"cab_a": {}}',
        "nope",
    ):
        path = tmp_path / "c.json"
        path.write_text(text)
        with pytest.raises(ValueError):
            config.load_cabinets(path)
    path.write_text('{"cab_a": {"url": "https://experio.example/"}}')
    assert config.load_cabinets(path) == {"cab_a": "https://experio.example"}


@pytest.mark.security
def test_a_burst_of_wrong_keys_for_one_login_reaches_experio_only_until_the_pause(client, fake):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(20) as pool:
        codes = list(pool.map(lambda n: rpc(client, "cab_a", "tools/list", headers=auth("alice", f"w{n}")).status_code,
                              range(20)))  # fmt: skip
    assert len(fake.authentications) == 3  # FAILED_MAX: the others find the pause
    assert codes.count(401) == 3 and codes.count(429) == 17


@pytest.mark.security
def test_a_paused_login_does_not_lock_its_owner_out_while_the_session_is_remembered(client):
    assert rpc(client, "cab_a", "tools/list").status_code == 200
    for _ in range(3):
        rpc(client, "cab_a", "tools/list", headers=auth("alice", "wrong"))  # an attacker pauses the login
    assert rpc(client, "cab_a", "tools/list").status_code == 200  # the owner's remembered key still works


@pytest.mark.security
def test_an_experio_error_at_login_is_not_a_wrong_key(client, fake):
    fake.auth_fault = "psycopg2.OperationalError: too many connections"
    for _ in range(5):
        assert rpc(client, "cab_a", "tools/list").status_code == 503
    fake.auth_fault = None
    assert rpc(client, "cab_a", "tools/list").status_code == 200  # nothing was counted against the client


@pytest.mark.security
def test_a_request_body_over_the_cap_is_refused(client, monkeypatch):
    from experio_client_mcp import config

    monkeypatch.setattr(config, "MAX_BODY", 100)
    assert client.post("/cab_a/mcp", content="x" * 200, headers=ALICE).status_code == 413


@pytest.mark.security
def test_an_ipv6_address_stands_for_its_whole_64_and_the_counters_stay_bounded(app, monkeypatch):
    from experio_client_mcp import limits

    seen = {app.address({"client": (f"2001:db8:1:2::{n:x}", 1)}, {}) for n in range(1, 50)}
    assert seen == {"2001:db8:1:2::/64"}
    now = [0.0]
    failures = limits.Failures(lambda: now[0])
    monkeypatch.setattr(limits, "MAX_KEYS", 20)
    for n in range(20):
        failures.failed(("cab_a", f"x{n}"))
    now[0] = 10_000  # all out of the window
    for n in range(20):
        failures.failed(("cab_a", f"y{n}"))
    assert len(failures.seen) <= 21


@pytest.mark.security
def test_a_cabinet_whose_threads_are_all_busy_refuses_at_once():
    import anyio

    from experio_client_mcp.experio import Refusal
    from experio_client_mcp.limits import Capacity

    async def main():
        capacity = Capacity()
        capacity.cabinets["cab_a"] = anyio.CapacityLimiter(1)
        await capacity.cabinets["cab_a"].acquire()
        with pytest.raises(Refusal, match="sollicité"):
            await capacity.run("cab_a", lambda: 1)
        assert await capacity.run("cab_b", lambda: 2) == 2  # another cabinet is not held up

    anyio.run(main)


@pytest.mark.security
def test_a_second_api_key_brings_no_second_allowance(client, monkeypatch):
    from experio_client_mcp import config

    monkeypatch.setattr(config, "RATE_PER_MINUTE", 2)
    assert [rpc(client, "cab_a", "tools/list").status_code for _ in range(2)] == [200, 200]
    other_key = auth("ALICE", "ka")  # the same user, the login in another case
    assert rpc(client, "cab_a", "tools/list", headers=other_key).status_code in (401, 429)
