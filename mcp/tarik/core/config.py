"""Values shared across Tarik, in one place: the services it talks to, how long a call may take, Experio's
own codes, and the thresholds names are matched with. Per-cabinet settings live in Vault (cabinets.py)."""

import os

# The services, the same for every cabinet (docker-compose.yml sets them).
ODOO_URL = os.environ["ODOO_URL"].rstrip("/")  # Experio
POSTGRES = {
    "host": os.getenv("POSTGRES_HOST", "postgres"),
    "dbname": os.getenv("POSTGRES_DB"),
    "user": os.getenv("POSTGRES_USER"),
    "password": os.getenv("POSTGRES_PASSWORD"),
}

# Seconds before a call is given up: a hung service must not hold a call slot, or the mailbox lock, for good.
RPC_TIMEOUT = 300  # one Experio XML-RPC call
JSON_TIMEOUT = 600  # one Experio JSON-RPC call
MODEL_TIMEOUT = 300  # one vision model answer
N8N_TIMEOUT = 600  # one n8n agent answer
# n8n stops an agent's workflow tool after 300s (TARIK_AGENT_TOOL_TIMEOUT_MS): a run stops waiting on Experio's
# reading in time to finish.
READ_WAIT = 180  # one run's wait for Experio to read its drafts, all of them

# Experio's codes.
READING = ("pending", "sent")  # bluejay_state while Experio still reads a scan
POSTED = ("post", "export")  # state of a posted invoice
# Experio's own definition of a client dossier (tax calendar, notifications, bank...):
# enabled, not company 1 (the cabinet itself), not deleted. Archived ones are inactive, hence hidden.
DOSSIER_DOMAIN = [("enabled", "=", True), ("id", "!=", 1), ("is_deleted", "=", False)]
TEST_MODE = "MODE TEST : aucune modification enregistree dans Experio. "  # run_summary spots test runs by it

# Name matching (text.similarity, 0..1).
MIN_SIMILARITY = 0.7  # dossiers: right matches score 0.9+, unrelated ones stay under 0.6
# Tiers names share generic words ("distribution", "SARL"). On 449 real tiers, right matches
# scored 0.89+ and wrong ones up to 0.71 (e.g. two "... DISTRIBUTION" companies).
TIERS_MIN_SIMILARITY = 0.8
SIMILAR_NAME = 0.9  # a new dossier's name this close to another one's is asked about first

# Documents.
MAX_PAGES = 3  # as tarik-master: the first pages carry every field checked
SCALE = 150 / 72  # pages rendered at 150 dpi for the model
ENTRY_FILES = (".csv", ".txt", ".xlsx", ".xlsm")  # FEC and grand livre files, as import_entries reads them
