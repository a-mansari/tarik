"""Pages of the documents clients send, as images the model can read."""

import base64
import io

import pypdfium2
from PIL import Image

from tarik.core.config import MAX_PAGES, SCALE


def to_pdf(raw):
    """A photo (image bytes) as one-page PDF bytes: Experio only takes PDFs."""
    buffer = io.BytesIO()
    Image.open(io.BytesIO(raw)).convert("RGB").save(buffer, format="PDF")
    return buffer.getvalue()


def pdf_pages(raw, limit=MAX_PAGES, scale=SCALE):
    """The first ``limit`` pages (None: all) of a PDF (bytes), as PNG bytes rendered at ``scale``."""
    pages = []
    for page in list(pypdfium2.PdfDocument(raw))[:limit]:
        buffer = io.BytesIO()
        page.render(scale=scale).to_pil().save(buffer, format="PNG")
        pages.append(buffer.getvalue())
    return pages


def scan_pages(document_pdf):
    """The first MAX_PAGES pages of a scan stored in Experio (base64 str), as PNG bytes."""
    return pdf_pages(base64.b64decode(document_pdf))
