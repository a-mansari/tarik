"""What a step's failure tells the client: never the MCP library's validation details."""

import pytest

from tarik.invoices import pipeline
from tests.fakes import FakeDb


@pytest.mark.communication
def test_a_validation_error_is_told_plainly(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(pipeline, "cabinet_db", lambda: db)
    pipeline.record_step(
        "r",
        "s",
        "Creation (create_dossier)",
        {
            "status": "error",
            "message": (
                "Error executing tool create_dossier: 1 validation error for create_dossierArguments\nname\n  "
                "Field required "
                "[type=missing, input_value={'ice': '', 'session_id': 'mail-1'}, input_type=dict]"
            ),
        },
    )
    message = db.queries[0][1][-4]  # the message, then routine, the choices, the journal
    assert message == "des informations manquent ou sont invalides pour cette etape."


@pytest.mark.communication
def test_a_crash_is_told_plainly():
    """The MCP library masks a crash as "Error executing tool <name>", nothing after: the client reads French."""
    assert pipeline.readable("Error executing tool link_tiers") == "echec technique, reessayez plus tard."
    assert (
        pipeline.readable("Error executing tool link_tiers: Facture 3 : tiers refuse.") == "Facture 3 : tiers refuse."
    )
