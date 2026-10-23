"""What a step's failure tells the client: its refusal word for word, never a technical text (engine/tools.py)."""

import asyncio

import pytest

from engine import tools
from tarik.core.app import TOOLS, ToolError


@pytest.fixture
def tool(monkeypatch, db_cabinet):
    """A test tool, swapped into the registry: called as the graphs call any tool."""

    def install(function):
        monkeypatch.setitem(TOOLS, "test_tool", function)
        return lambda **arguments: asyncio.run(tools.call(db_cabinet, "test_tool", **arguments))

    return install


@pytest.mark.communication
def test_a_refusal_reaches_the_client_word_for_word(tool):
    def refuse(invoice_id: int):
        raise ToolError(f"Facture {invoice_id} : tiers refuse.")

    with pytest.raises(tools.ToolFailure, match=r"^Facture 3 : tiers refuse\.$"):
        tool(refuse)(invoice_id=3)


@pytest.mark.communication
def test_missing_or_unknown_arguments_are_told_plainly(tool):
    def needs(name: str):
        return {"status": "ok"}

    for arguments in ({}, {"name": "x", "other": 1}):
        with pytest.raises(
            tools.ToolFailure, match=r"^des informations manquent ou sont invalides pour cette etape\.$"
        ):
            tool(needs)(**arguments)


@pytest.mark.communication
def test_a_crash_is_told_plainly_and_its_details_stay_in_the_log(tool, capsys):
    def crash():
        raise KeyError("ODOO_PASSWORD secret-value")

    with pytest.raises(tools.ToolFailure, match=r"^echec technique, reessayez plus tard\.$"):
        tool(crash)()
    assert "secret-value" in capsys.readouterr().out  # logged for the team, never shown


@pytest.mark.communication
def test_a_quiet_step_s_ok_reaches_the_summary_only_when_the_client_must_read_it(monkeypatch, db_cabinet):
    from tarik.invoices.pipeline import NO_FILE, run_summary

    told = {"status": "ok", "message": NO_FILE, "files": [], "tell": True}
    for result in (told, {"status": "ok", "message": "2 fichier(s) a traiter.", "files": [1, 2]}):
        monkeypatch.setitem(TOOLS, "files_to_process", lambda session_id, asked=False, result=result: result)
        asyncio.run(tools.step(db_cabinet, "r", "s", "files_to_process", session_id="s", asked=True))
    assert run_summary("r")["message"] == f"**Fait**\n- {NO_FILE}"
