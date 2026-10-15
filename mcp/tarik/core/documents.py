"""Pages of the documents clients send, as images the model can read."""

import base64
import io

import pypdfium2
from PIL import Image


MAX_PAGES = 3  # as tarik-master: the first pages carry every field checked

def to_pdf(raw):
    """A photo of a facture as a one-page PDF: Experio only takes PDFs."""
    buffer = io.BytesIO()
    Image.open(io.BytesIO(raw)).convert("RGB").save(buffer, format="PDF")
    return buffer.getvalue()

def scan_pages(document_pdf, scale=150 / 72):
    """The first MAX_PAGES pages of a scan stored in Experio (base64), as PNG bytes."""
    return pdf_pages(base64.b64decode(document_pdf), scale)

def pdf_pages(raw, scale=150 / 72):
    """The first MAX_PAGES pages of a PDF, as PNG bytes."""
    pages = []
    for page in list(pypdfium2.PdfDocument(raw))[:MAX_PAGES]:
        buffer = io.BytesIO()
        page.render(scale=scale).to_pil().save(buffer, format="PNG")
        pages.append(buffer.getvalue())
    return pages
