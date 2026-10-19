"""What n8n's agents were sent, to read in a browser: cabinets, then their conversations, then each model call's
system text, messages and tools (tarik_prompt_calls and tarik_prompt_parts, saved by n8n/patch.js). Read-only, served
on port 8081, which docker-compose.yml publishes on this machine only: the prompts hold the clients' words.
Run as: python prompts.py"""

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from tarik.core.cabinets import CABINETS, cabinet_db, using

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Tarik - prompts</title>
<style>
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--mute:#6b7385;--line:#e2e5eb;--accent:#4f46e5;
--code:#f0f2f6;--system:#fff4e0;--user:#e8f1ff;--assistant:#e9f7ee;--tool:#f1ecfb}
@media(prefers-color-scheme:dark){:root{--bg:#14161c;--card:#1d2028;--ink:#e6e8ee;--mute:#8e96a8;--line:#2d313c;
--accent:#8b87ff;--code:#14161c;--system:#3a2e17;--user:#17304f;--assistant:#183a27;--tool:#2c2547}}
*{box-sizing:border-box}
body{font:14px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--ink);margin:0}
main{max-width:1000px;margin:0 auto;padding:1.5em 1em 4em}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
nav{color:var(--mute);margin-bottom:.5em}nav a+a:before,nav a+span:before{content:"/";margin:0 .4em}
h1{font-size:1.5em;margin:.2em 0 1em;word-break:break-all}
.list{display:grid;gap:.6em;padding:0;list-style:none}
.list a{display:flex;justify-content:space-between;gap:1em;flex-wrap:wrap;background:var(--card);
border:1px solid var(--line);border-radius:10px;padding:.8em 1em;color:var(--ink)}
.list a:hover{border-color:var(--accent);text-decoration:none}
.list small{color:var(--mute)}
.badge{background:var(--accent);color:#fff;border-radius:99px;padding:0 .6em;font-size:.85em}
details.call{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:.7em 0}
details.call>summary{cursor:pointer;padding:.7em 1em;font-weight:600;display:flex;gap:.8em;flex-wrap:wrap}
details.call>summary small{color:var(--mute);font-weight:400}
.body{padding:0 1em 1em}
details.part{border-top:1px solid var(--line);padding:.4em 0}
details.part>summary{cursor:pointer;color:var(--mute);font-weight:600}
pre{white-space:pre-wrap;word-break:break-word;background:var(--code);padding:.6em .8em;border-radius:8px;
overflow:auto;margin:.5em 0;font:13px/1.45 ui-monospace,monospace}
.msg{border-radius:10px;padding:.5em .8em;margin:.5em 0}
.msg b{display:block;font-size:.75em;text-transform:uppercase;letter-spacing:.05em;color:var(--mute)}
.msg pre{background:none;padding:0;margin:.2em 0 0}
.system{background:var(--system)}.user{background:var(--user)}.assistant{background:var(--assistant)}
.tool{background:var(--tool)}
</style><main>"""


def text(value):
    """``value`` (from the database) as shown: a message's content as written, a list of messages one after the
    other, the rest as indented JSON."""
    if isinstance(value, list):
        return "\n\n".join(text(item) for item in value)
    if isinstance(value, dict) and isinstance(value.get("content"), str):
        return html.escape(f"[{value.get('role', '')}]\n{value['content']}")
    return html.escape(value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False))


def link(href, label, detail=""):
    return f'<li><a href="{html.escape(href)}"><span>{html.escape(label)}</span><small>{detail}</small></a></li>'


def crumbs(*trail):
    """The way back to the cabinets: each (href, label) of ``trail`` is a link but the last, which is plain."""
    links = "".join(f'<a href="{html.escape(href)}">{html.escape(label)}</a>' for href, label in trail[:-1])
    return f'<nav><a href="/">Cabinets</a>{links}<span>{html.escape(trail[-1][1])}</span></nav>'


def message(item):
    """One message of a call as a bubble coloured by its role; what is not a message stays JSON."""
    if isinstance(item, dict) and isinstance(item.get("content"), str):
        role = html.escape(str(item.get("role", "")))
        return f'<div class="msg {role}"><b>{role}</b><pre>{html.escape(item["content"])}</pre></div>'
    return f"<pre>{text(item)}</pre>"


def page(path):
    """The HTML of ``path``: / (cabinets), /<cabinet> (its conversations), /<cabinet>/<session> (its model calls)."""
    parts = [unquote(part) for part in path.strip("/").split("/") if part]
    if not parts:
        names = "".join(link(f"/{name}", name) for name in sorted(CABINETS))
        return f'<nav><span>Cabinets</span></nav><h1>Cabinets</h1><ul class="list">{names}</ul>'
    if parts[0] not in CABINETS or len(parts) > 2:
        return None
    cabinet = html.escape(parts[0])
    with using(parts[0]), cabinet_db() as db:
        if len(parts) == 1:
            rows = db.execute(
                "select session_id, count(*), max(created_at) from tarik_prompt_calls "
                "group by 1 order by 3 desc limit 200"
            ).fetchall()
            sessions = "".join(
                link(f"/{parts[0]}/{session}", session, f'<span class="badge">{count}</span> {last:%Y-%m-%d %H:%M:%S}')
                for session, count, last in rows
            )
            return f'{crumbs(("", parts[0]))}<h1>{cabinet}</h1><ul class="list">{sessions}</ul>'
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
        f'<details class="call"><summary>#{id} <small>{created:%Y-%m-%d %H:%M:%S}</small> {html.escape(agent)} '
        f'<span class="badge">{len(messages)} messages</span></summary><div class="body">'
        f'{"".join(message(item) for item in messages)}'
        f'<details class="part"><summary>System</summary><pre>{text(system)}</pre></details>'
        f'<details class="part"><summary>Tools</summary><pre>{text(tools)}</pre></details></div></details>'
        for id, created, agent, system, tools, messages in rows
    )
    return (
        f'{crumbs((f"/{parts[0]}", parts[0]), ("", parts[1]))}'
        f'<h1>{html.escape(parts[1])}</h1>{calls or "No call."}'
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = page(self.path.split("?")[0])
        self.send_response(200 if body is not None else 404)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((PAGE + (body if body is not None else "Not found")).encode())


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
