"""Gladden: Experio's document classifier (LayoutLMv3), on its runner. identify_document asks it first when
GLADDEN_URL and GLADDEN_CALLBACK_URL are set (Vault secret/tarik/shared). The model
(engine/tools/invoices/classify.py) decides when Gladden gives no type, and checks every answer other than
"invoice" on a PDF (engine/tools/invoices/intake.py).

Asynchronous: a POST only records the document (state 1, type and sub_type still null) and the classification is
POSTed later to the ``response_uri`` given, matched by ``token``. Each POST makes a new record, even for a token
already sent, and the runner offers no way to ask for a result: the callback is the only answer.

The callback, 5 to 30 s later, as seen on the test runner (not the shape its documentation gives; no confidence):
    {"params": {"token": "...", "results": {"type": "invoice" | "bank", "sub_type": "bmce" | "sgeneral" | null}}}
A document Gladden fails on (not a PDF, or some real invoices) gets no callback at all: wait with a time limit.
A callback refused (any non-2xx) is sent again 10 times, 5 s apart, then dropped."""

import base64
import secrets
import threading
from typing import Final

import httpx

from engine.tools.core.app import log

# Cloudflare in front of the runner refuses some clients' default User-Agent (error 1010, Python's urllib among them).
HEADERS: Final = {"User-Agent": "tarik/1.0"}
WAIT: Final = 60  # seconds for the callback; the test runner answered in 5 to 30

# The documents waiting for their callback, in this process (the server's tools run in it): token -> (event, results).
# shortcut: a restart drops the waiting ones (their file falls back to the model), fine while a wait is one minute.
_waiting = {}


def submit(url, pdf, token, response_uri):
    """Send PDF bytes ``pdf`` to Gladden at ``url``, its result to be POSTed to ``response_uri`` under ``token``.
    Returns the record the runner made ({"id", "token", "state": 1, "type": None, "sub_type": None, "file",
    "response_uri"}). Raises httpx.HTTPError when the runner refuses or can't be reached."""
    file = base64.b64encode(pdf).decode()
    body = [{"file": file, "token": token, "response_uri": response_uri, "doctype": "gladden"}]
    answer = httpx.post(url, json=body, headers=HEADERS, timeout=60)
    answer.raise_for_status()
    return answer.json()[0]


def classify(url, pdf, callback_url, wait=WAIT):
    """Return Gladden's (type, sub_type) for PDF bytes ``pdf``, waiting for its callback: type "invoice", "bank"...,
    sub_type a statement's bank as Experio's bank code ("bmce", "sgeneral"), the doctype its reading takes.

    Args:
        url: Gladden's address on its runner (GLADDEN_URL).
        callback_url: this server's /gladden/ route as the runner reaches it; the callback goes to
            <callback_url>/<token>.
        wait: seconds to wait for the callback.

    Returns (None, None) when the document can't be sent or no callback comes in ``wait`` seconds; each is None when
    Gladden gives none.
    The token is random: it is the only key the open callback route checks."""
    token = secrets.token_urlsafe(24)
    event = threading.Event()
    _waiting[token] = (event, {})
    try:
        submit(url, pdf, token, f"{callback_url.rstrip('/')}/{token}")
        if not event.wait(wait):
            log(f"gladden: no answer in {wait} s")
            return None, None
        _event, results = _waiting[token]
        log(f"gladden: type {results.get('type')}, sub_type {results.get('sub_type')}")
        return results.get("type") or None, results.get("sub_type") or None
    except httpx.HTTPError as error:
        log(f"gladden: {type(error).__name__}: {error}")
        return None, None
    finally:
        _waiting.pop(token, None)


def answered(token, callback):
    """Hand Gladden's ``callback`` (its JSON body) to the classify call waiting on ``token``. True when one was
    waiting, False otherwise."""
    waiting = _waiting.get(token)
    if waiting is None:
        return False
    event, results = waiting
    given = (callback.get("params") or {}).get("results") or {}
    results.update(type=given.get("type"), sub_type=given.get("sub_type"))
    event.set()
    return True
