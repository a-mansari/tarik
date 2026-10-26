"""Unpack the zip files clients send (by email, from the Discussions page): before anything is saved, each zip is
replaced by the files in it that Tarik takes, so every later tool sees ordinary files. Each file's name keeps the
archive it came from (« factures.zip › facture 12.pdf »).

A zip comes from outside: nothing in it is trusted. Each file is read with a hard limit, whatever size the archive
announces (a zip bomb stops at the limit); there are at most MAX_FILES of them and MAX_TOTAL bytes in all; a zip in
the zip is opened too, one level only; a path is cut to its file name (« ../../x » is « x »); an encrypted file, a
hidden one (« .DS_Store », « __MACOSX/ ») and a type Tarik does not take are left out; so is a text file that
holds no entries (a note, a README: entries_like), in a zip or sent alone. Every file left out is listed (skipped)
and reported to the client, never silently lost."""

import io
import mimetypes
import zipfile

MAX_FILES = 100  # files kept from one archive, the nested ones included
MAX_FILE = 50 * 1024 * 1024  # bytes read of one file, whatever its header says
MAX_TOTAL = 200 * 1024 * 1024  # bytes kept from one archive in all
SEPARATOR = " › "
MB = 1024 * 1024


def is_zip(name):
    return name.lower().endswith(".zip")


def unpacked(files, accepted):
    """Replace each zip in ``files`` by the files in it that Tarik takes, and drop text files that hold no entries.

    Args:
        files: [(name, mime type, bytes)] as the client sent them.
        accepted: tuple of file name endings Tarik takes from inside a zip (".pdf", ".csv"...).

    Returns (kept, skipped): kept is [(name, mime type, bytes)]; skipped is ["name : why"], each file left out and
    why, in plain French."""
    kept, skipped = [], []
    for name, mime, data in files:
        if is_zip(name):
            budget = {"files": MAX_FILES, "bytes": MAX_TOTAL}
            kept += opened(name, data, accepted, budget, skipped, depth=1)
        elif name.lower().endswith(TEXT_ENTRIES) and not entries_like(data):
            skipped.append(f"{name} : {NO_ENTRIES}")
        else:
            kept.append((name, mime, data))
    return kept, skipped


def opened(name, data, accepted, budget, skipped, depth):
    """Return the files Tarik takes from archive ``name`` ([(label, mime type, bytes)]), opening nested zips.

    Args:
        data: the archive's bytes.
        accepted: tuple of file name endings Tarik takes.
        budget: {"files", "bytes"} still allowed for the whole top-level archive, spent as files are kept.
        skipped: list each file left out is appended to, as "label : why".
        depth: 1 for an archive the client sent, 2 for a zip inside it (a deeper zip is left out)."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        members = archive.infolist()
    except zipfile.BadZipFile, OSError, ValueError:
        skipped.append(f"{name} : archive illisible")
        return []
    found, said = [], len(skipped)
    for member in members:
        inner = member.filename.replace("\\", "/")
        base = inner.rsplit("/", 1)[-1]
        if member.is_dir() or not base or base.startswith(".") or inner.startswith("__MACOSX/"):
            continue  # folders, and the files an OS adds by itself: never the client's
        label = f"{name}{SEPARATOR}{base}"
        if member.flag_bits & 0x1:
            skipped.append(f"{label} : protégé par un mot de passe")
            continue
        if is_zip(base) and depth >= 2:
            skipped.append(
                f"{label} : archive dans une archive dans une archive (une seule archive dans l'archive au plus)"
            )
            continue
        if not is_zip(base) and not base.lower().endswith(accepted):
            skipped.append(f"{label} : type non pris en charge")
            continue
        if budget["files"] <= 0:
            skipped.append(
                f"{name} : plus de {MAX_FILES} fichiers, les suivants ignorés (envoyez-les dans une autre archive)"
            )
            break
        limit = min(MAX_FILE, budget["bytes"])
        content, why = read(archive, member, limit)
        if why == TOO_BIG:
            why = too_big(limit)
        if why:
            skipped.append(f"{label} : {why}")
            continue
        if is_zip(base):
            found += opened(label, content, accepted, budget, skipped, depth + 1)
            continue
        if base.lower().endswith(TEXT_ENTRIES) and not entries_like(content):
            skipped.append(f"{label} : {NO_ENTRIES}")
            continue
        budget["files"] -= 1
        budget["bytes"] -= len(content)
        found.append((label, mimetypes.guess_type(base)[0] or "application/octet-stream", content))
    if not found and len(skipped) == said:
        skipped.append(f"{name} : archive vide")  # nothing kept nor left out: said, or the client hears nothing
    return found


def too_big(limit):
    """Return why a file over ``limit`` bytes is left out (French): over MAX_FILE, or over what MAX_TOTAL had left."""
    if limit == MAX_FILE:
        return f"plus de {MAX_FILE // MB} Mo une fois décompressé ({MAX_FILE // MB} Mo au plus par fichier)"
    return f"l'archive dépasse {MAX_TOTAL // MB} Mo décompressés (envoyez-le dans une autre archive)"


TOO_BIG = "too big"  # read's answer for a file over its limit, said by the caller with the limit it was over
TEXT_ENTRIES = (".txt", ".csv")  # entries files that are plain text: a FEC, a grand livre exported as CSV
SEPARATORS = "\t;|,"
MIN_COLUMNS = 4  # a FEC has 18, a grand livre at least date, account, label, amount
NO_ENTRIES = "texte sans écritures comptables (ni FEC ni grand livre)"


def entries_like(data):
    """True when plain text ``data`` (bytes) reads as a table of entries (a FEC, a grand livre): its first two non-empty
    lines both split into MIN_COLUMNS columns or more by the same separator. A note or a README is not."""
    start = data[:65536]  # the first 64 KB hold the header and rows enough
    text = start.decode("utf-8", "ignore") or start.decode("latin-1")
    header_and_row = [line for line in text.splitlines() if line.strip()][:2]
    if len(header_and_row) < 2:
        return False
    return any(all(line.count(sep) + 1 >= MIN_COLUMNS for line in header_and_row) for sep in SEPARATORS)


def read(archive, member, limit):
    """Read zip ``member`` of ``archive``, at most ``limit`` bytes (+1 to detect a file over the limit).

    Returns (content, ""): the file's bytes when read whole; (None, TOO_BIG) when it is over ``limit``;
    (None, "illisible") when it cannot be read (corrupt, unsupported compression...)."""
    try:
        with archive.open(member) as stream:
            content = stream.read(limit + 1)
    except zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, ValueError:
        return None, "illisible"
    return (None, TOO_BIG) if len(content) > limit else (content, "")
