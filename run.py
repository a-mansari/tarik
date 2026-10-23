"""Run a graph on server.py and watch it, node by node, as each one ends:

  python3 langgraph/run.py traitement '{"call": {"mode": "resume", "session_id": "x", "cabinet": "experio_cabinet_8"}}'
  python3 langgraph/run.py agent '{"messages": [{"role": "user", "content": "Experio fonctionne-t-il ?"}]}' --thread t1

Each line: seconds since the start, where the node sits (its subgraphs), the node, what it says in short."""

import json
import os
import sys
import time

import httpx

SERVER = os.getenv("TARIK_GRAPH_URL", "http://127.0.0.1:2024")
HEADERS = {"authorization": f"Bearer {os.environ['TARIK_GRAPH_TOKEN']}"} if os.getenv("TARIK_GRAPH_TOKEN") else {}


def elapsed(started):
    return f"{time.monotonic() - started:7.2f}s"


def start_run(http, graph, payload, thread):
    """Start ``graph`` on ``payload`` without waiting; its run id."""
    body = {"input": json.loads(payload), "wait": False}
    if thread:
        body["thread_id"] = thread
    response = http.post(f"{SERVER}/runs/{graph}", json=body).raise_for_status()
    return response.json()["run_id"]


def watch(http, run_id, started):
    """Print each node as it ends, until the run ends."""
    with http.stream("GET", f"{SERVER}/runs/{run_id}/events") as events:
        for line in events.iter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line.removeprefix("data: "))
            if "node" in event:
                where = f"{event['path']} > " if event["path"] else ""
                said = " ".join(filter(None, (event["status"], event["summary"])))
                print(f"{elapsed(started)}  {where}{event['node']:<28} {said[:120]}", flush=True)
            elif event.get("error"):
                print("ERROR", event["error"])


def text_of(message):
    """A message's text: its content, or its text blocks joined."""
    content = message["content"]
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if block.get("type") == "text")


def main(graph, payload, thread=None):
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
