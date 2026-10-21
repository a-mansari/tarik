"""What the agent is sent at each model call (n8n's prompt log, rebuilt): in the cabinet's own tarik_prompt_calls
and tarik_prompt_parts (sql/cabinet_schema.sql), each distinct piece (the system text, the tools, one message) kept
once by its hash, so a growing conversation is not stored again at every call. Saving never blocks or fails a model
call; files are shown by their size."""

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
from tarik.core.queries import Q

KEEP_DAYS = int(os.environ.get("TARIK_PROMPTS_KEEP_DAYS") or 30)
AGENT = "Tarik - {}"


def runs():
    """engine.runs, for its pool: imported at use, since runs imports the agent, which imports this module."""
    from engine import runs as module

    return module


TABLES = {"calls": "tarik_prompt_calls", "parts": "tarik_prompt_parts"}


def of(statement, cabinet):
    """``statement`` (sql/orchestrator.sql) on the cabinet's own prompt tables: its {calls} and {parts} named."""
    named = {key: sql.Identifier(cabinet, table) for key, table in TABLES.items() if f"{{{key}}}" in statement}
    if "{runs}" in statement:  # the run store's own table, beside the cabinet's
        named["runs"] = sql.Identifier(runs().SCHEMA, "runs")
    return sql.SQL(statement).format(**named)


PENDING = set()  # the saves under way (a task nobody holds may be collected before it ends)


def plain(value):
    """``value`` with a file's bytes, or a very long unbroken string (base64), shown by its size."""
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
    content = json.dumps(plain(value), ensure_ascii=False, default=str)
    return hashlib.sha256(content.encode()).hexdigest()[:32], content


def message(entry):
    """One message as the model got it: its role, content (thinking and tool calls included) and tool ids."""
    kept = {"role": entry.type, "content": entry.content}
    for key in ("tool_calls", "tool_call_id", "name"):
        if getattr(entry, key, None):
            kept[key] = getattr(entry, key)
    return kept


async def save(cabinet, session_id, agent, system, tools, messages):

    try:
        system_part, tools_part, *message_parts = [part(value) for value in (system, tools, *messages)]
        parts = dict([system_part, tools_part, *message_parts])
        async with runs().pool.connection() as db:
            await db.execute(
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
            if random.random() < 0.01:  # now and then, the calls older than KEEP_DAYS and the parts no call uses
                await db.execute(of(Q.prompt_calls_expired_removed, cabinet), (KEEP_DAYS,))
                await db.execute(of(Q.prompt_parts_unused_removed, cabinet))
        live.touched(runs().RUNNING.get(), cabinet, session_id)  # the LLM Calls pages show it at once
    except Exception as error:
        print(f"tarik prompts not saved: {error}", file=sys.stderr)


@wrap_model_call
async def logged(request, handler):
    """The agent's middleware: each model call's prompt saved (in the background), then the call itself."""
    try:
        config = get_config()
        if session := session_of(config):
            cabinet = cabinet_of(config)
            system = request.system_message.content if request.system_message else ""
            tools = [{"name": tool.name, "description": tool.description} for tool in request.tools]
            task = asyncio.create_task(
                save(cabinet, session, AGENT.format(cabinet), system, tools, [message(m) for m in request.messages])
            )
            PENDING.add(task)
            task.add_done_callback(PENDING.discard)
    except Exception as error:
        print(f"tarik prompts not saved: {error}", file=sys.stderr)
    return await handler(request)


# The UI's reads, for a cabinet this stack serves (CABINETS): never a name taken from the request as is.


async def listed(cabinet, before=None, limit=100):
    """The cabinet's model calls, latest first, before call id ``before``: {id, agent, at, session_id, messages,
    run_id} (the agent run it was made in, None when gone)."""
    asked = {"cabinet": cabinet, "before": before or 2**31 - 1, "limit": limit}
    async with runs().pool.connection() as db:
        rows = await (await db.execute(of(Q.prompt_calls_listed, cabinet), asked)).fetchall()
    keys = ("id", "agent", "at", "session_id", "messages", "run_id")
    return [{**dict(zip(keys, row, strict=True)), "run_id": row[5] and str(row[5])} for row in rows]


async def calls(cabinet, session_id, since=None, until=None):
    """A conversation's model calls, in order (optionally those of one run's time): {id, agent, at, messages}."""

    async with runs().pool.connection() as db:
        rows = await (
            await db.execute(
                of(Q.prompt_calls_of, cabinet),
                (session_id, since, since, until, until),
            )
        ).fetchall()
    return [{"id": i, "agent": agent, "at": at, "messages": n} for i, agent, at, n in rows]


async def prompt(cabinet, call_id):
    """One call's whole prompt: {agent, at, session_id, system, tools, messages}, or None."""

    async with runs().pool.connection() as db:
        row = await (
            await db.execute(
                of(Q.prompt_call, cabinet),
                (call_id,),
            )
        ).fetchone()
        if not row:
            return None
        agent, at, session, system, tools, hashes = row
        found = dict(
            await (
                await db.execute(
                    of(Q.prompt_parts_of, cabinet),
                    ([system, tools, *hashes],),
                )
            ).fetchall()
        )
    missing = "[effacé]"  # a part removed since (the keeping period)
    return {
        "agent": agent,
        "at": at,
        "session_id": session,
        "system": found.get(system, missing),
        "tools": found.get(tools, []),
        "messages": [found.get(h, missing) for h in hashes],
    }
