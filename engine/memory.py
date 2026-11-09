"""What Tarik remembers of his clients across conversations, kept by Hindsight (its own container, its own schema in
Tarik's Postgres: docker-compose.yml). One Hindsight bank per cabinet (named as the cabinet), so a cabinet's memories
are never read for another. Only HINDSIGHT_URL turns it on; empty, nothing here does anything.

Never an error for the caller: a Hindsight that is down or slow costs a turn nothing but its memories (logged).
Hindsight's REST API (its /openapi.json): POST /v1/default/banks/{bank}/memories to retain, .../memories/recall."""

import asyncio
import os
from datetime import UTC, datetime

import httpx

from engine.tools.core.app import log

URL = os.getenv("HINDSIGHT_URL", "").rstrip("/")
RECALL_TOKENS = 1500  # what a turn's recall may hold at most, Hindsight's own count
RECALL_WAIT, RETAIN_WAIT = 8, 30  # seconds: a turn waits for the first, never for the second (retained in background)
BACKGROUND = set()  # the retains going on: a task nobody holds can be dropped before it ends


def on():
    """True when Hindsight is set up (HINDSIGHT_URL)."""
    return bool(URL)


async def post(bank, path, body, wait):
    """POST ``body`` to the cabinet ``bank``'s ``path`` and return Hindsight's JSON answer."""
    async with httpx.AsyncClient(timeout=wait) as client:
        answer = await client.post(f"{URL}/v1/default/banks/{bank}/{path}", json=body)
        answer.raise_for_status()
        return answer.json()


async def recalled(cabinet, question):
    """Return what cabinet ``cabinet`` remembers about ``question`` as one text, a fact per line; "" when nothing,
    when Hindsight is off or when it fails."""
    if not on() or not question.strip():
        return ""
    try:
        found = await post(cabinet, "memories/recall", {"query": question, "max_tokens": RECALL_TOKENS}, RECALL_WAIT)
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 404:  # 404: the cabinet's bank is made at its first retain, nothing before
            log(f"{cabinet} memory recall: {error}")
        return ""
    except Exception as error:
        log(f"{cabinet} memory recall: {type(error).__name__}: {error}")
        return ""
    return "\n".join(f"- {fact['text']}" for fact in found["results"])


async def retained(cabinet, session, said, answer):
    """Give Hindsight a client's message ``said`` and Tarik's ``answer`` in conversation ``session``, to keep the
    facts worth keeping (it extracts them). It works on it in the background (async): a slow extraction never holds
    a reply."""
    if not on():
        return
    item = {
        "content": f"Client : {said}\n\nTarik : {answer}",
        "context": "Échange d'un client du cabinet avec Tarik, son agent comptable",
        "timestamp": datetime.now(UTC).isoformat(),
        "tags": [f"conversation:{session}"],
    }
    try:
        await post(cabinet, "memories", {"items": [item], "async": True}, RETAIN_WAIT)
    except Exception as error:
        log(f"{cabinet} memory retain: {type(error).__name__}: {error}")


def retain_later(cabinet, session, said, answer):
    """Start ``retained`` without waiting for it: the client's reply never waits on Hindsight."""
    task = asyncio.create_task(retained(cabinet, session, said, answer))
    BACKGROUND.add(task)
    task.add_done_callback(BACKGROUND.discard)
