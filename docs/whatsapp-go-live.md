# WhatsApp go-live checklist

Tarik's WhatsApp channel is on `main` (the Sparrow client, the `/sparrow/handler` route, the WhatsApp page) and has never
run against the real Sparrow. This is what to set up and how to test it with one number before anyone else is enrolled.

## How it works, in four lines

- All of Experio shares one WhatsApp number at Sparrow (one *sender*). A collaborator is a Sparrow *recipient*: their
  phone, bound to Tarik's public host.
- An admin enrolls a collaborator on the **WhatsApp** page (sidebar, admins only): their Experio user's `phone` field is
  registered at Sparrow and they receive a welcome message.
- Sparrow forwards each message they write to `https://<Tarik's host>/sparrow/handler`; Tarik answers in a
  conversation of its own (`wa-…`), with the collaborator's own rights, and replies on WhatsApp.
- Only enrolled collaborators are answered. A message from anyone else gets a 401 and no reply.

## 1. Before the first test

- [ ] **Import the content.** Contenu page, import `docs/content-export/tarik-contenu-whatsapp-20261105.json` (the two new
      Journal entries' words). Until then `python check.py` reports `journal action whatsapp_enrolled: no words`.
- [ ] **Vault `secret/tarik/shared`** holds the four keys below, as `DEPLOY.md` describes for `shared.json`
      (`VAULT_SEED_OVERWRITE=1` merges them into an existing path):

  | Key | Value |
  | --- | --- |
  | `PUBLIC_URL` | this server as clients reach it (`https://…`); **its host is the host Sparrow calls Tarik on**. It must reach `/sparrow/handler` from the outside: Sparrow writes to Tarik there, and an enrollment succeeds even when that host doesn't route it, the messages then never arrive. A stack that has only `GLADDEN_CALLBACK_URL` (its public `/gladden` address) works too: its host is used when `PUBLIC_URL` is not set |
  | `SPARROW_URL` | `https://sparrow.experioservices.com` (also Experio's own default) |
  | `SPARROW_SENDER_ID` | `SM3DpyJAXOQNG2kR`, the sender of Experio's WhatsApp number `212781220229` (see « Proven on 2026-11-05 ») |
  | `SPARROW_TOKEN` | Sparrow's master token |

  The local dev Vault holds all four. A deployed stack has its own Vault: the three `SPARROW_*` keys must be put there too
  (`deploy/seed.example/shared.json` lists them).
- [ ] **Build and restart.** `cd server/web && npm run build`, then restart the server. The start creates
      `whatsapp_recipients` and `whatsapp_messages` (orchestrator) and `tarik_whatsapp_senders` in each cabinet's schema.
- [ ] **Sparrow can reach Tarik.** From outside the network:
      `curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<host>/sparrow/handler` must print `401` (the route is
      up and refuses a caller with no recipient). Anything else: the funnel or the route is not reachable.
- [ ] **A test collaborator** in the test cabinet (`experio_cabinet_8`): an Experio user of type collaborator, active,
      with a Moroccan number in the user's **phone** field (`+212 6…` or `06…`; not `mobile`), **a number not yet
      registered at Sparrow**: Sparrow routes a message by phone alone, so a phone already registered (a dossier's
      WhatsApp number, or a test recipient) is refused as taken. Check first:
      `GET /recipients/lookup/<212…>` must answer 404. On 2026-11-05 the only collaborator with a phone,
      `achraf.mansari.1@gmail.com`, had a number already registered at Sparrow (host `experio.sarnec.com`).

## 2. Proven on 2026-11-05, against the live Sparrow

Read-only lookups, then two real sends to a team member's own phone. Nothing was enrolled or changed.

- **The master token works.** `GET /recipients/lookup/<phone>` answered with it; the audit's worry (Experio's own calls
  carry no token) is settled for Tarik.
- **Real sending works.** A free text and the `bienvenu_comptable` template both got a real `wamid.` id (a simulated send
  starts with `sim-`); the template arrived on the phone, from `212781220229`.
- **The sender.** `SM3DpyJAXOQNG2kR` is the sender of `212781220229`. Sparrow has no endpoint that lists senders, and a
  lookup finds recipients only, so it was found as the one sender of Experio's dossiers: 21 recipients, 23 numbers, all
  under it.
- **The 24-hour rule is real.** The free text, sent when the person had not written to the number that day, never
  arrived although Sparrow accepted it (the Cloud API accepts it and fails later; Sparrow ignores delivery statuses).
  The template arrived. So Tarik answers only inside 24 h of the person's last message, and a failed delivery is
  invisible to it.

## 3. What only Sparrow's owners can confirm

Ask them before the first enrollment; the code cannot tell.

- [ ] **The welcome message.** Tarik uses Sparrow's default `bienvenu_comptable` (written for dossier clients, with the
      person's name and the cabinet's name). Acceptable for collaborators, or is a Tarik template wanted?
- [ ] **The five fixes** found in the audit, if not done: a forward that fails is never retried; the webhook is not
      signed (`X-Hub-Signature-256`); the same phone under two senders breaks inbound; voice notes, video and stickers
      are dropped silently; `SPARROW_TOKEN` empty or `admin/admin` in production.

## 4. First test, with your own number

Use the test cabinet. Ask questions that do not write in Experio first.

1. [ ] **WhatsApp page**, test cabinet: your row shows « Prêt à activer » with your number. Click **Activer**.
       Expected: « … peut écrire à Tarik sur WhatsApp », state « Activé », the welcome message on your phone, a
       `Collaborateur activé sur WhatsApp` line in the Journal.
2. [ ] **Reply to the welcome message** (this opens WhatsApp's 24 h window; Tarik can only answer inside it: a free
       text outside it is accepted by Sparrow and never delivered, with no error anywhere).
       Write « Bonjour, quels sont mes dossiers ? ». Expected: an answer on WhatsApp within a minute or two.
3. [ ] **In the Discussions page**: a conversation whose id starts with `wa-` shows your message and Tarik's answer.
4. [ ] **Send a PDF invoice** for a test dossier you are assigned to (this one does write; the test cabinet is
       `DRY_RUN=0`). Expected: Tarik takes it in charge and says so; a dossier you are not assigned to is refused.
5. [ ] **Send a photo, then two at once.** Each is answered in turn (not grouped); a voice note gets no answer (Sparrow
       does not forward it).
6. [ ] **A number that is not enrolled** writes to the same WhatsApp number: no answer from Tarik.
7. [ ] **Click Retirer.** Expected: « n'écrit plus à Tarik », the row back to « Prêt à activer »; a new message from
       your phone is no longer answered.

If a step fails: `docker compose logs server | grep -E 'sparrow|whatsapp'` says which call Sparrow refused (its status is
logged, its text only on the server).

## 5. Rolling out

- [ ] Enroll the real collaborators one by one on the WhatsApp page, in each cabinet. A phone that is already a dossier's
      WhatsApp number in Experio is refused (« déjà enregistré chez Sparrow »): Sparrow routes a message by phone alone, so
      a phone belongs to one host. A foreign number is shown « Numéro non marocain » (Sparrow accepts `212` only).
- [ ] Tell them: write to Experio's WhatsApp number as they would email Tarik; it replies in the same chat. Tarik
      cannot start a conversation: for that, email stays.

## 6. Going back

- **One person:** Retirer on the WhatsApp page (Sparrow first, then the row).
- **The whole channel:** Retirer every enrolled person (each is removed at Sparrow, so it stops forwarding). A row
  deleted at Tarik alone leaves Sparrow forwarding to a route that answers 401. Unsetting `SPARROW_TOKEN` does not stop
  incoming messages (they need only the rows); it stops enrollment and replies. Emails and the Discussions page are
  untouched.
- **An orphan at Sparrow** (a server stopped between Sparrow's registration and the row): the phone is then refused as
  taken, and the recipient is removed by hand with the master token (`DELETE /recipients/{recipient_id}`).

## Known limits

- Tarik answers inside WhatsApp's 24 h window only; a routine's report stays on email.
- A person's messages are answered one after the other, a photo sent right after another is a second answer.
- Moroccan numbers only; the phone is the user's `phone` field as it stood when the page was shown.
- A message taken and not yet answered when the server restarts is lost: the person sends it again.
- Not done yet: a WhatsApp label in the Discussions list, a cap on queued answers per person, pruning of
  `whatsapp_messages`, a check that a stored phone still matches Experio's.
