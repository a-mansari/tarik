// The Discussion page: a message and its files sent (files as base64 in JSON), then the conversation asked for again
// while Tarik works, as n8n's chat shows it: the message at once, each tool as it runs, then the reply.
(() => {
  const form = document.getElementById("chat-form");
  if (!form) return;
  const failure = document.getElementById("chat-error");
  const button = form.querySelector("button");
  let polling = false;

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
    const answer = await fetch(`${location.pathname}?partial=1`).catch(() => null);
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

  async function follow() {
    if (polling) return;
    polling = true;
    busy(true);
    while (await refresh()) await new Promise((r) => setTimeout(r, 1200));
    polling = false;
    busy(false);
    form.message.focus();
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
      form.reset();
      follow();
    } catch (error) {
      busy(false);
      failure.textContent = error.message;
      failure.hidden = false;
    }
  });

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
