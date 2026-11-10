"""What Tarik remembers of his clients across conversations, kept by Hindsight (its own container, its own schema in
Tarik's Postgres: docker-compose.yml). One Hindsight bank per cabinet (named as the cabinet), so a cabinet's memories
are never read for another. Only HINDSIGHT_URL turns it on; empty, nothing here does anything.

Never an error for the caller: a Hindsight that is down or slow costs a turn nothing but its memories (logged).
Hindsight's REST API (its /openapi.json): POST /v1/default/banks/{bank}/memories to retain, .../memories/recall.
A retain is accepted at once and extracted later (async), so a failure, a revoked Claude token above all, shows only in
Hindsight's own operations: failures() reads them for the settings page."""

import asyncio
import os
from datetime import UTC, datetime

import httpx

from engine.tools.core.app import log

URL = os.getenv("HINDSIGHT_URL", "").rstrip("/")
RECALL_TOKENS = 1500  # what a turn's recall may hold at most, Hindsight's own count
RECALL_WAIT, RETAIN_WAIT = 4, 30  # seconds: a turn waits for the first, never for the second (retained in background)
QUERY_CHARS, KEPT_CHARS = 1000, 4000  # a long email's start asks and is kept, not all of it (and its quoted thread)
BACKGROUND = set()  # the retains going on: a task nobody holds can be dropped before it ends
BACKGROUND_MAX = 20  # more at once (Hindsight slow or down): the new ones are dropped, never queued without end


def on():
    """True when Hindsight is set up (HINDSIGHT_URL)."""
    return bool(URL)


async def post(bank, path, body, wait):
    """POST ``body`` to the cabinet ``bank``'s ``path`` and return Hindsight's JSON answer."""
    async with httpx.AsyncClient(timeout=wait) as client:
        answer = await client.post(f"{URL}/v1/default/banks/{bank}/{path}", json=body)
        answer.raise_for_status()
        return answer.json()


async def get(path, wait, params=None):
    """GET ``path`` (under /v1/default) from Hindsight and return its JSON answer."""
    async with httpx.AsyncClient(timeout=wait) as client:
        answer = await client.get(f"{URL}/v1/default{path}", params=params)
        answer.raise_for_status()
        return answer.json()


async def recalled(cabinet, question):
    """Return what cabinet ``cabinet`` remembers about ``question`` as one text, a fact per line; "" when nothing or
    when Hindsight is off; None when it failed (the caller does not keep that as an answer)."""
    if not on() or not question.strip():
        return ""
    try:
        found = await post(
            cabinet, "memories/recall", {"query": question[:QUERY_CHARS], "max_tokens": RECALL_TOKENS}, RECALL_WAIT
        )
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 404:  # 404: the cabinet's bank is made at its first retain, nothing before
            log(f"{cabinet} memory recall: {error}")
            return None
        return ""
    except Exception as error:
        log(f"{cabinet} memory recall: {type(error).__name__}: {error}")
        return None
    return "\n".join(f"- {fact['text']}" for fact in found["results"])


async def retained(cabinet, session, said):
    """Give Hindsight what a client said (``said``) in conversation ``session``, to keep the facts worth keeping (it
    extracts them). Only their words: Tarik's replies hold Experio's figures, which Experio keeps and changes, and
    text from untrusted tool results. Hindsight works on it in the background (async): never a reply's wait."""
    if not on() or not said.strip():
        return
    item = {
        "content": said[:KEPT_CHARS],
        "context": "Message d'un client du cabinet à Tarik, son agent comptable",
        "timestamp": datetime.now(UTC).isoformat(),
        "tags": [f"conversation:{session}"],
    }
    try:
        await post(cabinet, "memories", {"items": [item], "async": True}, RETAIN_WAIT)
    except Exception as error:
        log(f"{cabinet} memory retain: {type(error).__name__}: {error}")


def retain_later(cabinet, session, said):
    """Start ``retained`` without waiting for it: the client's reply never waits on Hindsight."""
    if len(BACKGROUND) >= BACKGROUND_MAX:
        log(f"{cabinet} memory retain dropped: {BACKGROUND_MAX} already going on")
        return
    task = asyncio.create_task(retained(cabinet, session, said))
    BACKGROUND.add(task)
    task.add_done_callback(BACKGROUND.discard)


FAILED_PER_BANK = 20


async def failures():
    """Return Hindsight's failed operations, newest first: [{cabinet, at, kind, error}], at most FAILED_PER_BANK per
    cabinet, ``error`` cut to 300 characters. [] when Hindsight is off or nothing failed; None when it cannot be
    reached (logged)."""
    if not on():
        return []
    found = []
    try:
        for bank in (await get("/banks", RECALL_WAIT))["banks"]:
            params = {"status": "failed", "exclude_parents": "true", "limit": FAILED_PER_BANK}
            for one in (await get(f"/banks/{bank['bank_id']}/operations", RECALL_WAIT, params))["operations"]:
                at = one.get("updated_at") or one["created_at"]
                found.append(
                    {
                        "cabinet": bank["bank_id"],
                        "at": at,
                        "kind": one.get("operation_type") or one["task_type"],
                        "error": (one.get("error_message") or "")[:300],
                    }
                )
    except Exception as error:
        log(f"memory failures: {type(error).__name__}: {error}")
        return None
    return sorted(found, key=lambda one: one["at"], reverse=True)
