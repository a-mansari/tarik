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
