"""A cabinet sees only its own Postgres schema."""

import psycopg
import pytest

from tarik.core.cabinets import POSTGRES, cabinet_db, using
from tarik.invoices import pipeline

from shared import A, B, payload


@pytest.mark.security
def test_each_cabinet_has_its_own_tables(schemas):
    with using(A):
        pipeline.save_files(payload("same-session", ("x1", "a.pdf", b"A")))
    with using(B):
        pipeline.save_files(payload("same-session", ("x1", "b.pdf", b"B")))
    with using(A), cabinet_db() as db:
        assert db.execute("select file_name, data from tarik_files").fetchall() == [("a.pdf", b"A")]
    with using(B), cabinet_db() as db:
        assert db.execute("select file_name, data from tarik_files").fetchall() == [("b.pdf", b"B")]


@pytest.mark.security
def test_same_ids_in_two_cabinets_are_two_files(schemas):
    """Ids restart in each schema: file 1 of A and file 1 of B are each cabinet's own."""
    with using(A):
        a = pipeline.save_files(payload("s", (None, "a.pdf", b"A")))["files"][0]["id"]
    with using(B):
        b = pipeline.save_files(payload("s", (None, "b.pdf", b"B")))["files"][0]["id"]
    assert a == b
    with using(B), cabinet_db() as db:
        assert db.execute("select data from tarik_files where id = %s", (a,)).fetchone() == (b"B",)


@pytest.mark.security
def test_one_cabinets_session_is_invisible_to_another(schemas):
    with using(A):
        pipeline.save_files(payload("session-of-a", (None, "facture.pdf", b"%PDF")))
        assert len(pipeline.files_to_process("session-of-a")["files"]) == 1
    with using(B):
        assert pipeline.files_to_process("session-of-a")["files"] == []


@pytest.mark.security
def test_the_search_path_holds_only_the_cabinet(schemas):
    with psycopg.connect(**POSTGRES) as admin:
        admin.execute("create table if not exists public.tarik_test_public_only (id int)")
    try:
        with using(A), cabinet_db() as db:
            assert db.execute("show search_path").fetchone() == (A,)
            assert db.execute("select current_schemas(false)").fetchone() == ([A],)
            # Nothing of B's, and nothing in public, is reachable by an unqualified name.
            with pytest.raises(psycopg.errors.UndefinedTable):
                db.execute("select * from tarik_test_public_only")
    finally:
        with psycopg.connect(**POSTGRES) as admin:
            admin.execute("drop table if exists public.tarik_test_public_only")


@pytest.mark.technical
def test_a_saved_file_is_saved_once(schemas):
    with using(A):
        assert len(pipeline.save_files(payload("s", ("att-1", "a.pdf", b"A")))["files"]) == 1
        assert pipeline.save_files(payload("s", ("att-1", "a.pdf", b"A")))["files"] == []
    with using(B):  # the same attachment id in B is B's own
        assert len(pipeline.save_files(payload("s", ("att-1", "a.pdf", b"A")))["files"]) == 1


@pytest.mark.security
def test_the_clients_earlier_words_count_and_stay_in_their_cabinet(schemas):
    """Asked in one message, confirmed in a later one: the copy is the client's own request, in A only."""
    from tarik.core.conversation import client_messages, remember_client_message
    from tarik.dossiers.setup import copy_asked
    with using(A):
        remember_client_message("s", "Ouvrez VEGA avec la meme configuration que LYJA.")
        assert copy_asked(client_messages("s", "oui"), "LYJA (Test)")
    with using(B):
        assert not copy_asked(client_messages("s", "oui"), "LYJA (Test)")
