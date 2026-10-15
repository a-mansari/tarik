"""What a step's failure tells the client: never the MCP library's validation details."""

from tarik.invoices import pipeline


class FakeDb:
    def __init__(self):
        self.saved = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params):
        self.saved.append(params)
        return self


def test_a_validation_error_is_told_plainly(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(pipeline, "cabinet_db", lambda: db)
    pipeline.record_step("r", "s", "Creation (create_dossier)", {"status": "error", "message": (
        "Error executing tool create_dossier: 1 validation error for create_dossierArguments\nname\n  Field required "
        "[type=missing, input_value={'ice': '', 'session_id': 'mail-1'}, input_type=dict]")})
    message = db.saved[0][-1]
    assert message == "des informations manquent ou sont invalides pour cette etape."
