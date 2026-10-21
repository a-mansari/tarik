"""Values shared across Tarik, in one place: the services it talks to, how long a call may take, Experio's
own codes, and the thresholds names are matched with. Per-cabinet settings live in Vault (cabinets.py)."""

import os
from typing import Final

# The services, the same for every cabinet (docker-compose.yml sets them).
ODOO_URL: Final = os.environ["ODOO_URL"].rstrip("/")  # Experio
# A dict, not frozen: the tests point it at their own database (tests/conftest.py).
POSTGRES: Final = {
    "host": os.getenv("POSTGRES_HOST", "db"),  # docker-compose.yml's service
    "dbname": os.getenv("POSTGRES_DB"),
    "user": os.getenv("POSTGRES_USER"),
    "password": os.getenv("POSTGRES_PASSWORD"),
}

# Seconds before a call is given up: a hung service must not hold a call slot, or the mailbox lock, for good.
RPC_TIMEOUT: Final = 300  # one Experio XML-RPC call
JSON_TIMEOUT: Final = 600  # one Experio JSON-RPC call
MODEL_TIMEOUT: Final = 300  # one vision model answer
# Experio reads a scan in well under a minute; drafts still unread after this are taken up by the email follow-up
# (waiting_drafts), so a run is never held by one stuck reading.
READ_WAIT: Final = 600  # one run's wait for Experio to read its drafts, all of them

# Experio's codes.
READING: Final = ("pending", "sent")  # bluejay_state while Experio still reads a scan
POSTED: Final = ("post", "export")  # state of a posted invoice
# Experio's own definition of a client dossier (tax calendar, notifications, bank...):
# enabled, not company 1 (the cabinet itself), not deleted. Archived ones are inactive, hence hidden.
DOSSIER_DOMAIN: Final = (("enabled", "=", True), ("id", "!=", 1), ("is_deleted", "=", False))
TEST_MODE: Final = "MODE TEST : aucune modification enregistree dans Experio. "  # run_summary spots test runs by it

# Name matching (words.similarity, 0..1).
MIN_SIMILARITY: Final = 0.7  # dossiers: right matches score 0.9+, unrelated ones stay under 0.6
# Tiers names share generic words ("distribution", "SARL"). On 449 real tiers, right matches
# scored 0.89+ and wrong ones up to 0.71 (e.g. two "... DISTRIBUTION" companies).
TIERS_MIN_SIMILARITY: Final = 0.8
SIMILAR_NAME: Final = 0.9  # a new dossier's name this close to another one's is asked about first

# Documents.
MAX_PAGES: Final = 3  # the first pages carry every field checked
SCALE: Final = 150 / 72  # pages rendered at 150 dpi for the model
ENTRY_FILES: Final = (".csv", ".txt", ".xlsx", ".xlsm")  # FEC and grand livre files, as import_entries reads them
FILE_TYPES: Final = (".pdf", ".jpg", ".jpeg", ".png", *ENTRY_FILES)  # what Tarik takes from a client (a zip: its files)
