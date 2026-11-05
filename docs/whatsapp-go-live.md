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
  | `GLADDEN_CALLBACK_URL` | this server's public `/gladden` address; **its host is the host Sparrow calls Tarik on** |
  | `SPARROW_URL` | Sparrow's address (Experio's own default is `https://sparrow.experioservices.com`) |
  | `SPARROW_SENDER_ID` | Experio's one sender id at Sparrow: Experio's WhatsApp settings page shows it (`whatsapp.whatsapp_sender_id`) |
  | `SPARROW_TOKEN` | Sparrow's master token |

  The local dev Vault has `GLADDEN_CALLBACK_URL` and `SPARROW_TOKEN`; it lacks `SPARROW_URL` and `SPARROW_SENDER_ID`. A
  deployed stack has its own Vault: the token must be put there too.
- [ ] **Build and restart.** `cd server/web && npm run build`, then restart the server. The start creates
      `whatsapp_recipients` and `whatsapp_messages` (orchestrator) and `tarik_whatsapp_senders` in each cabinet's schema.
- [ ] **Sparrow can reach Tarik.** From outside the network:
      `curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<host>/sparrow/handler` must print `401` (the route is
      up and refuses a caller with no recipient). Anything else: the funnel or the route is not reachable.
- [ ] **A test collaborator** in the test cabinet (`experio_cabinet_8`): an Experio user of type collaborator, active,
      with your own Moroccan number in the user's **phone** field (`+212 6…` or `06…`; not `mobile`).

## 2. What only Sparrow's owners can confirm

Ask them before the first enrollment; the code cannot tell.

- [ ] **Authentication.** Sparrow's `/recipients` and `/messages` require a bearer token, yet Experio's own calls to them
      carry none. Does the master token work, or does Sparrow run with auth off? If it answers 401 or 403, enrollment
      fails with « Sparrow n'a pas répondu comme attendu » and the server log says `sparrow: GET recipients answered 401`.
- [ ] **Real sending.** Is Sparrow in production sending for real (`WHATSAPP_REAL_SEND`)? If not, the welcome and the
      replies are simulated and nothing reaches the phone.
- [ ] **The welcome message.** Tarik uses Sparrow's default `bienvenu_comptable` (written for dossier clients, with the
      person's name and the cabinet's name). Acceptable for collaborators, or is a Tarik template wanted?
- [ ] **The five fixes** found in the audit, if not done: a forward that fails is never retried; the webhook is not
      signed (`X-Hub-Signature-256`); the same phone under two senders breaks inbound; voice notes, video and stickers
      are dropped silently; `SPARROW_TOKEN` empty or `admin/admin` in production.

## 3. First test, with your own number

Use the test cabinet. Ask questions that do not write in Experio first.

1. [ ] **WhatsApp page**, test cabinet: your row shows « Prêt à activer » with your number. Click **Activer**.
       Expected: « … peut écrire à Tarik sur WhatsApp », state « Activé », the welcome message on your phone, a
       `Collaborateur activé sur WhatsApp` line in the Journal.
2. [ ] **Reply to the welcome message** (this opens WhatsApp's 24 h window; Tarik can only answer inside it).
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

## 4. Rolling out

- [ ] Enroll the real collaborators one by one on the WhatsApp page, in each cabinet. A phone that is already a dossier's
      WhatsApp number in Experio is refused (« déjà enregistré chez Sparrow »): Sparrow routes a message by phone alone, so
      a phone belongs to one host. A foreign number is shown « Numéro non marocain » (Sparrow accepts `212` only).
- [ ] Tell them: write to Experio's WhatsApp number as they would email Tarik; it replies in the same chat. Tarik
      cannot start a conversation: for that, email stays.

## 5. Going back

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
