"""Zip files clients send (by email, from the Discussions page): each replaced, before anything is saved, by the files
in it that Tarik takes, so every tool after sees ordinary files. A file keeps where it came from in its name
(« factures.zip › facture 12.pdf »).

A zip comes from outside: nothing in it is trusted. Each file is read with a hard limit, whatever size the archive
announces (a zip bomb stops at the limit); there are at most MAX_FILES of them and MAX_TOTAL bytes in all; a zip in
the zip is opened too, one level only; a path is cut to its file name (« ../../x » is « x »); an encrypted file, a
hidden one (« .DS_Store », « __MACOSX/ ») and a type Tarik does not take are left out. What is left out is said
(skipped), never silently lost."""

import io
import mimetypes
import zipfile

MAX_FILES = 100  # files kept from one archive, the nested ones included
MAX_FILE = 50 * 1024 * 1024  # bytes read of one file, whatever its header says
MAX_TOTAL = 200 * 1024 * 1024  # bytes kept from one archive in all
SEPARATOR = " › "


def is_zip(name):
    return name.lower().endswith(".zip")


def unpacked(files, accepted):
    """``files`` [(name, mime type, bytes)] with each zip replaced by the files in it whose name ends with one of
    ``accepted``: (the files, [what was left out and why, in plain French])."""
    kept, skipped = [], []
    for name, mime, data in files:
        if is_zip(name):
            budget = {"files": MAX_FILES, "bytes": MAX_TOTAL}
            kept += opened(name, data, accepted, budget, skipped, depth=1)
        else:
            kept.append((name, mime, data))
    return kept, skipped


def opened(name, data, accepted, budget, skipped, depth):
    """The files of the archive ``name`` that Tarik takes, ``budget`` (files and bytes left) spent as they are read."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        members = archive.infolist()
    except zipfile.BadZipFile, OSError, ValueError:
        skipped.append(f"{name} : archive illisible")
        return []
    found = []
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
            skipped.append(f"{label} : archive dans une archive dans une archive")
            continue
        if not is_zip(base) and not base.lower().endswith(accepted):
            skipped.append(f"{label} : type non pris en charge")
            continue
        if budget["files"] <= 0:
            skipped.append(f"{name} : plus de {MAX_FILES} fichiers, les suivants ignorés")
            break
        content, why = read(archive, member, min(MAX_FILE, budget["bytes"]))
        if why:
            skipped.append(f"{label} : {why}")
            continue
        if is_zip(base):
            found += opened(label, content, accepted, budget, skipped, depth + 1)
            continue
        budget["files"] -= 1
        budget["bytes"] -= len(content)
        found.append((label, mimetypes.guess_type(base)[0] or "application/octet-stream", content))
    return found


def read(archive, member, limit):
    """(the member's bytes, "") read at most ``limit`` (+1 to know it is over), or (None, why it is left out)."""
    try:
        with archive.open(member) as stream:
            content = stream.read(limit + 1)
    except zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, ValueError:
        return None, "illisible"
    return (None, "trop volumineux une fois décompressé") if len(content) > limit else (content, "")
