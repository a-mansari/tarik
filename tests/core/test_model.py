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


# Many threads reading PDFs at once, as the server's tools do: a separate process, so a pdfium segfault fails the
# test instead of killing the run.
READ_AT_ONCE = """
import io, sys
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
from engine.tools.core import model
from engine.tools.invoices import intake, tpe

buffer = io.BytesIO()
pages = [Image.new("RGB", (400, 560), "white") for _ in range(4)]
pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
raw = buffer.getvalue()
reads = [lambda: model.pdf_pages(raw, None), lambda: intake.split_pdf(raw, [2, 3]), lambda: tpe.first_page_text(raw),
         lambda: model.pdf_text(raw, 2), lambda: intake.merged_pdf([raw, raw])]
with ThreadPoolExecutor(16) as pool:
    list(pool.map(lambda i: reads[i % len(reads)](), range(400)))
"""


@pytest.mark.technical
def test_pdfs_read_in_many_threads_at_once_never_crash_the_server():
    import pathlib
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parents[2]
    done = subprocess.run([sys.executable, "-c", READ_AT_ONCE], cwd=root, capture_output=True, timeout=300)
    assert done.returncode == 0, done.stderr.decode()[-2000:]


@pytest.mark.technical
def test_a_pdf_read_that_hangs_or_crashes_is_a_tool_error_and_the_next_read_works(monkeypatch):
    import os
    import time

    monkeypatch.setattr(model, "PDF_TIMEOUT", 1)
    with pytest.raises(ToolError, match="illisible"):
        model.pdfium(time.sleep, 30)  # a PDF pdfium loops on
    with pytest.raises(ToolError, match="illisible"):
        model.pdfium(os._exit, 1)  # a PDF pdfium segfaults on
    page = io.BytesIO()
    Image.new("RGB", (400, 560), "white").save(page, format="PNG")
    assert len(model.pdf_pages(model.to_pdf(page.getvalue()))) == 1  # a fresh pool
