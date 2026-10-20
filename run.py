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


def main(graph, payload, thread=None):
    started = time.monotonic()
    with httpx.Client(timeout=None, headers=HEADERS) as http:
        body = {"input": json.loads(payload), "wait": False, **({"thread_id": thread} if thread else {})}
        run_id = http.post(f"{SERVER}/runs/{graph}", json=body).raise_for_status().json()["run_id"]
        with http.stream("GET", f"{SERVER}/runs/{run_id}/events") as events:
            for line in events.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                if "node" in event:
                    where = f"{event['path']} > " if event["path"] else ""
                    line = " ".join(filter(None, (event["status"], event["summary"])))
                    print(f"{time.monotonic() - started:7.2f}s  {where}{event['node']:<28} {line[:120]}", flush=True)
                elif event.get("error"):
                    print("ERROR", event["error"])
        output = http.get(f"{SERVER}/runs/{run_id}").json()["output"] or {}
    if graph == "agent" and output.get("messages"):
        content = output["messages"][-1]["content"]
        reply = (
            content
            if isinstance(content, str)
            else "".join(b.get("text", "") for b in content if b.get("type") == "text")
        )
        print(f"{time.monotonic() - started:7.2f}s  reply: {reply[:400]}")
    elif output.get("message"):
        print(f"{time.monotonic() - started:7.2f}s  summary: {output['message'][:400]}")
    print(f"{time.monotonic() - started:7.2f}s  done")


if __name__ == "__main__":
    thread = sys.argv[sys.argv.index("--thread") + 1] if "--thread" in sys.argv else None
    main(sys.argv[1], sys.argv[2], thread)
