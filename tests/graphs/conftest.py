"""The workflow graphs run with their tools faked: each step answers what the test scripted (ok by default) and the
steps are recorded in the order they ran (record_step left out, run_summary kept: the run's last node)."""

import asyncio

import pytest

from engine import calls, traitement


class Steps:
    def __init__(self):
        self.script, self.calls = {}, []

    def answer(self, tool, *results):
        """``tool`` answers ``results`` one after another (the last one again after that); a callable is given the
        call's arguments, an exception is raised."""
        self.script[tool] = list(results)

    @property
    def names(self):
        return [tool for tool, _ in self.calls]

    def args(self, tool):
        return [arguments for name, arguments in self.calls if name == tool]

    def run(self, graph, **given):
        """``graph`` run on the call ``given`` (a session and a cabinet added): its output."""
        call = {"session_id": "s1", "cabinet": "c1", **given}
        return asyncio.run(graph.ainvoke({"call": call, "outcomes": []}))

    async def call(self, cabinet, tool, /, **arguments):
        if tool == "record_step":
            return {"status": "ok"}
        self.calls.append((tool, arguments))
        if tool == "run_summary":
            return {"message": "resume"}
        results = self.script.get(tool) or [{"status": "ok"}]
        result = results.pop(0) if len(results) > 1 else results[0]
        if isinstance(result, Exception):
            raise result
        if callable(result):
            result = result(**arguments)
        ids = {key: arguments[key] for key in ("invoice_id", "file_id", "dossier_id") if key in arguments}
        return {**ids, **result}  # a step tool's ids are copied into its result (with_ids)


@pytest.fixture
def steps(monkeypatch):
    faked = Steps()
    monkeypatch.setattr(calls, "call", faked.call)
    monkeypatch.setattr(traitement, "run_tool", faked.call)
    return faked
