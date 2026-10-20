"""What the agent is sent at each model call, saved as n8n saves it (n8n/patch.js, tarikSavePrompt): in the cabinet's
own tarik_prompt_calls and tarik_prompt_parts (mcp/tarik/core/cabinet.sql), each distinct piece (the system text,
the tools, one message) kept once by its hash. The UI reads both n8n's calls and these, named by their agent.
Saving never blocks or fails a model call; files are shown by their size."""

import asyncio
import hashlib
import json
import os
import random
import sys

from langchain.agents.middleware import wrap_model_call
from langgraph.config import get_config
from psycopg import sql

from engine.settings import cabinet_of

KEEP_DAYS = int(os.environ.get("TARIK_PROMPTS_KEEP_DAYS") or 30)
AGENT = "Tarik - {} (LangGraph)"  # n8n's calls are "Tarik - <cabinet>"
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
    from engine.runs import pool  # set once the server runs; runs imports the graphs that import this module

    try:
        system_part, tools_part, *message_parts = [part(value) for value in (system, tools, *messages)]
        parts = dict([system_part, tools_part, *message_parts])
        calls, parts_table = sql.Identifier(cabinet, "tarik_prompt_calls"), sql.Identifier(cabinet, "tarik_prompt_parts")
        async with pool.connection() as db:
            await db.execute(
                sql.SQL(
                    "with new_parts as (insert into {} (hash, content) select hash, content::jsonb "
                    "from unnest(%s::text[], %s::text[]) as t(hash, content) on conflict do nothing) "
                    "insert into {} (session_id, agent, system_hash, tools_hash, message_hashes) values (%s, %s, %s, %s, %s)"
                ).format(parts_table, calls),
                (list(parts), list(parts.values()), session_id, agent, system_part[0], tools_part[0], [h for h, _ in message_parts]),
            )
            if random.random() < 0.01:  # now and then, the calls older than KEEP_DAYS and the parts no call uses
                await db.execute(sql.SQL("delete from {} where created_at < now() - %s * interval '1 day'").format(calls), (KEEP_DAYS,))
                # A part younger than a day may belong to a call being saved right now.
                await db.execute(sql.SQL(
                    "delete from {} p where p.created_at < now() - interval '1 day' and not exists (select 1 from {} c "
                    "where p.hash = c.system_hash or p.hash = c.tools_hash or p.hash = any(c.message_hashes))"
                ).format(parts_table, calls))
    except Exception as error:
        print(f"tarik prompts not saved: {error}", file=sys.stderr)


@wrap_model_call
async def logged(request, handler):
    """The agent's middleware: each model call's prompt saved (in the background), then the call itself."""
    try:
        configurable = get_config().get("configurable", {})
        session = configurable.get("session_id") or configurable.get("thread_id")
        if session:
            cabinet = cabinet_of({"configurable": configurable})
            system = request.system_message.content if request.system_message else ""
            tools = [{"name": tool.name, "description": tool.description} for tool in request.tools]
            task = asyncio.create_task(save(cabinet, session, AGENT.format(cabinet), system, tools, [message(m) for m in request.messages]))
            PENDING.add(task)
            task.add_done_callback(PENDING.discard)
    except Exception as error:
        print(f"tarik prompts not saved: {error}", file=sys.stderr)
    return await handler(request)


# The UI's reads. A cabinet is one whose schema holds the prompt tables; never a name taken from the request as is.

async def cabinets():
    from engine.runs import pool

    async with pool.connection() as db:
        rows = await (await db.execute("select table_schema from information_schema.tables where table_name = 'tarik_prompt_calls' order by 1")).fetchall()
    return [row[0] for row in rows]


async def conversations(cabinet, limit=100):
    """The cabinet's conversations with model calls, latest first: {session_id, calls, agents, last}."""
    from engine.runs import pool

    async with pool.connection() as db:
        rows = await (await db.execute(sql.SQL(
            "select session_id, count(*), array_agg(distinct agent), max(created_at) from {} group by session_id order by 4 desc limit %s"
        ).format(sql.Identifier(cabinet, "tarik_prompt_calls")), (limit,))).fetchall()
    return [{"session_id": s, "calls": n, "agents": agents, "last": last} for s, n, agents, last in rows]


async def calls(cabinet, session_id, since=None, until=None):
    """A conversation's model calls, in order (optionally those of one run's time): {id, agent, at, messages}."""
    from engine.runs import pool

    async with pool.connection() as db:
        rows = await (await db.execute(sql.SQL(
            "select id, agent, created_at, cardinality(message_hashes) from {} where session_id = %s "
            "and (%s::timestamptz is null or created_at >= %s) and (%s::timestamptz is null or created_at <= %s) order by id"
        ).format(sql.Identifier(cabinet, "tarik_prompt_calls")), (session_id, since, since, until, until))).fetchall()
    return [{"id": i, "agent": agent, "at": at, "messages": n} for i, agent, at, n in rows]


async def prompt(cabinet, call_id):
    """One call's whole prompt: {agent, at, session_id, system, tools, messages}, or None."""
    from engine.runs import pool

    calls_table, parts = sql.Identifier(cabinet, "tarik_prompt_calls"), sql.Identifier(cabinet, "tarik_prompt_parts")
    async with pool.connection() as db:
        row = await (await db.execute(sql.SQL(
            "select agent, created_at, session_id, system_hash, tools_hash, message_hashes from {} where id = %s"
        ).format(calls_table), (call_id,))).fetchone()
        if not row:
            return None
        agent, at, session, system, tools, hashes = row
        found = dict(await (await db.execute(sql.SQL("select hash, content from {} where hash = any(%s)").format(parts), ([system, tools, *hashes],))).fetchall())
    missing = "[effacé]"  # a part removed since (the keeping period)
    return {"agent": agent, "at": at, "session_id": session, "system": found.get(system, missing), "tools": found.get(tools, []),
            "messages": [found.get(h, missing) for h in hashes]}
