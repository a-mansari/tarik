// The Discussion page: a message and its files sent (files as base64 in JSON), then Tarik's reply waited for (its
// run polled) and the page shown again with it.
(() => {
  const form = document.getElementById("chat-form");
  if (!form) return;
  const waiting = document.getElementById("waiting");
  const failure = document.getElementById("chat-error");
  const button = form.querySelector("button");

  const base64 = (file) => new Promise((done, failed) => {
    const reader = new FileReader();
    reader.onload = () => done(reader.result.split(",")[1] || "");
    reader.onerror = () => failed(reader.error);
    reader.readAsDataURL(file);
  });

  function busy(on, runId) {
    button.disabled = on;
    form.message.disabled = on;
    waiting.hidden = !on;
    if (runId) document.getElementById("waiting-run").href = `/ui/runs/${runId}`;
  }

  async function wait(runId) {
    busy(true, runId);
    for (;;) {
      await new Promise((r) => setTimeout(r, 2000));
      const found = await fetch(`/runs/${runId}`).then((r) => r.json()).catch(() => null);
      if (found && !["queued", "running"].includes(found.status)) return location.reload();
    }
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
      const talk = document.getElementById("talk");
      const shown = document.createElement("li");
      shown.className = "client";
      shown.innerHTML = '<span class="who">Client</span><pre class="said"></pre>';
      shown.querySelector("pre").textContent = form.message.value + (files.length ? `\n📎 ${files.map((f) => f.file_name).join(", ")}` : "");
      talk.append(shown);
      form.reset();
      wait(found.run_id);
    } catch (error) {
      busy(false);
      failure.textContent = error.message;
      failure.hidden = false;
    }
  });

  if (["queued", "running"].includes(waiting.dataset.status)) wait(waiting.dataset.run);
})();
