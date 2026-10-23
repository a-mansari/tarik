"""Where the tests keep their data: database tarik_test, never the cabinets' own (tarik), and in it schemas
named for this run (tarik_test_<run>_<name>), so two runs at once never share one. A run holds a Postgres
lock while it lives; a run that finds another's schemas with nobody holding its lock (it crashed) drops them."""

import re
import uuid

import psycopg

from engine.tools.core import cabinets

DATABASE = "tarik_test"
RUN = uuid.uuid4().hex[:8]  # the process id is 1 in every container: no use here
ALIVE = "select pg_try_advisory_lock(hashtext('tarik test run ' || %s))"


def schema(name):
    """This run's schema ``name``: a lowercase identifier, as a cabinet's name must be."""
    return f"tarik_test_{RUN}_{name}"


def connect():
    return psycopg.connect(**{**cabinets.POSTGRES, "dbname": DATABASE}, autocommit=True)


def start():
    """The test database made if missing, this run's lock taken, and crashed runs' schemas dropped. Returns
    the connection holding the lock: closing it, at the end of the run, lets the next run drop what is left."""
    with psycopg.connect(**cabinets.POSTGRES, autocommit=True) as admin:
        if not admin.execute("select 1 from pg_database where datname = %s", (DATABASE,)).fetchone():
            admin.execute(f"create database {DATABASE}")
    held = connect()
    held.execute(ALIVE, (RUN,))
    for (name,) in held.execute("select nspname from pg_namespace where nspname like 'tarik\\_test\\_%'").fetchall():
        found = re.fullmatch(r"tarik_test_([0-9a-f]{8})_\w+", name)
        if found and found.group(1) != RUN and held.execute(ALIVE, (found.group(1),)).fetchone()[0]:
            held.execute(f"drop schema {name} cascade")  # its run is gone: its lock was free (now ours, freed below)
            held.execute("select pg_advisory_unlock(hashtext('tarik test run ' || %s))", (found.group(1),))
    return held


def empty(name):
    """Every table of schema ``name`` emptied, its sequences restarted."""
    with connect() as db:
        tables = [table for (table,) in db.execute("select tablename from pg_tables where schemaname = %s", (name,))]
        if tables:
            db.execute(f"truncate {', '.join(f'{name}.{table}' for table in tables)} restart identity cascade")


def drop(*names):
    with connect() as db:
        for name in names:
            db.execute(f"drop schema if exists {name} cascade")
