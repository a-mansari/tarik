"""prompts.py shows a saved prompt as it was written, and escapes it."""

from prompts import crumbs, message, text


def test_a_string_is_shown_as_written_and_escaped():
    assert text("Tu es <Tarik>\nVouvoie.") == "Tu es &lt;Tarik&gt;\nVouvoie."


def test_json_is_coloured_and_escaped():
    shown = text({"type": "<text>", "n": 2, "ok": True, "none": None})
    assert '<span class="key">"type"</span>:' in shown
    assert '<span class="str">"&lt;text&gt;"</span>' in shown
    assert '<span class="num">2</span>' in shown and '<span class="lit">true</span>' in shown
    assert '<span class="lit">null</span>' in shown
    assert '<span class="key">' in text('{"a": 1}')  # a string holding JSON is coloured too


def test_a_message_is_a_bubble_of_its_role_and_escaped():
    assert message({"role": "user", "content": "<b>"}) == '<div class="msg user"><b>user</b><pre>&lt;b&gt;</pre></div>'
    assert "msg" not in message({"type": "x"})
    assert crumbs(("/a", "a"), ("", "b")).endswith("<span>b</span></nav>")
