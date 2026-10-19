"""prompts.py shows a saved prompt as it was written, and escapes it."""

from prompts import text


def test_a_message_is_shown_as_written():
    shown = text(
        [{"role": "system", "content": "Tu es <Tarik>\nVouvoie."}, {"role": "user", "content": [{"type": "text"}]}]
    )
    assert "[system]\nTu es &lt;Tarik&gt;\nVouvoie." in shown
    assert "&quot;type&quot;: &quot;text&quot;" in shown  # a content that is not text stays JSON
