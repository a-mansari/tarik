"""What Tarik remembers of his clients across conversations, kept by Hindsight (its own container, its own schema in
Tarik's Postgres: docker-compose.yml). One Hindsight bank per cabinet (named as the cabinet), so a cabinet's memories
are never read for another. Only HINDSIGHT_URL turns it on; empty, nothing here does anything.

Never an error for the caller: a Hindsight that is down or slow costs a turn nothing but its memories (logged).
Hindsight's REST API (its /openapi.json): POST /v1/default/banks/{bank}/memories to retain, .../memories/recall.
A retain is accepted at once and extracted later (async), so a failure, a revoked Claude token above all, shows only in
Hindsight's own operations: failures() reads them for the settings page."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx

from engine.tools.core import cabinets
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


def enabled(cabinet):
    """True when Tarik may use his memory for ``cabinet``: Hindsight is set up (on) and neither the general switch
    (MEMORY_ENABLED, the Paramètres page) nor the cabinet's own (CABINET_MEMORY_ENABLED, its Réglages page) says
    anything but "1" (a value edited by hand in Vault, "false" or "off", turns it off: a safety switch fails closed).
    Read from the settings each time (Vault's, kept in memory and read again every few minutes, and at once after a
    save), so a switch applies to the next message, with no restart."""
    if not on():
        return False
    if cabinets.VAULT_ENABLED:
        shared, every = cabinets.vault_settings()
        asked = (shared.get("MEMORY_ENABLED"), every.get(cabinet, {}).get("CABINET_MEMORY_ENABLED"))
    else:  # the tests: the environment, as cabinets.source() gives it
        every = cabinets.source()
        asked = (every.get("MEMORY_ENABLED"), every.get(f"{cabinet.upper()}__CABINET_MEMORY_ENABLED"))
    return all(str(value).strip() in ("", "1") for value in asked if value is not None)  # any other value: off


async def hindsight(client, method, path, **kwargs):
    """Send ``method`` to Hindsight's ``path`` (under /v1/default) with ``client`` and return its JSON answer."""
    answer = await client.request(method, f"{URL}/v1/default{path}", **kwargs)
    answer.raise_for_status()
    return answer.json()


async def post(bank, path, body, wait):
    """POST ``body`` to the cabinet ``bank``'s ``path`` and return Hindsight's JSON answer."""
    async with httpx.AsyncClient(timeout=wait) as client:
        return await hindsight(client, "POST", f"/banks/{quote(bank, safe='')}/{path}", json=body)


async def recalled(cabinet, question):
    """Return what cabinet ``cabinet`` remembers about ``question`` as one text, a fact per line; "" when nothing or
    when the memory is off (enabled); None when it failed (the caller does not keep that as an answer)."""
    if not enabled(cabinet) or not question.strip():
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
    if not enabled(cabinet) or not said.strip():
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
    if not enabled(cabinet):
        return
    if len(BACKGROUND) >= BACKGROUND_MAX:
        log(f"{cabinet} memory retain dropped: {BACKGROUND_MAX} already going on")
        return
    task = asyncio.create_task(retained(cabinet, session, said))
    BACKGROUND.add(task)
    task.add_done_callback(BACKGROUND.discard)


FAILED_PER_BANK, FAILED_DAYS, FAILED_WAIT = 20, 7, 6  # a failure fixed since is forgotten; the whole read waits 6 s


def moment(text):
    """Return Hindsight's ISO time ``text`` as a datetime, None when it is none or not one."""
    try:
        return datetime.fromisoformat(text)
    except TypeError, ValueError:
        return None


async def failures():
    """Return Hindsight's failed operations of the last FAILED_DAYS days, newest first: [{id, cabinet, at, kind,
    error}], at most FAILED_PER_BANK per cabinet, ``at`` an ISO time (None when Hindsight gave none), ``error`` cut to
    300 characters. [] when Hindsight is off or nothing failed; None when it cannot be reached in FAILED_WAIT seconds
    (logged). A bank whose operations cannot be read is left out (logged), the others kept."""
    if not on():
        return []
    params = {"status": "failed", "exclude_parents": "true", "limit": FAILED_PER_BANK}
    try:
        async with asyncio.timeout(FAILED_WAIT), httpx.AsyncClient(timeout=RECALL_WAIT) as client:
            banks = [bank["bank_id"] for bank in (await hindsight(client, "GET", "/banks"))["banks"]]
            listed = await asyncio.gather(
                *(
                    hindsight(client, "GET", f"/banks/{quote(bank, safe='')}/operations", params=params)
                    for bank in banks
                ),
                return_exceptions=True,
            )
    except Exception as error:
        log(f"memory failures: {type(error).__name__}: {error}")
        return None
    since, found = datetime.now(UTC) - timedelta(days=FAILED_DAYS), []
    for bank, operations in zip(banks, listed, strict=True):
        if isinstance(operations, Exception):
            log(f"{bank} memory failures: {type(operations).__name__}: {operations}")
            continue
        for one in operations.get("operations", []):
            at = moment(one.get("updated_at") or one.get("created_at"))
            if at is None or at >= since:
                found.append(
                    {
                        "id": one.get("id") or one.get("operation_id") or "",
                        "cabinet": bank,
                        "at": at and at.isoformat(),
                        "kind": one.get("operation_type") or one.get("task_type") or "",
                        "error": (one.get("error_message") or "")[:300],
                    }
                )
    return sorted(found, key=lambda one: moment(one["at"]) or datetime.min.replace(tzinfo=UTC), reverse=True)
