"""Every use of pdfium, each a function run in one of model.PDF_POOL's processes: bytes in, bytes or text out. Kept
apart from model.py so a process imports only pdfium and PIL, not the engine."""

import contextlib
import io
import itertools
import re
import signal
import threading

import pypdfium2

# pdfium is not thread-safe, not even for two documents: every use holds PDFIUM and closes its documents inside it,
# as one left to the garbage collector is closed whenever and in whatever thread it runs. Seven statements read at
# once segfaulted the whole server (2026-10-30). Only ever called through model.pdfium, in its pool's processes (one
# call at a time each, the lock keeping it so): called in the server, a hang or a crash takes the server with it.
PDFIUM = threading.RLock()


@contextlib.contextmanager
def pdf_document(raw=None):
    """Yield pypdfium2's document of PDF bytes ``raw`` (None: a new empty one) under PDFIUM, closed with its pages on
    exit. Raises pypdfium2.PdfiumError when ``raw`` is no PDF."""
    with PDFIUM:
        document = pypdfium2.PdfDocument.new() if raw is None else pypdfium2.PdfDocument(raw)
        try:
            yield document
        finally:
            document.close()


def within(seconds, function, *args):
    """Return ``function(*args)``, this process killed after ``seconds`` (SIGALRM, left to its default action): the only
    way out of a pdfium call that hangs or loops on a PDF."""
    signal.alarm(seconds)
    try:
        return function(*args)
    finally:
        signal.alarm(0)


def saved(document):
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def pages(raw, limit, scale, max_side):
    """Return the first ``limit`` pages (None: all) of PDF ``raw`` as PNG bytes, rendered at ``scale`` with their
    longest side at most ``max_side`` pixels."""
    encoded = []
    with pdf_document(raw) as document:
        for index in range(len(document) if limit is None else min(limit, len(document))):
            page = document[index]
            fitting = min(scale, max_side / max(page.get_size()))
            buffer = io.BytesIO()  # encoded at once: a long file's pages never all held as images
            page.render(scale=fitting).to_pil().save(buffer, format="PNG")  # to_pil converts BGR to RGB
            encoded.append(buffer.getvalue())
    return encoded


def text(raw, pages=None):
    """Return the text of PDF ``raw``'s first ``pages`` pages (None: all), blanks squeezed; "" for a scan, an image or
    an unreadable file."""
    try:
        with pdf_document(raw) as document:
            count = len(document) if pages is None else min(pages, len(document))
            found = "\n".join(document[i].get_textpage().get_text_range() for i in range(count))
    except Exception:  # a scan or another format: its text is not read
        return ""
    return re.sub(r"[ \t]+", " ", found)


def first_page_text(raw):
    """Return the text of PDF ``raw``'s first page, as pdfium reads it ("" for no page or an unreadable PDF)."""
    try:
        with pdf_document(raw) as document:
            return document[0].get_textpage().get_text_range() if len(document) else ""
    except pypdfium2.PdfiumError:
        return ""


def count(raw):
    """Return PDF ``raw``'s number of pages. Raises pypdfium2.PdfiumError when it is no PDF."""
    with pdf_document(raw) as document:
        return len(document)


def split(raw, starts):
    """Cut PDF ``raw`` into [(first page, last page, bytes)], 1-based, one per page of ``starts`` (page 1 always,
    numbers out of range dropped)."""
    documents = []
    with pdf_document(raw) as pdf:
        total = len(pdf)
        starts = sorted({1, *(n for n in starts if 1 <= n <= total)})
        for first, after in itertools.pairwise([*starts, total + 1]):
            with pdf_document() as document:
                document.import_pages(pdf, list(range(first - 1, after - 1)))
                documents.append((first, after - 1, saved(document)))
    return documents


def picked(raw, numbers):
    """Return a new PDF (bytes) made of pages ``numbers`` (0-based) of PDF ``raw``."""
    with pdf_document(raw) as source, pdf_document() as document:
        document.import_pages(source, list(numbers))
        return saved(document)


def merged(raws):
    """Return PDFs ``raws`` merged into one (bytes), their pages in order."""
    with pdf_document() as document:
        for raw in raws:
            with pdf_document(raw) as source:
                document.import_pages(source)
        return saved(document)
