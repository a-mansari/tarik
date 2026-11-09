"""The conversation's outbox: the files Tarik makes, and the ones Experio stored on a record."""

import base64

import pytest

from engine.tools.core import outbox
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.queries import Q


def kept(session_id):
    with cabinet_db() as db:
        return [tuple(row[1:]) for row in db.execute(Q.outbox_unsent, (session_id,))]


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("name", "mime"),
    [("export.XLSX", outbox.MIMES["xlsx"]), ("tva.xml", "application/xml"), ("scan", "application/octet-stream")],
)
def test_a_kept_file_gets_the_type_of_its_extension(db_cabinet, name, mime):
    outbox.keep_file("s1", name, b"data")
    assert kept("s1") == [(name, mime, b"data")]


def fake_record(monkeypatch, record):
    asked = []

    def odoo_call(model, method, ids, fields, context):
        asked.append((model, method, ids, fields, context))
        return [record]

    monkeypatch.setattr(outbox, "odoo_call", odoo_call)
    return asked


@pytest.mark.functionality
def test_the_file_experio_stored_on_a_record_is_kept_under_its_name(db_cabinet, monkeypatch):
    record = {"xml_file": base64.b64encode(b"<tva/>").decode(), "xml_name": "tva-2026-09.xml"}
    asked = fake_record(monkeypatch, record)
    assert outbox.keep_record_file("s1", "exp.tva", 4, "xml_file", "xml_name", {"c": 8}) == "tva-2026-09.xml"
    assert asked == [("exp.tva", "read", [4], ["xml_file", "xml_name"], {"c": 8})]
    assert kept("s1") == [("tva-2026-09.xml", "application/xml", b"<tva/>")]


@pytest.mark.functionality
def test_a_stored_file_without_a_name_is_named_after_its_record(db_cabinet, monkeypatch):
    fake_record(monkeypatch, {"xml_file": base64.b64encode(b"x").decode(), "xml_name": False})
    assert outbox.keep_record_file("s1", "exp.ras", 6, "xml_file", "xml_name", {}) == "exp.ras-6"
    assert kept("s1") == [("exp.ras-6", "application/octet-stream", b"x")]


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("session_id", "data"), [("s1", False), ("", base64.b64encode(b"x").decode())], ids=["no file", "no conversation"]
)
def test_nothing_is_kept_without_a_stored_file_or_a_conversation(db_cabinet, monkeypatch, session_id, data):
    fake_record(monkeypatch, {"xml_file": data, "xml_name": "a.xml"})
    assert outbox.keep_record_file(session_id, "exp.tva", 4, "xml_file", "xml_name", {}) == ""
    assert kept("s1") == [] and kept("") == []


@pytest.mark.functionality
def test_a_waiting_file_goes_by_whatsapp_and_a_refused_one_stays_for_an_email(db_cabinet, monkeypatch):
    sent = []
    refusal = {"a.docx": None, "b.pdf": outbox.sparrow.SparrowError("Sparrow answered 502", 502)}

    def send_file(recipient_id, name, mime, data):
        if refusal[name]:
            raise refusal[name]
        sent.append((recipient_id, name, mime, bytes(data)))

    monkeypatch.setattr(outbox.sparrow, "send_file", send_file)
    for name in refusal:
        outbox.keep_file("s1", name, b"data")
    files = outbox.unsent_files("s1")
    assert [name for _, name, _ in files] == ["a.docx", "b.pdf"]
    assert [outbox.send_unsent_file("s1", "r" * 16, file) for file in files] == [True, False]
    assert sent == [("r" * 16, "a.docx", outbox.MIMES["docx"], b"data")]
    assert [row[0] for row in kept("s1")] == ["b.pdf"]  # a.docx is sent, b.pdf waits for an email
    assert outbox.send_unsent_file("s1", "r" * 16, files[0]) is True  # sent meanwhile: nothing is sent twice
    assert len(sent) == 1


@pytest.mark.functionality
def test_a_file_sparrow_will_never_take_is_given_up_on_not_tried_by_every_reply(db_cabinet, monkeypatch):
    def send_file(*args):
        raise outbox.sparrow.SparrowError("Sparrow answered 422", 422)

    monkeypatch.setattr(outbox.sparrow, "send_file", send_file)
    outbox.keep_file("s1", "empty.pdf", b"")
    [file] = outbox.unsent_files("s1")
    assert outbox.send_unsent_file("s1", "r" * 16, file) is False  # the person is told
    assert outbox.unsent_files("s1") == []  # and no later reply tries it again
