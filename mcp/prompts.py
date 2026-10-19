"""What n8n's agents were sent, to read in a browser: cabinets, then their conversations, then each model call's
system text, messages and tools (tarik_prompt_calls and tarik_prompt_parts, saved by n8n/patch.js). Read-only, served
on port 8081, which docker-compose.yml publishes on this machine only: the prompts hold the clients' words.
Run as: python prompts.py"""

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from tarik.core.cabinets import CABINETS, cabinet_db, using

PAGE = (
    "<!doctype html><meta charset=utf-8><title>Tarik - prompts</title>"
    "<style>body{font:14px sans-serif;max-width:1000px;margin:2em auto}"
    "pre{white-space:pre-wrap;background:#f3f3f3;padding:.6em;overflow:auto}</style>"
)


def text(value):
    """``value`` (from the database) as shown: a message's content as written, a list of messages one after the
    other, the rest as indented JSON."""
    if isinstance(value, list):
        return "\n\n".join(text(item) for item in value)
    if isinstance(value, dict) and isinstance(value.get("content"), str):
        return html.escape(f"[{value.get('role', '')}]\n{value['content']}")
    return html.escape(value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False))


def link(href, label):
    return f'<li><a href="{html.escape(href)}">{html.escape(label)}</a></li>'


def page(path):
    """The HTML of ``path``: / (cabinets), /<cabinet> (its conversations), /<cabinet>/<session> (its model calls)."""
    parts = [unquote(part) for part in path.strip("/").split("/") if part]
    if not parts:
        return "<h1>Cabinets</h1><ul>" + "".join(link(f"/{name}", name) for name in sorted(CABINETS)) + "</ul>"
    if parts[0] not in CABINETS or len(parts) > 2:
        return None
    with using(parts[0]), cabinet_db() as db:
        if len(parts) == 1:
            rows = db.execute(
                "select session_id, count(*), max(created_at) from tarik_prompt_calls "
                "group by 1 order by 3 desc limit 200"
            ).fetchall()
            return f"<h1>{parts[0]}</h1><ul>" + "".join(
                link(f"/{parts[0]}/{session}", f"{session}: {count} calls, last {last:%Y-%m-%d %H:%M:%S}")
                for session, count, last in rows
            )
        rows = db.execute(
            "select c.id, c.created_at, c.agent, system.content, tools.content, ("
            " select coalesce(jsonb_agg(part.content order by hash.position), '[]')"
            " from unnest(c.message_hashes) with ordinality as hash(value, position)"
            " join tarik_prompt_parts part on part.hash = hash.value) "
            "from tarik_prompt_calls c "
            "left join tarik_prompt_parts system on system.hash = c.system_hash "
            "left join tarik_prompt_parts tools on tools.hash = c.tools_hash "
            "where c.session_id = %s order by c.id",
            (parts[1],),
        ).fetchall()
    calls = "".join(
        f"<details><summary>#{id} {created:%Y-%m-%d %H:%M:%S} {html.escape(agent)}: {len(messages)} messages</summary>"
        f"<h3>System</h3><pre>{text(system)}</pre><h3>Messages</h3><pre>{text(messages)}</pre>"
        f"<h3>Tools</h3><pre>{text(tools)}</pre></details>"
        for id, created, agent, system, tools, messages in rows
    )
    return f'<p><a href="/{parts[0]}">{parts[0]}</a></p><h1>{html.escape(parts[1])}</h1>{calls or "No call."}'


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = page(self.path.split("?")[0])
        self.send_response(200 if body is not None else 404)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((PAGE + (body if body is not None else "Not found")).encode())


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
