"""Start a graph run on Tarik's server (server/server.py) and print each node as it ends:

  python3 run.py traitement '{"call": {"mode": "resume", "session_id": "x", "cabinet": "experio_cabinet_8"}}'
  python3 run.py agent '{"messages": [{"role": "user", "content": "Experio fonctionne-t-il ?"}]}' --thread t1

Run from the repo root. TARIK_GRAPH_URL is the server (default http://127.0.0.1:2024), TARIK_GRAPH_TOKEN its
bearer token. Each line: seconds since the start, the subgraph path, the node, and its status and summary."""

import json
import os
import sys
import time

import httpx

SERVER = os.getenv("TARIK_GRAPH_URL", "http://127.0.0.1:2024")
POLL_SECONDS = 1
HEADERS = {"authorization": f"Bearer {os.environ['TARIK_GRAPH_TOKEN']}"} if os.getenv("TARIK_GRAPH_TOKEN") else {}


def elapsed(started):
    """Seconds since the monotonic time ``started``, formatted like "  12.34s"."""
    return f"{time.monotonic() - started:7.2f}s"


def start_run(http, graph, payload, thread):
    """Start ``graph`` without waiting and return the new run's id.

    ``payload`` is the run's input as a JSON string; ``thread`` the thread id to run in, or None for a new one."""
    body = {"input": json.loads(payload), "wait": False}
    if thread:
        body["thread_id"] = thread
    response = http.post(f"{SERVER}/runs/{graph}", json=body).raise_for_status()
    return response.json()["run_id"]


def watch(http, run_id, started):
    """Print one line per node as it ends, and any error, until the run ends: polls GET /runs/{run_id}?after=<step id>
    every POLL_SECONDS (the server has no per-run event stream)."""
    after = 0
    while True:
        answer = http.get(f"{SERVER}/runs/{run_id}", params={"after": after}).raise_for_status().json()
        for step in answer["steps"]:
            where = f"{step['path']} > " if step["path"] else ""
            said = " ".join(filter(None, (step["status"], step["summary"])))
            print(f"{elapsed(started)}  {where}{step['node']:<28} {said[:120]}", flush=True)
            after = step["id"]
        if answer["status"] not in ("queued", "running"):
            if answer.get("error"):
                print("ERROR", answer["error"])
            return
        time.sleep(POLL_SECONDS)


def text_of(message):
    """Return a message's text: its content when it is a string, else its text blocks joined."""
    content = message["content"]
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if block.get("type") == "text")


def main(graph, payload, thread=None):
    """Run ``graph`` on ``payload`` (JSON string), watch it, then print the agent's reply or the run's summary."""
    started = time.monotonic()
    with httpx.Client(timeout=None, headers=HEADERS) as http:
        run_id = start_run(http, graph, payload, thread)
        watch(http, run_id, started)
        output = http.get(f"{SERVER}/runs/{run_id}").json()["output"] or {}

    if graph == "agent" and output.get("messages"):
        print(f"{elapsed(started)}  reply: {text_of(output['messages'][-1])[:400]}")
    elif output.get("message"):
        print(f"{elapsed(started)}  summary: {output['message'][:400]}")
    print(f"{elapsed(started)}  done")


if __name__ == "__main__":
    thread = sys.argv[sys.argv.index("--thread") + 1] if "--thread" in sys.argv else None
    main(sys.argv[1], sys.argv[2], thread)
