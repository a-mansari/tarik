"""The Sparrow client: a phone belongs to one host, long texts are cut, the token only travels in the header."""

import httpx
import pytest

from engine.tools.core import cabinets, sparrow

TOKEN = "t0ken-secret"


@pytest.fixture
def relay(monkeypatch):
    """Sparrow, faked: ``relay.answers`` maps "METHOD path" to (status, json); requests are kept in ``relay.sent``."""
    monkeypatch.setattr(cabinets, "CABINETS", frozenset({"cabinet_a"}))
    for key, value in {
        "SPARROW_URL": "https://sparrow.example/",
        "SPARROW_SENDER_ID": "s" * 16,
        "SPARROW_TOKEN": TOKEN,
    }.items():
        monkeypatch.setenv(key, value)

    class Relay:
        answers = {}
        sent = []

    def request(method, url, headers=None, json=None, **kwargs):
        Relay.sent.append((method, url, headers, json))
        status, body = Relay.answers.get(f"{method} {url.removeprefix('https://sparrow.example')}", (200, {}))
        return httpx.Response(status, json=body, request=httpx.Request(method, url))

    monkeypatch.setattr(sparrow.httpx, "request", request)
    Relay.sent = []
    with cabinets.using("cabinet_a"):
        yield Relay


@pytest.mark.functionality
def test_a_free_phone_is_enrolled_with_digits_only_and_gets_its_recipient_id(relay):
    relay.answers = {
        "GET /recipients/lookup/212600600600": (404, {"detail": "No recipient"}),
        "POST /recipients/activate": (200, {"recipient_id": "r" * 16}),
    }
    assert sparrow.enroll("+212 600-600600", "tarik.example", "Amal", "Cabinet Alami") == "r" * 16
    method, url, headers, body = relay.sent[-1]
    assert body == {
        "phone_number": "212600600600",
        "host_uri": "tarik.example",
        "sender_id": "s" * 16,
        "user_name": "Amal",
        "cabinet_name": "Cabinet Alami",
    }


@pytest.mark.functionality
def test_a_phone_already_registered_is_never_enrolled_again(relay):
    relay.answers = {"GET /recipients/lookup/212600600600": (200, [{"recipient_id": "x" * 16}])}
    with pytest.raises(sparrow.PhoneTaken):
        sparrow.enroll("212600600600", "tarik.example", "Amal", "Cabinet Alami")
    assert all(method == "GET" for method, *_ in relay.sent)  # nothing was activated
    relay.answers = {
        "GET /recipients/lookup/212600600600": (404, {"detail": ""}),
        "POST /recipients/activate": (409, {"detail": "Recipient already exists"}),
    }
    with pytest.raises(sparrow.PhoneTaken):  # registered between the lookup and the activation
        sparrow.enroll("212600600600", "tarik.example", "Amal", "Cabinet Alami")


@pytest.mark.functionality
def test_a_long_text_is_sent_in_messages_of_at_most_4096_characters_cut_at_a_line(relay):
    relay.answers = {"POST /messages/send": (200, {"message_id": "wamid"})}
    text = "\n".join(["x" * 100] * 120)  # 12 119 characters
    assert sparrow.send("r" * 16, text, reply_to="wamid.in") == ["wamid"] * 3
    bodies = [body for *_, body in relay.sent]
    assert all(0 < len(body["content"]) <= 4096 for body in bodies)
    assert all(set(line) == {"x"} for body in bodies for line in body["content"].split("\n"))  # no line cut
    assert [body["reply_to_message_id"] for body in bodies] == ["wamid.in", None, None]
    assert sparrow.send("r" * 16, "  ") == []  # nothing to say, nothing sent


@pytest.mark.functionality
def test_removing_a_recipient_already_gone_is_not_an_error(relay):
    relay.answers = {"DELETE /recipients/" + "r" * 16: (404, {"detail": "Recipient not found"})}
    sparrow.remove("r" * 16)
    relay.answers = {"DELETE /recipients/" + "r" * 16: (500, {"detail": "boom"})}
    with pytest.raises(sparrow.SparrowError):
        sparrow.remove("r" * 16)


@pytest.mark.security
def test_the_token_travels_in_the_header_only_and_never_in_an_error(relay):
    relay.answers = {"POST /messages/send": (502, {"detail": "WhatsApp refused the send"})}
    with pytest.raises(sparrow.SparrowError) as refused:
        sparrow.send("r" * 16, "Bonjour")
    method, url, headers, body = relay.sent[-1]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in url and TOKEN not in str(body)
    assert TOKEN not in str(refused.value) and refused.value.status == 502
