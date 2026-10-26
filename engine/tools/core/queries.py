"""Every SQL statement Tarik runs, in one place: engine/sql/*.sql, one file per area, each statement under its name
(a line "-- name: <name>", then the statement, its comments with it). Q.<name> is its text, read once, ready for
execute(). A statement for a table whose name the code gives (a cabinet's schema) has {placeholders}, filled with
psycopg.sql.Identifier."""

import re
from pathlib import Path
from types import SimpleNamespace

SQL = Path(__file__).resolve().parents[2] / "sql"
NAMED = re.compile(r"^-- name: (\w+)\n(.*?)(?=^-- name: |\Z)", re.S | re.M)


def load():
    """Return {name: statement} of every engine/sql/*.sql. Raises ValueError for a name used twice."""
    found = {}
    for path in sorted(SQL.glob("*.sql")):
        for name, body in NAMED.findall(path.read_text()):
            if name in found:
                raise ValueError(f"{path.name}: {name} is already named in another file")
            found[name] = body.strip()
    return found


Q = SimpleNamespace(**load())
