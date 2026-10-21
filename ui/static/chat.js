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

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    failure.hidden = true;
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
        failure.textContent = `Laissés de côté dans l'archive : ${found.skipped.join(" ; ")}`;
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
