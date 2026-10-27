"""The prompt log: what the agent is sent at each model call, for the LLM Calls pages.

Saved in the cabinet's own tarik_prompt_calls and tarik_prompt_parts (engine/sql/cabinet_schema.sql). Each distinct
piece (the system text, the tools, one message) is kept once by its hash, so a growing conversation is not stored
again at every call. Saving never fails a model call (it only waits for the insert, so the call's tokens and cost are
kept under its id); files are logged as their size."""

import asyncio
import hashlib
import json
import os
import random
import sys

from langchain.agents.middleware import wrap_model_call
from langgraph.config import get_config
from psycopg import sql

from engine import live
from engine.settings import cabinet_of, session_of
from engine.tools.core import usage
from engine.tools.core.queries import Q

KEEP_DAYS = int(os.environ.get("TARIK_PROMPTS_KEEP_DAYS") or 30)
AGENT = "Tarik - {}"


def runs():
    """Return the engine.runs module (for its pool), imported at use: runs imports the agent, which imports this
    module."""
    from engine import runs as module

    return module


TABLES = {"calls": "tarik_prompt_calls", "parts": "tarik_prompt_parts", "usage": "tarik_model_usage"}


def of(statement, cabinet):
    """Return the SQL ``statement`` (engine/sql/orchestrator.sql) with {calls}, {parts} and {usage} naming ``cabinet``'s
    tables and {runs} the run store's table."""
    named = {key: sql.Identifier(cabinet, table) for key, table in TABLES.items() if f"{{{key}}}" in statement}
    if "{runs}" in statement:  # the run store's own table, beside the cabinet's
        named["runs"] = sql.Identifier(runs().SCHEMA, "runs")
    return sql.SQL(statement).format(**named)


def plain(value):
    """Return ``value`` with any bytes, or any string over 5000 characters without a space (base64), replaced by its
    size."""
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return f"[file, {len(value)} bytes]"
    if isinstance(value, str) and len(value) > 5000 and " " not in value:
        return f"[{len(value)} characters]"
    return value


def part(value):
    """Return (hash, json_text) for one prompt piece: the JSON of plain(value) and the first 32 hex of its sha256."""
    content = json.dumps(plain(value), ensure_ascii=False, default=str)
    return hashlib.sha256(content.encode()).hexdigest()[:32], content


def message(entry):
    """Return one message as the model got it: {role, content} plus tool_calls, tool_call_id and name when set."""
    kept = {"role": entry.type, "content": entry.content}
    for key in ("tool_calls", "tool_call_id", "name"):
        if getattr(entry, key, None):
            kept[key] = getattr(entry, key)
    return kept


async def save(cabinet, session_id, agent, system, tools, messages):
    """Save one model call's prompt in ``cabinet``'s prompt tables, wake the pages showing it and return its id.

    ``agent`` is the agent's name, ``system`` the system text, ``tools`` [{name, description}], ``messages`` as
    message() gives them. About once in 100 saves, expired_removed() runs in the background. A
    failure is only printed to stderr, and None returned."""

    try:
        system_part, tools_part, *message_parts = [part(value) for value in (system, tools, *messages)]
        parts = dict([system_part, tools_part, *message_parts])
        async with runs().pool.connection() as db:
            saved = await db.execute(
                of(Q.prompt_saved, cabinet),
                (
                    list(parts),
                    list(parts.values()),
                    session_id,
                    agent,
                    system_part[0],
                    tools_part[0],
                    [h for h, _ in message_parts],
                ),
            )
            (call_id,) = await saved.fetchone()
        if random.random() < 0.01:  # now and then, off the model call's path: old calls and the parts none uses
            task = asyncio.create_task(expired_removed(cabinet))
            PENDING.add(task)
            task.add_done_callback(PENDING.discard)
        live.touched(runs().RUNNING.get(), cabinet, session_id)  # the LLM Calls pages show it at once
        return call_id
    except Exception as error:
        print(f"tarik prompts not saved: {error}", file=sys.stderr)
        return None


PENDING = set()  # the clean-ups under way (a task nobody holds may be collected before it ends)


async def expired_removed(cabinet):
    """Remove ``cabinet``'s calls older than KEEP_DAYS and the parts no call uses; a failure is only printed."""
    try:
        async with runs().pool.connection() as db:
            await db.execute(of(Q.prompt_calls_expired_removed, cabinet), (KEEP_DAYS,))
            await db.execute(of(Q.prompt_parts_unused_removed, cabinet))
    except Exception as error:
        print(f"tarik prompts not cleaned: {error}", file=sys.stderr)


@wrap_model_call
async def logged(request, handler):
    """Middleware: save each model call's prompt (when the run has a session), then make the call under its id
    (usage.PROMPT_CALL: its tokens and cost are kept with it)."""
    call_id = None
    try:
        config = get_config()
        if session := session_of(config):
            cabinet = cabinet_of(config)
            system = request.system_message.content if request.system_message else ""
            tools = [{"name": tool.name, "description": tool.description} for tool in request.tools]
            call_id = await save(
                cabinet, session, AGENT.format(cabinet), system, tools, [message(m) for m in request.messages]
            )
    except Exception as error:
        print(f"tarik prompts not saved: {error}", file=sys.stderr)
    making = usage.PROMPT_CALL.set(call_id)
    try:
        return await handler(request)
    finally:
        usage.PROMPT_CALL.reset(making)


# The UI's reads, for a cabinet this stack serves (CABINETS): never a name taken from the request as is.


async def listed(cabinet, before=None, limit=100):
    """Return up to ``limit`` of the cabinet's model calls with an id below ``before`` (all when None), latest
    first, as [{id, agent, at, session_id, messages, run_id}]: messages is the number of messages, run_id the agent
    run's id as text (None when it is gone)."""
    asked = {"cabinet": cabinet, "before": before or 2**31 - 1, "limit": limit}
    async with runs().pool.connection() as db:
        rows = await (await db.execute(of(Q.prompt_calls_listed, cabinet), asked)).fetchall()
    keys = ("id", "agent", "at", "session_id", "messages", "run_id")
    return [{**dict(zip(keys, row, strict=True)), "run_id": row[5] and str(row[5])} for row in rows]


async def calls(cabinet, session_id, since=None, until=None):
    """Return a conversation's model calls in order, as [{id, agent, at, messages}] (messages: their number);
    ``since``/``until`` (optional times) limit them to one run's time."""

    async with runs().pool.connection() as db:
        asked = (session_id, since, since, until, until)
        rows = await (await db.execute(of(Q.prompt_calls_of, cabinet), asked)).fetchall()
    return [{"id": i, "agent": agent, "at": at, "messages": n} for i, agent, at, n in rows]


async def prompt(cabinet, call_id):
    """Return one call's whole prompt as {agent, at, session_id, system, tools, messages}; None when there is no such
    call. A part removed since (older than KEEP_DAYS) shows as "[effacé]"."""

    async with runs().pool.connection() as db:
        row = await (await db.execute(of(Q.prompt_call, cabinet), (call_id,))).fetchone()
        if not row:
            return None
        agent, at, session, system, tools, hashes = row
        parts = await (await db.execute(of(Q.prompt_parts_of, cabinet), ([system, tools, *hashes],))).fetchall()
        found = dict(parts)
    missing = "[effacé]"  # a part removed since (the keeping period)
    return {
        "agent": agent,
        "at": at,
        "session_id": session,
        "system": found.get(system, missing),
        "tools": found.get(tools, []),
        "messages": [found.get(h, missing) for h in hashes],
    }


async def spent(cabinet, statement, keys):
    """Return {key: (input, cached, output, cost, priced)} of the cabinet's model calls, the runs' (Q.usage_of_runs,
    their nested runs included), the conversations' (Q.usage_of_sessions) or the agent's calls' (Q.usage_of_calls)
    named by ``keys``; a key without any call is left out. ``statement`` is one of those three."""
    if not keys:
        return {}
    async with runs().pool.connection() as db:
        rows = await (await db.execute(of(statement, cabinet), {"cabinet": cabinet, "keys": list(keys)})).fetchall()
    return {key: tuple(row) for key, *row in rows}
