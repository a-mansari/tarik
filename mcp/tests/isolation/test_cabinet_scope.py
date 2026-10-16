"""Every call runs for the one cabinet it names, and fails when it names none: names, scopes, threads."""

import concurrent.futures
import threading
import time

import pytest

from shared import A, B
from tarik.core import cabinets, experio
from tarik.core.cabinets import cabinet_db, cabinet_names, current, setting, using


@pytest.mark.security
@pytest.mark.parametrize(
    "bad",
    [
        "Experio_Cabinet_8",
        "experio cabinet",
        "cabinet-8",
        "8cabinet",
        "public",
        "pg_temp",
        "a;drop schema x",
        "a,b,a",
        "x" * 64,
        "café",
    ],
)
def test_only_exact_lowercase_names_are_cabinets(bad):
    with pytest.raises(SystemExit):
        cabinet_names(bad)


@pytest.mark.technical
def test_cabinet_names_are_read_exactly():
    assert cabinet_names("experio_cabinet_8, experio_cabinet_12") == {"experio_cabinet_8", "experio_cabinet_12"}
    assert cabinet_names("") == frozenset()


@pytest.mark.security
def test_nothing_runs_without_a_cabinet():
    with pytest.raises(RuntimeError):
        current()
    with pytest.raises(RuntimeError):
        setting("ODOO_UID")
    with pytest.raises(RuntimeError):
        cabinet_db()
    with pytest.raises(RuntimeError):
        experio.account()


@pytest.mark.security
def test_an_unknown_cabinet_is_refused(two_cabinets):
    for name in ("experio_cabinet_999", "", None, A.upper(), A + " "):
        with pytest.raises(PermissionError), using(name):
            pass


@pytest.mark.technical
def test_scopes_nest_and_restore(two_cabinets):
    with using(A):
        with using(B):
            assert current() == B
        assert current() == A
    with pytest.raises(RuntimeError):
        current()


@pytest.mark.technical
def test_a_scope_is_restored_after_an_error(two_cabinets):
    with pytest.raises(ValueError), using(A):
        raise ValueError
    with pytest.raises(RuntimeError):
        current()


@pytest.mark.security
def test_threads_keep_their_own_cabinet(two_cabinets):
    """Many calls at once, each in its cabinet, each checking it stays there."""

    def work(name):
        with using(name):
            for _ in range(200):
                if current() != name:
                    return False
                time.sleep(0)
        return True

    with concurrent.futures.ThreadPoolExecutor(32) as pool:
        assert all(pool.map(work, [A, B] * 200))


@pytest.mark.security
def test_a_new_thread_starts_with_no_cabinet(two_cabinets):
    seen = []
    with using(A):
        thread = threading.Thread(target=lambda: seen.append(cabinets._current.get(None)))
        thread.start()
        thread.join()
    assert seen == [None]
