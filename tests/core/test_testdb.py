"""The tests' own database: a crashed run's schemas are dropped by the next run, a living run's never."""

import pytest

from tests import testdb


@pytest.mark.technical
def test_only_a_crashed_runs_schemas_are_dropped():
    with testdb.connect() as db:
        db.execute("create schema if not exists tarik_test_0badc0de_db")  # a run that crashed: nobody holds its lock
        alive = testdb.connect()
        alive.execute(testdb.ALIVE, ("0a11ce00",))
        db.execute("create schema if not exists tarik_test_0a11ce00_db")  # a run still going
    try:
        testdb.start().close()
        with testdb.connect() as db:
            left = {
                name
                # only the two made here: this run's own id may start with 0 too (one run in 16)
                for (name,) in db.execute(
                    "select nspname from pg_namespace where nspname = any(%s)",
                    (["tarik_test_0badc0de_db", "tarik_test_0a11ce00_db"],),
                )
            }
        assert left == {"tarik_test_0a11ce00_db"}
    finally:
        alive.close()
        testdb.drop("tarik_test_0a11ce00_db")
