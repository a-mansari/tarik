---
name: pr-review
description: Tarik's review routine for a branch or PR, run before every `gh pr create` and on any "review this PR/branch". Runs the deterministic gates (ruff, extra lint on changed files, dead code, check.py, tests, web build), then five parallel reviewers (correctness and missing behaviour, cabinet isolation and leaks, duplication and simpler code, performance, Experio first: proven from Experio's code, its built-ins used instead of reinvented), verifies each finding, and reports them ranked. Changes nothing unless asked. Args: optional PR number or branch (default: current branch against main).
---

# PR review

Goal: every PR leaves the code base as simple, isolated and correct as it found it. Report only real, verified problems
in what the PR changed or broke. No praise, no style nits ruff already covers.

## 1. Scope

- PR number given: read it (no checkout) with `gh pr diff <n>` and `gh pr view <n> --json title,body,files`.
- Otherwise: `git fetch origin main -q` then `git diff origin/main...HEAD` (plus `git diff` for uncommitted work).
- Note the changed files: `git diff --name-only origin/main...HEAD`. `$PY` below = the changed `.py` files that still
  exist, written out.
- Read the PR's intent (title, body, commit messages). It is the yardstick for "missing functionality".

## 2. Deterministic gates (run in the server container; all in one go)

```bash
docker compose exec -T server sh -c '
python -m ruff check --no-cache . && python -m ruff format --check --no-cache .
python -m ruff check --no-cache --select ARG,C901,S --ignore S101 --output-format concise $PY
python check.py
python -m pytest -q -x <test dirs matching the changed areas, e.g. tests/invoices tests/isolation>
'
vulture $PY --min-confidence 80
```

- Always include `tests/isolation` and `tests/style` in the pytest run; add the test directories of each changed area
  (engine/tools/invoices -> tests/invoices, server/api -> tests/api, ...). Full `python -m pytest` if engine/tools/core,
  engine/agent.py, server/server.py or conftest changed. Never `-m llm` unless the user asks (slow, billed).
- `server/web` changed: `cd server/web && npm run build` must pass.
- The extra ruff rules (unused arguments, complexity > 10, security) and vulture are advisory: report only findings on
  lines the PR added or touched. Unused `request`/`app`/`kwargs` forced by a framework signature are not findings.
- Container down: say so, run what works on the host (vulture, `git diff --check`), and mark the rest NOT RUN.

## 3. Reviewers (launch all five in ONE message, Agent tool, subagent_type general-purpose, in the background)

Give each: the diff command, the changed file list, the PR intent, its lens below, and this contract:
"Read the connected code, not only the diff (callers, callees, tests). Report at most 8 findings as
`file:line | severity (high/medium/low) | problem | concrete failure scenario | fix`. A finding without a concrete
scenario (input or state -> wrong result) is dropped. Report nothing outside your lens. Change no file."

1. **Correctness and missing behaviour.** Logic errors, wrong conditions, unhandled None/empty/error results, broken
   callers of a changed signature, step results without a `status` the graph routes on, a run that cannot resume after
   a crash or timeout. Missing functionality: what the PR intent promises but the code does not do; new tool without a
   catalogue entry, prompt (`tools/<name>.md`), labels.json description or test; new graph input without INPUTS/labels;
   new Experio write without a dry-run path; behaviour change without its test.
2. **Cabinet isolation, security and leaks.** One cabinet's data, settings or schema reachable from another (SQL
   without the cabinet's schema, caches or globals keyed without the cabinet, `cabinets.using` missing). Secrets
   (Vault values, Experio keys, passwords, tokens) logged, returned or shown. Tool messages and ToolError texts that
   reach the client or the model carrying internal data: SQL, table/schema names, Odoo model or field names, record
   ids the client did not give, stack traces, file paths, raw Experio errors, another dossier's data. Admin-only API
   routes without `admin_only`, state-changing routes without `same_origin`, unchecked senders, SQL built by string
   formatting, subprocess with user input, HTTP calls without timeout, TLS checks weakened.
3. **Duplication, dead code and simpler code.** New helpers that repeat one already in the code base (grep for the same
   Experio call, query, date or amount parsing, label building; check engine/tools/core first); copy-pasted blocks
   across tools; parameters never used or always passed the same value; options, wrappers or abstractions with one
   caller; code left dead by the change (old function, constant, label, prompt, test fixture); a long function that a
   standard-library call, an existing helper, an Experio built-in (leave those to reviewer 5) or a dict lookup would
   replace. Give the shorter version.
4. **Performance and scale.** Experio/Odoo calls or SQL inside loops (N+1) where one search_read or one query would do;
   reading whole tables or all dossiers where a filter fits; blocking I/O inside async code; unbounded lists, retries
   or waits; work repeated per invoice that could be done once per run; missing indexes for new queries; large PDFs or
   mail bodies held in memory twice. Think one shared stack serving many cabinets at once.
5. **Experio first.** Experio's own code (`../experio_app`, Odoo 15 plus its addons) is the source of truth, and its
   built-ins beat ours. For every Experio model, field, method, state, account, tax, journal or form the diff names
   or assumes:
   - **Proven:** find it in `../experio_app` (`grep -rn` the model `_name`, field or method) and cite file:line, or
     read it on a live replica (Experio MCP `describe_model` / `search_read`). Not found = finding (high if it writes).
     Field names, selection values, states and defaults are read from the model, never typed from memory.
   - **Not reinvented:** before accepting any Tarik logic that computes, checks or decides something Experio already
     does (tax and amounts, duplicates, account and journal choice, partner or tiers matching and creation, dates and
     periods, sequences, reconciliation, posting checks, declarations, warnings), search Experio for a method,
     onchange, compute, constraint, wizard or form that does it. One exists = finding: call it through `odoo_call` /
     `odoo_json` (as the rest of engine/tools/core/experio.py does) instead of reimplementing it, with file:line of the
     built-in and the shorter call.
   - **Same path as the UI:** writes go through Experio's own forms, onchanges and action methods (create/write with
     the context the UI uses, `action_post`, wizards) so its computes and constraints run. Raw `write` of computed or
     related fields, direct SQL on Experio tables, or values Tarik computes that Experio would recompute = finding.
   - **Rules match:** Tarik's conditions mirror Experio's (same states, same domain, same rounding, same company and
     dossier scoping). A copied rule that drifted from Experio's code = finding, citing both lines.
   - Docstrings and comments that state an Experio rule must match the cited code.

## 4. Verify

For each finding, check it yourself (or one verifier agent per batch when there are many): open the cited lines, follow
the scenario, and keep it only if it holds on this branch. Drop duplicates across reviewers (keep the highest severity).
Mark kept findings CONFIRMED (reproduced or traced end to end) or PLAUSIBLE (traced, not run).
An Experio finding is kept only with the `../experio_app` file:line (or replica read) that proves it; open that line.

## 5. Report

- Gates: one line each, PASS / FAIL (shortest decisive output line) / NOT RUN (why).
- Findings: if the ReportFindings tool is available, use it (ranked, most severe first). Otherwise a numbered list,
  most severe first: `file:line` severity, problem, scenario, fix. Plain words, short.
- Verdict line: "ready", "ready after fixes 1-n", or "not ready" (any high finding or failing gate).
- Then ask whether to fix them. With `--fix`: apply the fixes, re-run the gates of step 2, report again.
- With `--comment` and a PR number: post the report as one `gh pr review <n> --comment --body-file <file>` after
  showing the text to the user and getting a yes. Never approve or request changes on the PR.
