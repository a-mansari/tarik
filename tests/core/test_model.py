"""The vision model's side: a phone photo it can read, and failures that say who failed."""

import io

import httpx
import pytest
from PIL import Image

from engine.tools.core import model, usage
from engine.tools.core.app import ToolError
from engine.tools.core.config import MAX_SIDE


@pytest.mark.functionality
def test_a_phone_photo_reaches_the_model_at_a_readable_size():
    photo = io.BytesIO()
    Image.new("RGB", (5712, 4284), "white").save(photo, format="JPEG")  # an iPhone's photo
    (page,) = model.pdf_pages(model.to_pdf(photo.getvalue()))
    assert max(Image.open(io.BytesIO(page)).size) <= MAX_SIDE  # 11900 px before: the model's service refused it


@pytest.mark.communication
@pytest.mark.parametrize(
    "status, says",
    [(400, "a refusé de lire ce fichier"), (413, "a refusé de lire ce fichier"), (529, "indisponible")],
)
def test_a_failed_reading_says_it_is_tarik_s_reader_not_experio(monkeypatch, status, says):
    def post(url, **kw):
        return httpx.Response(status, request=httpx.Request("POST", url))

    monkeypatch.setattr(model, "setting", lambda name, default="": "key" if name == "MODEL_API_KEY" else default)
    monkeypatch.setattr(model, "model_name", lambda: "m")
    monkeypatch.setattr(model, "adapter", lambda: "minimax")
    monkeypatch.setattr(model.HTTP, "post", post)
    with pytest.raises(ToolError) as raised:
        model.ask_model("s", [], "i", "Identification du document")
    assert says in str(raised.value) and "pas Experio" in str(raised.value)


@pytest.mark.functionality
def test_a_minimax_call_is_priced_at_its_published_rates():
    assert usage.priced("MiniMax-M3", 100_000, 100_000, 100_000) == pytest.approx(0.030 + 0.006 + 0.120)
    assert usage.priced("MiniMax-M3", 600_000, 0, 0) == pytest.approx(2 * 0.18)  # a long prompt: every rate doubled
    assert usage.priced("unknown", 10, 0, 10) is None
