# Tools and workflows

Every piece in short: what the agents see, the workflows they start, and the Python tools behind
them. For how they fit together, see [architecture.md](architecture.md).

## What an agent sees

Each "Tarik - <cabinet>" agent has these tools, named exactly as below:

| Tool | What it does | Kind |
|---|---|---|
| `Experio_experio_connection` | Says whether Experio can be reached. | MCP, read |
| `Experio_list_dossiers` | The cabinet's dossiers. | MCP, read |
| `Experio_find_dossier` | Finds a dossier from a name, even misspelled or partial; closest first, with a likeness score. | MCP, read |
| `Experio_list_invoices` | A dossier's invoices, most recent first; filter: en cours, brouillon, comptabilisee, payee. | MCP, read |
| `Experio_list_collaborators` | The cabinet's collaborators: role, manager or not, number of dossiers. | MCP, read |
| `Experio_traiter_fichiers` | Processes the invoices and bank statements attached to the conversation, for one dossier, as purchases, sales or both, as the client said. | Workflow "Tarik - Traitement" |
| `Experio_repondre` | Hands the client's answer (their own words, filled by n8n) to what the conversation waits on, and resumes the processing. Its parameters: `decision`, what the agent read in the answer (oui or non, the tiers, exercice or bank account chosen), and `about`, the invoice or statement answered when several questions wait. | Workflow "Tarik - Traitement" (answer mode) |
| `Experio_parametrer_dossier` | Creates a dossier, or imports a FEC or grand livre into one. | Workflow "Tarik - Dossier" |
| `Experio_modifier_dossier` | Changes an existing dossier's identity fields. | Workflow "Tarik - Dossier" (mode `update`) |
| `load_skill` | Loads one of the skills below (n8n's own). | n8n |

And four skills (`instructions/skills/`), loaded when a request matches:

| Skill | When |
|---|---|
| Traitement des documents | Invoices or statements sent, or to process, enter ("saisir"), record or post. |
| Nouveau dossier | A new dossier, or a FEC or grand livre for one. |
| Modification de dossier | A change to a dossier's name, type, ICE, IF, RC, patente, T.S.C, capital, CNSS or address. |
| Réponse à une question | The client answers a question Tarik asked while processing documents, or asks to resume that processing. |

## Workflows

Each is built by a script (`workflows/build_tarik_*.py`): edit the script, not the JSON. Every step is
an MCP call at the cabinet's address, its outcome recorded; the run's summary is the answer.

| Workflow | Started by | What it does |
|---|---|---|
| **Tarik - Traitement** (`TarikTraitement1`) | `Experio_traiter_fichiers`, `Experio_repondre`, "Tarik - Emails" (follow-up) | Saves the agent's attachments, checks the email sender, then for each file: identifies it, imports a bank statement, or splits a PDF into invoices and runs each through the sequence: draft, Experio's reading, check against the scan, correction, classification, tiers, duplicate, account, TVA, journal, withholding, prepaid charges, fiscal year, posting. A step that needs the client stops with a question. Answer mode: `answer_pending` applies the client's answer, then the sequence resumes at the next step; with nothing waiting, drafts Experio hadn't read yet are resumed. Resume mode (a follow-up): those drafts only. |
| **Tarik - Dossier** (`TarikDossier0001`) | `Experio_parametrer_dossier`, `Experio_modifier_dossier`, "Tarik - Emails" (follow-up) | A new dossier: creates it, copies a model dossier's configuration when the agent gives one. An existing one: checks the email sender. Then imports each FEC or grand livre, finishes the onboarding, and reports the dossier's state. Resume mode (a follow-up): finishes the dossiers left waiting. |
| **Tarik - Emails** (`TarikEmails00001`) | `tarik-mail-watch` (webhook, secret header) | Claims the threads waiting for an answer, acknowledges a new one, gets each one's reply from the cabinet's agent, sends it; a sender refused on the dossier gets nothing, the manager is told. A follow-up runs Traitement and Dossier in resume mode, without the agent, and sends their summaries. The watcher reads and stores the emails, and tells the manager about refused senders. |

## The Python tools (`tarik-mcp`)

Served at `/c/<cabinet>/mcp`, each call for that cabinet only. The agents see the five read tools
above; the workflows call the rest.

### Experio and dossiers (`tarik/core`, `tarik/dossiers`)

| Tool | What it does |
|---|---|
| `experio_connection` | Checks Experio can be reached and Tarik can log in. |
| `list_dossiers` / `find_dossier` | The cabinet's dossiers / the closest ones to a name. |
| `list_collaborators` | The cabinet's collaborators and their dossiers. |
| `check_sender` | An email's sender must be a collaborator, or one of Experio's own staff, with that dossier; the chat has no sender to check. |
| `create_dossier` | Creates a dossier; refuses an existing ICE or IF, asks about a very similar name; ICE/IF as the client wrote them. |
| `update_dossier` | Changes a dossier's identity fields; only values the client wrote; refuses another dossier's ICE, IF or name. |
| `copy_configuration` | Copies a model dossier's configuration (the agent asks for it when the client named that dossier). |
| `import_entries` | Imports a FEC or grand livre through Experio's Importation screen. |
| `finish_setup` / `setup_status` / `waiting_setups` | Finishes a new dossier's onboarding / where its setup stands / the conversation's dossiers left waiting. |

### Invoices and statements (`tarik/invoices`)

| Tool | What it does |
|---|---|
| `save_files` / `files_to_process` | Saves a conversation's files / lists those not processed yet. |
| `identify_document` | Invoice or bank statement? |
| `import_bank_statement` | Imports a statement into the dossier's bank journal. |
| `split_invoices` | Asks purchase or sale when the client didn't write it, then splits a PDF holding several invoices (vision model). |
| `create_draft_invoice` / `wait_until_read` | Creates the draft / waits for Experio's reading (up to 3 minutes). |
| `check_invoice` / `correct_invoice` | Compares what Experio read with the scan / corrects the differences. |
| `classify_invoice` | Purchase, sale, or bank statement; stops an invoice whose scan contradicts the side the client gave. |
| `link_tiers` | The invoice's supplier or customer; when none is detected, a question with the close names. |
| `check_duplicate` | Refuses an invoice already recorded. |
| `assign_account` | The invoice's account; when none fits, a question. |
| `settle_tva` / `check_journal` / `settle_withholding` / `settle_prepaid` / `settle_exercice` | TVA, journal, withholding tax, prepaid charges, fiscal year: each settled, or a question to the client. |
| `post_invoice` | Posts the invoice when nothing blocks it. |
| `list_invoices` | A dossier's invoices, amounts as Experio shows them. |
| `answer_pending` / `waiting_drafts` | The client's answer to what the conversation waits on (an invoice's question, a statement's bank account; the invoice or statement it names when several wait), with the agent's `decision` (oui or non, the tiers, exercice or bank account chosen) and `about` (which question, when several wait), after the email sender check / drafts waiting for Experio. |
| `record_step` / `run_summary` | A run's step outcomes (each marked routine or not) / its summary for the client: per invoice, what is not routine, and where it stands. |

### Emails (`tarik/emails`)

| Tool | What it does |
|---|---|
| `claim_threads` | The threads waiting for an answer, each locked for the run (`tarik-mail-watch` stored their emails). |
| `acknowledge_thread` | Tells the client their new thread was received, once, before the agent works on it. |
| `ask_agent` | Gets a thread's reply from the cabinet's agent, in the thread's own conversation. |
| `send_email` | Sends Tarik's reply (whitelisted addresses only), with a copy in the Sent folder. |
| `finish_thread` / `thread_refusal` / `notify_manager` | Closes a thread / detects a refused sender / tells the cabinet's manager. |

## Scripts

| Script | Use |
|---|---|
| `workflows/build_tarik_traitement.py`, `build_tarik_dossier.py`, `build_tarik_emails.py`, `build_tarik_merge_test.py` | Rebuild a workflow's JSON; then import and publish it (README). |
| `workflows/build_tarik_agents.py` | Create or update every cabinet's agent from the template and the skills; checks the tool names first. |
| `workflows/save_layout.py` | Saves the node positions arranged in n8n's editor to `workflows/layout.json`, which the builders apply. |
| `docs/workflows-overview/build.py` | Rebuilds the workflows overview pages (and their PDFs) from the workflows' JSON, and the developers' guide page from `docs/repository-guide.md`. |
| `vault/setup.sh` | Sets up the local Vault. |
