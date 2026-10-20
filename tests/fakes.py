"""Stand-ins the unit tests share."""


class FakeDb:
    """Stands in for cabinet_db(): records each query, answers fetchone() with ``row`` and a listing with nothing."""

    def __init__(self, row=None):
        self.row, self.queries = row, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=()):
        self.queries.append((query, params))
        return self

    def fetchone(self):
        return self.row

    def __iter__(self):
        return iter([])


def flag(field, stored_value, read_value):
    """One difference check_invoice reports between what Experio captured and what the scan shows."""
    return {"field": field, "stored": str(stored_value), "extracted": str(read_value), "problem": "x"}
