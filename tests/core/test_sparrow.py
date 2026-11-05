"""The Sparrow client: a phone belongs to one host, long texts are cut, the token only travels in the header."""

import httpx
import pytest

from engine.tools.core import cabinets, sparrow

TOKEN = "t0ken-secret"
SENDER = "s" * 16
RECIPIENT = "r" * 16
HOST = "tarik.example"
FREE = (404, {"detail": sparrow.NO_RECIPIENT})


@pytest.fixture
def relay(monkeypatch):
    """Sparrow, faked: ``relay.answers`` maps "METHOD path" to (status, json), or to a list of them, one per call;
    requests are kept in ``relay.sent``."""
    monkeypatch.setattr(cabinets, "CABINETS", frozenset({"cabinet_a"}))
    for key, value in {
        "SPARROW_URL": "https://sparrow.example/",
        "SPARROW_SENDER_ID": SENDER,
        "SPARROW_TOKEN": TOKEN,
    }.items():
        monkeypatch.setenv(key, value)

    class Relay:
        answers = {}
        sent = []

    def request(method, url, headers=None, json=None, **kwargs):
        Relay.sent.append((method, url, headers, json))
        answer = Relay.answers.get(f"{method} {url.removeprefix('https://sparrow.example')}", (200, {}))
        if isinstance(answer, list):
            answer = answer.pop(0)
        status, body = answer
        return httpx.Response(status, json=body, request=httpx.Request(method, url))

    monkeypatch.setattr(sparrow.HTTP, "request", request)
    Relay.sent = []
    with cabinets.using("cabinet_a"):
        yield Relay


def enrolled():
    return sparrow.enroll("+212 600-600600", HOST, "Amal", "Cabinet Alami")


@pytest.mark.functionality
def test_a_free_phone_is_enrolled_with_digits_only_and_gets_its_recipient_id(relay):
    relay.answers = {
        "GET /recipients/lookup/212600600600": FREE,
        "POST /recipients/activate": (200, {"recipient_id": RECIPIENT}),
    }
    assert enrolled() == RECIPIENT
    assert relay.sent[-1][3] == {
        "phone_number": "212600600600",
        "host_uri": HOST,
        "sender_id": SENDER,
        "user_name": "Amal",
        "cabinet_name": "Cabinet Alami",
    }


@pytest.mark.functionality
def test_a_phone_already_registered_is_never_enrolled_again(relay):
    relay.answers = {"GET /recipients/lookup/212600600600": (200, [{"recipient_id": "x" * 16}])}
    with pytest.raises(sparrow.PhoneTaken):
        enrolled()
    assert all(method == "GET" for method, *_ in relay.sent)  # nothing was activated
    relay.answers = {
        "GET /recipients/lookup/212600600600": FREE,
        "POST /recipients/activate": (409, {"detail": "Recipient already exists"}),
    }
    with pytest.raises(sparrow.PhoneTaken):  # registered between the lookup and the activation
        enrolled()


@pytest.mark.functionality
def test_a_lookup_that_found_a_wrong_route_is_not_taken_for_a_free_phone(relay):
    relay.answers = {"GET /recipients/lookup/212600600600": (404, {"detail": "Not Found"})}
    with pytest.raises(sparrow.SparrowError):
        enrolled()
    assert all(method == "GET" for method, *_ in relay.sent)  # the phone was not activated on a wrong address


@pytest.mark.functionality
def test_a_welcome_that_fails_leaves_no_recipient_behind(relay):
    ours = {"recipient_id": RECIPIENT, "host_uri": HOST, "sender_id": SENDER}
    other = {"recipient_id": "o" * 16, "host_uri": "dossier.example", "sender_id": SENDER}
    relay.answers = {
        "GET /recipients/lookup/212600600600": [FREE, (200, [other, ours])],
        "POST /recipients/activate": (502, {"detail": "WhatsApp API error 131026"}),
    }
    with pytest.raises(sparrow.SparrowError) as refused:
        enrolled()
    assert refused.value.status == 502
    deleted = [url for method, url, *_ in relay.sent if method == "DELETE"]
    assert deleted == [f"https://sparrow.example/recipients/{RECIPIENT}"]  # not the dossier's


@pytest.mark.functionality
def test_a_long_text_is_sent_in_messages_of_at_most_4096_characters_cut_at_a_line(relay):
    relay.answers = {"POST /messages/send": (200, {"message_id": "wamid"})}
    text = "\n".join(["x" * 100] * 120)  # 12 119 characters
    assert sparrow.send(RECIPIENT, text) == ["wamid"] * 3
    bodies = [body for *_, body in relay.sent]
    assert all(0 < len(body["content"]) <= 4096 for body in bodies)
    assert all(set(line) == {"x"} for body in bodies for line in body["content"].split("\n"))  # no line cut
    assert sparrow.send(RECIPIENT, "  ") == []  # nothing to say, nothing sent
    assert sparrow.parts("y" * 5000) == ["y" * 4096, "y" * 904]  # no break at all: cut at the limit


@pytest.mark.functionality
def test_a_text_cut_short_says_which_parts_were_delivered(relay):
    relay.answers = {"POST /messages/send": [(200, {"message_id": "one"}), (502, {"detail": "window closed"})]}
    with pytest.raises(sparrow.SparrowError) as refused:
        sparrow.send(RECIPIENT, "\n".join(["x" * 100] * 120))
    assert refused.value.sent == ["one"]


@pytest.mark.functionality
def test_removing_a_recipient_already_gone_is_not_an_error(relay):
    relay.answers = {f"DELETE /recipients/{RECIPIENT}": (404, {"detail": "Recipient not found"})}
    sparrow.remove(RECIPIENT)
    relay.answers = {f"DELETE /recipients/{RECIPIENT}": (500, {"detail": "boom"})}
    with pytest.raises(sparrow.SparrowError):
        sparrow.remove(RECIPIENT)


@pytest.mark.security
def test_only_a_recipient_id_of_the_form_sparrow_makes_goes_into_a_url(relay):
    for bad in ("../recipients/lookup/212600600600", "", "r" * 15, "r?x=1&&&&&&&&&"):
        with pytest.raises(sparrow.SparrowError):
            sparrow.remove(bad)
        with pytest.raises(sparrow.SparrowError):
            sparrow.send(bad, "Bonjour")
    assert relay.sent == []


@pytest.mark.security
def test_the_token_travels_in_the_header_only_and_sparrow_s_words_stay_in_the_log(relay):
    relay.answers = {"POST /messages/send": (422, {"detail": f"bad {SENDER} for Amal"})}
    with pytest.raises(sparrow.SparrowError) as refused:
        sparrow.send(RECIPIENT, "Bonjour")
    method, url, headers, body = relay.sent[-1]
    assert headers == {"Authorization": f"Bearer {TOKEN}"}
    assert TOKEN not in url and TOKEN not in str(body)
    assert str(refused.value) == "Sparrow answered 422"  # what Sparrow echoed (the sender, a name) never travels on
    assert SENDER in refused.value.detail


@pytest.mark.functionality
def test_a_sparrow_left_unset_is_an_error_to_catch_not_a_missing_key(relay, monkeypatch):
    monkeypatch.delenv("SPARROW_SENDER_ID")
    relay.answers = {"GET /recipients/lookup/212600600600": FREE}
    with pytest.raises(sparrow.SparrowError, match="not set up"):
        enrolled()
    monkeypatch.delenv("SPARROW_URL")
    with pytest.raises(sparrow.SparrowError, match="not set up"):
        sparrow.send(RECIPIENT, "Bonjour")


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("typed", "found"),
    [
        ("+212 600-600600", "212600600600"),
        ("00212600600600", "212600600600"),
        ("0600600600", "212600600600"),
        ("+212 (0)6 00 60 06 00", "212600600600"),
        ("+2120600600600", "212600600600"),
        ("212600600600", "212600600600"),
        ("+33 6 12 34 56 78", None),
        ("060060060", None),
        ("", None),
    ],
)
def test_a_phone_is_taken_in_its_international_or_local_form_or_not_at_all(typed, found):
    assert sparrow.moroccan(typed) == found


@pytest.mark.functionality
def test_a_foreign_number_is_refused_before_anything_is_asked_of_sparrow(relay):
    with pytest.raises(sparrow.SparrowError, match="Moroccan"):
        sparrow.enroll("+33 6 12 34 56 78", HOST, "Amal", "Cabinet Alami")
    assert relay.sent == []
