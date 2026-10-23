"""What a run was asked, as the run's page shows it: each input under its plain name, coded values in words, an email
thread as its sender, subject and message, and nothing internal."""

import asyncio
import json

import pytest

from server.api.runs import asked


def test_a_workflow_s_inputs_read_in_plain_words():
    call = {
        "mode": "files",
        "dossier": "TARIK TEST",
        "client_confirmed": True,
        "skip_ice_if_check": False,
        "session_id": "s",
        "cabinet": "c",
        "attachments": [{"data": "..."}],
        "compte": "",
    }
    shown = asked({"graph": "traitement", "input": {"call": call}})
    assert shown == {
        "Ce qui est demandé": "Traiter les fichiers joints",
        "Dossier": "TARIK TEST",
        "Confirmé par le client": "Oui",
    }


def test_an_email_thread_reads_as_its_sender_subject_and_message():
    thread = {"to": "client@cabinet.ma", "subject": "Factures", "message": "Voici", "files": ["a.pdf", "b.pdf"]}
    shown = asked({"graph": "fil", "input": {"cabinet": "c", "thread": thread, "done": []}})
    expected = {"De": "client@cabinet.ma", "Sujet": "Factures", "Message": "Voici", "Fichiers joints": "a.pdf, b.pdf"}
    assert shown == expected


def picked(cookie, path="/ui/executions"):
    return type(
        "R", (), {"cookies": {"tarik_cabinet": cookie} if cookie else {}, "url": type("U", (), {"path": path})}
    )()


def test_the_pages_show_the_cabinet_picked_else_the_first(monkeypatch):
    from server import api

    monkeypatch.setattr(api.runs, "CABINETS", {"cab_b", "cab_a"})
    monkeypatch.setattr(api.runs, "display_name", lambda cabinet: {"cab_a": "Cabinet A"}.get(cabinet, ""))
    assert api.runs.chosen(picked("cab_b")) == "cab_b"
    assert api.runs.chosen(picked("cab_gone")) == "cab_a" and api.runs.chosen(picked(None)) == "cab_a"
    shown = api.runs.sidebar(picked("cab_b", "/ui/discussion/chat-1"))
    assert [(c["name"], c["display"], c["chosen"]) for c in shown] == [
        ("cab_a", "Cabinet A", False),
        ("cab_b", "", True),
    ]


def test_picking_a_cabinet_keeps_it(monkeypatch):
    import asyncio

    from starlette.requests import Request

    from server import api

    monkeypatch.setattr(api.runs, "CABINETS", {"cab_a"})

    def pick(cabinet):
        async def receive():
            return {"type": "http.request", "body": json.dumps({"cabinet": cabinet}).encode()}

        scope = {"type": "http", "method": "POST", "path": "/ui/api/cabinet", "headers": [], "query_string": b""}
        return asyncio.run(api.runs.choose(Request(scope, receive)))

    answer = pick("cab_a")
    assert answer.status_code == 200 and "tarik_cabinet=cab_a" in answer.headers["set-cookie"]
    refused = pick("cab_unknown")
    assert refused.status_code == 400 and "set-cookie" not in refused.headers


@pytest.mark.security
@pytest.mark.parametrize(
    "target", ["/%5Cevil.example", "/%5C%5Cevil.example", "//evil.example", "https://evil.example"]
)
def test_logging_in_never_leads_to_another_site(target, monkeypatch):
    from starlette.requests import Request

    from server import server

    async def log_in(email, password):
        return "token"

    async def record(*args, **kwargs):
        pass

    monkeypatch.setattr(server.accounts, "log_in", log_in)
    monkeypatch.setattr(server.audit, "record", record)

    async def receive():
        return {"type": "http.request", "body": b'{"email": "a@experio.ma", "password": "p"}'}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/ui/api/login",
        "headers": [(b"host", b"tarik"), (b"origin", b"http://tarik")],
        "query_string": f"next={target}".encode(),
        "scheme": "http",
        "server": ("tarik", 80),
    }
    answer = json.loads(asyncio.run(server.login(Request(scope, receive))).body)
    assert answer["next"] == "/ui/accueil"  # browsers read « /\x » as « //x »


def answered(handler, path_params=None, query=b"", cookie=None):
    """A page's JSON and status, from ``handler`` called as the pages' app calls it."""
    from starlette.requests import Request

    headers = [(b"cookie", f"tarik_cabinet={cookie}".encode())] if cookie else []
    scope = {"type": "http", "method": "GET", "path": "/ui/api/x", "headers": headers, "query_string": query}
    scope["path_params"] = path_params or {}
    answer = asyncio.run(handler(Request(scope)))
    return answer.status_code, json.loads(answer.body)


def test_the_runs_list_gives_each_run_its_time_as_read(monkeypatch):
    from server import api

    async def recent(cabinet, graph, limit):
        return [{"run_id": "r1", "graph": "agent", "started": "2026-10-20T09:33:22+00:00", "depth": 0}]

    monkeypatch.setattr(api.runs, "CABINETS", {"cab_a"})
    monkeypatch.setattr(api.runs.runs, "recent", recent)
    status, shown = answered(api.runs.runs_page, cookie="cab_a")
    assert (
        status == 200
        and shown["cabinet"] == "cab_a"
        and shown["runs"][0]["when"] == api.runs.runs.when("2026-10-20T09:33:22+00:00")
    )


@pytest.mark.security
def test_a_run_s_conversation_reaches_the_page_only_as_the_server_s_markdown(monkeypatch):
    from server import api

    async def run(run_id):
        if run_id != "r1":
            return None
        messages = [
            {"type": "human", "content": "<script>alert(1)</script> **bonjour**"},
            {"type": "ai", "content": "[lien](javascript:alert(1))", "tool_calls": []},
        ]
        return {
            "run_id": "r1",
            "graph": "agent",
            "cabinet": "elsewhere",
            "session_id": "s",
            "input": {"messages": []},
            "output": {"messages": messages},
            "started": "2026-10-20T09:33:22+00:00",
            "ended": None,
            "steps": [],
        }

    monkeypatch.setattr(api.runs.runs, "run", run)
    assert answered(api.runs.run_page, {"run_id": "gone"})[0] == 404
    status, shown = answered(api.runs.run_page, {"run_id": "r1"})
    client, tarik = shown["conversation"]
    assert (
        status == 200
        and shown["run"]["when"] == api.runs.runs.when("2026-10-20T09:33:22+00:00")
        and shown["prompts"] == []
    )
    assert "<script>" not in client["text_html"] and "<strong>bonjour</strong>" in client["text_html"]
    assert "href" not in tarik["text_html"]  # an unsafe link left as words


@pytest.mark.security
def test_a_conversation_s_model_calls_are_only_the_chosen_cabinet_s(monkeypatch):
    from server import api

    asked = []

    async def calls(cabinet, session):
        asked.append(cabinet)
        return []

    monkeypatch.setattr(api.runs, "CABINETS", set())
    monkeypatch.setattr(api.runs.prompts, "calls", calls)
    assert answered(api.runs.prompt_calls_page, {"session_id": "s"}, cookie="cab_a")[0] == 404  # no cabinet served
    monkeypatch.setattr(api.runs, "CABINETS", {"cab_a"})
    assert answered(api.runs.prompt_calls_page, {"session_id": "s"}, cookie="cab_other")[0] == 404  # none of its calls
    assert asked == ["cab_a"]  # the cabinet served, never the cookie's name as is


def test_the_server_serves_the_pages_built_from_its_own_web_folder():
    from server import api

    assert (api.runs.WEB.parent / "package.json").exists()  # web/dist is built beside the app's own sources
