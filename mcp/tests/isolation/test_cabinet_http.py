"""Over HTTP: only a known cabinet's address answers, calls never cross, and the call caps hold."""

import concurrent.futures
import http.client as http_client
import json
import time

import httpx
import pytest

import server as mcp_server
from tarik.core.app import server

from shared import A, B, call, payload, serving


@pytest.mark.security
@pytest.mark.parametrize(
    "path",
    [
        "/mcp",
        "/mcp/",
        "/c//mcp",
        "/c/experio_cabinet_999/mcp",
        f"/c/{A.upper()}/mcp",
        f"/c/{A}/../{B}/mcp",
        f"/c/{A}/{B}/mcp",
        f"/c/{A}%2F../{B}/mcp",
        "/",
        f"/c/{A}",
    ],
)
def test_only_a_known_cabinets_address_answers(http, path):
    """Sent as written: httpx would resolve "..", the server must refuse it itself."""
    host, port = http.removeprefix("http://").split(":")
    connection = http_client.HTTPConnection(host, int(port), timeout=10)
    connection.request(
        "POST",
        path,
        body=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "which_cabinet", "arguments": {}}}
        ),
        headers={"accept": "application/json, text/event-stream", "content-type": "application/json"},
    )
    assert connection.getresponse().status == 404


@pytest.mark.security
def test_each_address_runs_its_own_cabinet(http):
    assert call(http, f"/c/{A}/mcp", "which_cabinet", {}) == A
    assert call(http, f"/c/{B}/mcp", "which_cabinet", {}) == B


@pytest.mark.security
def test_overlapping_calls_never_cross(http):
    """200 calls at once, alternating cabinets, each held long enough to overlap the others."""
    jobs = [A, B] * 100
    with (
        httpx.Client(limits=httpx.Limits(max_connections=200)) as client,
        concurrent.futures.ThreadPoolExecutor(64) as pool,
    ):
        seen = list(pool.map(lambda name: call(http, f"/c/{name}/mcp", "which_cabinet", {"delay": 0.2}, client), jobs))
    assert seen == jobs


@pytest.mark.security
def test_a_session_header_cannot_carry_a_cabinet(http):
    """Stateless: no session is handed out, and one sent along changes nothing."""
    answer = httpx.post(
        f"{http}/c/{A}/mcp",
        headers={"accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
    )
    assert answer.status_code == 200 and "mcp-session-id" not in answer.headers
    assert call(http, f"/c/{B}/mcp", "which_cabinet", {}, headers={"mcp-session-id": "anything"}) in (B, 400, 404)


@pytest.mark.security
def test_real_tools_over_http_stay_in_their_cabinet(http):
    """The same session id, saved in A over HTTP, is A's only."""
    call(http, f"/c/{A}/mcp", "save_files", {"payload": payload("shared-id", (None, "a.pdf", b"A"))})
    assert len(call(http, f"/c/{A}/mcp", "files_to_process", {"session_id": "shared-id"})["files"]) == 1
    assert call(http, f"/c/{B}/mcp", "files_to_process", {"session_id": "shared-id"})["files"] == []


def timed(base, jobs):
    """Run every (cabinet, delay) call at once: each cabinet's seconds until its last answer."""
    start, done = time.monotonic(), {}
    with (
        httpx.Client(limits=httpx.Limits(max_connections=100)) as client,
        concurrent.futures.ThreadPoolExecutor(len(jobs)) as pool,
    ):

        def one(job):
            assert call(base, f"/c/{job[0]}/mcp", "which_cabinet", {"delay": job[1]}, client) == job[0]
            done[job[0]] = max(done.get(job[0], 0), time.monotonic() - start)

        list(pool.map(one, jobs))
    return done


@pytest.mark.technical
def test_a_busy_cabinet_does_not_hold_up_another(schemas):
    """A sends 9 slow calls, 3 at a time; B's one call still runs at once."""
    with serving(
        mcp_server.by_cabinet(server.streamable_http_app(stateless_http=True), per_cabinet=3, total=5)
    ) as base:
        done = timed(base, [(A, 1.0)] * 9 + [(B, 1.0)])
    assert done[A] >= 3.0 - 0.1  # three rounds of three
    assert done[B] < 2.0  # not queued behind A


@pytest.mark.technical
def test_the_server_never_runs_more_than_its_total(schemas):
    """3 per cabinet, 4 in all: 6 calls need two rounds."""
    with serving(
        mcp_server.by_cabinet(server.streamable_http_app(stateless_http=True), per_cabinet=3, total=4)
    ) as base:
        done = timed(base, [(A, 1.0)] * 3 + [(B, 1.0)] * 3)
    assert max(done.values()) >= 2.0 - 0.1


@pytest.mark.technical
def test_more_calls_than_threads_still_all_answer(schemas):
    """The tools' thread pool is sized to the total cap: 60 overlapping calls, none lost."""
    with serving(
        mcp_server.by_cabinet(server.streamable_http_app(stateless_http=True), per_cabinet=60, total=60)
    ) as base:
        done = timed(base, [(A, 1.0)] * 30 + [(B, 1.0)] * 30)
    assert max(done.values()) < 2.5  # 60 at once, not 40 then 20
