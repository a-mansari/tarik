"""prompts.py shows a saved prompt as it was written, and escapes it."""

from prompts import text


def test_a_message_is_shown_as_written():
    shown = text(
        [{"role": "system", "content": "Tu es <Tarik>\nVouvoie."}, {"role": "user", "content": [{"type": "text"}]}]
    )
    assert "[system]\nTu es &lt;Tarik&gt;\nVouvoie." in shown
    assert "&quot;type&quot;: &quot;text&quot;" in shown  # a content that is not text stays JSON


def test_a_message_is_a_bubble_of_its_role_and_escaped():
    from prompts import crumbs, message

    assert message({"role": "user", "content": "<b>"}) == (
        '<div class="msg user"><b>user</b><pre>&lt;b&gt;</pre></div>'
    )
    assert "<pre>" in message({"type": "x"}) and "msg" not in message({"type": "x"})
    assert crumbs(("/a", "a"), ("", "b")).endswith("<span>b</span></nav>")
