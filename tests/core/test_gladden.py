"""Gladden's classification: its callback, taken only for a document waiting on it; no answer, no type."""

import threading

import httpx
import pytest
from starlette.testclient import TestClient

from tarik.core import gladden

RUNNER = "https://runner.example/gladden"


def answering(monkeypatch, callback):
    """Gladden's runner, faked: it POSTs ``callback`` for the token it was sent, as the route would hand it over."""
    sent = []

    def submit(url, pdf, token, response_uri):
        sent.append(response_uri)
        if callback is not None:
            threading.Timer(0.05, gladden.answered, (token, {"params": {"token": token, "results": callback}})).start()
        return {"id": 1, "token": token, "state": 1}

    monkeypatch.setattr(gladden, "submit", submit)
    return sent


@pytest.mark.functionality
def test_gladden_type_comes_back_from_its_callback(monkeypatch):
    sent = answering(monkeypatch, {"type": "bank", "sub_type": "bmce"})
    assert gladden.classify(RUNNER, b"%PDF", "https://tarik.example/gladden/") == "bank"
    base = "https://tarik.example/gladden/"
    assert sent[0].startswith(base) and len(sent[0]) > len(base) + 20  # a random token, the route's only key
    assert gladden._waiting == {}  # nothing left waiting


@pytest.mark.functionality
def test_no_callback_or_no_runner_means_no_type(monkeypatch):
    answering(monkeypatch, None)
    assert gladden.classify(RUNNER, b"%PDF", "https://tarik.example/gladden", wait=0.1) is None

    def down(*args):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(gladden, "submit", down)
    assert gladden.classify(RUNNER, b"%PDF", "https://tarik.example/gladden") is None
    assert gladden._waiting == {}


@pytest.mark.security
def test_the_open_callback_route_takes_only_a_token_waiting_on_it():
    from app.server import app

    client = TestClient(app)  # no lifespan: only the route is under test
    callback = {"params": {"results": {"type": "invoice"}}}
    assert client.post("/gladden/guessed", json=callback).status_code == 404
    assert client.post("/gladden/guessed", content=b"not json").status_code == 400
    gladden._waiting["t0k3n"] = (threading.Event(), {})
    try:
        assert client.post("/gladden/t0k3n", json=callback).status_code == 200
        assert gladden._waiting["t0k3n"][0].is_set() and gladden._waiting["t0k3n"][1]["type"] == "invoice"
    finally:
        gladden._waiting.pop("t0k3n")
