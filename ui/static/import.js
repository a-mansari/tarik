// Importing an export: a button (data-import, the address to send it to) opens a file picker; the chosen file is
// read here and sent as JSON (with its name, kept in the history's note). The page reloads to show what changed; a
// refusal is told on the page (#import-status), nothing having been saved.
const status = document.getElementById("import-status");

function tell(text, bad) {
  if (!status) return window.alert(text);
  status.hidden = false;
  status.className = `panel ${bad ? "bad" : "accent"}`;
  status.textContent = text;
  status.scrollIntoView({ block: "center" });
}

document.querySelectorAll("[data-import]").forEach((button) => button.addEventListener("click", (event) => {
  event.preventDefault(); // inside a card's summary: never folds or unfolds it
  const picker = Object.assign(document.createElement("input"), { type: "file", accept: "application/json,.json" });
  picker.addEventListener("change", async () => {
    const file = picker.files[0];
    if (!file) return;
    let data;
    try {
      data = JSON.parse(await file.text());
    } catch {
      return tell("Ce fichier n'est pas un export (JSON illisible).", true);
    }
    const answer = await fetch(button.dataset.import, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ...data, file: file.name }),
    });
    const result = await answer.json().catch(() => ({ error: `erreur ${answer.status}` }));
    if (!answer.ok) return tell(`Import refusé, rien n'a été enregistré : ${result.error}`, true);
    const saved = result.saved === true ? 1 : (result.saved || []).length;
    tell(saved ? `Importé : ${saved} nouvelle(s) version(s). Rechargement…` : "Rien n'a changé : le contenu était déjà celui-là.");
    if (saved) setTimeout(() => location.reload(), 900);
  });
  picker.click();
}));

// An export link inside a card's summary downloads without folding the card.
document.querySelectorAll("summary a.card-action").forEach((link) => link.addEventListener("click", (event) => event.stopPropagation()));
