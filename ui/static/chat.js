// The Discussion page: a message and its files sent (files as base64 in JSON), then the conversation shown again at
// each change of it while Tarik works (app.js: followChanges), as n8n's chat shows it: the message at once, each tool
// as it runs, then the reply. On the dashboard (data-opens), the first message folds the numbers away and the page
// becomes the new conversation's, its address too, without loading another page.
(() => {
  const form = document.getElementById("chat-form");
  if (!form) return;
  const failure = document.getElementById("chat-error");
  const button = form.querySelector("button");
  let following = null; // the conversation's changes, followed while Tarik works
  const session = form.dataset.send.split("/").pop();

  const base64 = (file) => new Promise((done, failed) => {
    const reader = new FileReader();
    reader.onload = () => done(reader.result.split(",")[1] || "");
    reader.onerror = () => failed(reader.error);
    reader.readAsDataURL(file);
  });

  function busy(on) {
    button.disabled = on;
    form.message.disabled = on;
  }

  // The conversation shown again from the server; true while its message is on its way.
  async function refresh() {
    const answer = await fetch(`${form.dataset.send}?partial=1`).catch(() => null);
    if (!answer?.ok) return true; // a hiccup: asked again next time
    const talk = document.getElementById("talk");
    const near = talk.getBoundingClientRect().bottom <= window.innerHeight + 80; // following the end: kept there
    const opened = [...talk.querySelectorAll("details.used[open]")].map((d) => d.dataset.turn); // kept open
    talk.outerHTML = await answer.text();
    const shown = document.getElementById("talk");
    for (const turn of opened) shown.querySelector(`details.used[data-turn="${turn}"]`)?.setAttribute("open", "");
    if (near) shown.scrollIntoView({ block: "end" });
    return Boolean(shown.dataset.going);
  }

  function done() {
    following?.close();
    following = null;
    busy(false);
    form.message.focus();
  }

  // Followed before the first look, so that no change between the two is missed.
  async function follow() {
    if (following) return;
    busy(true);
    following = window.followChanges(`session=${encodeURIComponent(session)}`, async () => { if (!(await refresh())) done(); });
    if (!(await refresh())) done();
  }

  // The caps the server holds (chat_pages.problems), said the same way before anything is read or sent.
  const checks = JSON.parse(form.dataset.checks);
  const megabytes = (size) => `${(size / 1048576).toFixed(1).replace(".", ",")} Mo`;
  function problems(text, files) {
    const found = [];
    if (text.length > checks.max_text) found.push(`Message de ${text.length} caractères : ${checks.max_text} au plus. Raccourcissez-le.`);
    if (files.length > checks.max_files) found.push(`${files.length} fichiers joints : ${checks.max_files} au plus par message. Envoyez les autres dans un second message, ou réunissez-les dans un zip.`);
    for (const file of files) {
      if (!checks.accepted.some((kind) => file.name.toLowerCase().endsWith(kind))) found.push(`${file.name} : type non accepté. Types acceptés : ${checks.types}.`);
      else if (!file.size) found.push(`${file.name} : fichier vide.`);
      else if (file.size > checks.max_file) found.push(`${file.name} : ${megabytes(file.size)}, ${checks.max_file / 1048576} Mo au plus par fichier. Envoyez-le compressé ou en plusieurs parties.`);
    }
    return found;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    failure.hidden = true;
    const exceeded = problems(form.message.value.trim(), [...form.files.files]);
    if (exceeded.length) {
      failure.textContent = `Rien n'a été envoyé :\n${exceeded.map((line) => `- ${line}`).join("\n")}`;
      failure.hidden = false;
      form.querySelector(".limits").open = true;
      return;
    }
    busy(true);
    try {
      const files = await Promise.all([...form.files.files].map(async (file) => ({
        file_name: file.name, mime_type: file.type, data: await base64(file),
      })));
      const answer = await fetch(form.dataset.send, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: form.message.value, files }),
      });
      const found = await answer.json().catch(() => ({ error: "Réponse illisible du serveur." }));
      if (!answer.ok) throw new Error(found.error || "Message refusé.");
      if (found.skipped?.length) { // what a zip left out, told to who sent it
        failure.textContent = `Fichiers laissés de côté :\n${found.skipped.map((line) => `- ${line}`).join("\n")}`;
        failure.hidden = false;
      }
      form.reset();
      if (form.dataset.opens) opened();
      follow();
    } catch (error) {
      busy(false);
      failure.textContent = error.message;
      failure.hidden = false;
    }
  });

  // The dashboard turned into the conversation it just opened: its numbers folded away, its address the
  // conversation's own (a reload or a link shows it as such), the back button back to the dashboard.
  function opened() {
    delete form.dataset.opens;
    window.metricsStream?.close(); // the numbers fold away: no longer followed
    document.getElementById("dashboard")?.classList.add("chatting");
    history.pushState({ conversation: true }, "", form.dataset.send);
    document.title = "Discussions · Tarik";
    for (const link of document.querySelectorAll(".bar nav a")) { // the top bar on the page it now is
      if (link.getAttribute("href") === "/ui/discussion") link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    window.addEventListener("popstate", () => location.reload());
  }

  // The files chosen, by name, under the button that chose them.
  const chosen = document.getElementById("chosen");
  function showChosen() {
    chosen.replaceChildren(...[...form.files.files].map((file) => {
      const item = document.createElement("li");
      item.textContent = file.name;
      return item;
    }));
  }
  form.files.addEventListener("change", showChosen);
  form.addEventListener("reset", () => setTimeout(showChosen));

  // Enter sends, Shift+Enter goes to a new line, as in n8n's chat.
  form.message.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  if (document.getElementById("talk").dataset.going) follow();
})();
