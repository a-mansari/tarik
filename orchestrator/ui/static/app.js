// The live parts of the pages: the runs list refreshes itself, a run going on shows each step as it ends, and a
// step's whole output loads when it is opened.

const rows = document.getElementById("runs-rows");
if (rows) {
  setInterval(async () => {
    try {
      const answer = await fetch(rows.dataset.live);
      if (answer.ok) rows.innerHTML = await answer.text();
    } catch {} // the next refresh tries again
  }, 4000);
}

const steps = document.getElementById("steps");
const labels = JSON.parse(document.getElementById("labels")?.textContent || "{}");

function when(iso) {
  return new Date(iso).toLocaleString("fr-FR", { timeZone: "Africa/Casablanca", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }).replace(",", "");
}

function text(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = value;
  return node;
}

function stepItem(step, graph) {
  const item = document.createElement("li");
  item.className = "new";
  item.dataset.id = step.id;
  item.style.setProperty("--depth", step.path ? step.path.split("|").length : 0);
  const details = document.createElement("details");
  details.dataset.step = step.id;
  const summary = document.createElement("summary");
  summary.append(text("span", "time num", when(step.at)));
  summary.append(text("span", "name", labels.steps?.[graph]?.[step.node] || step.node));
  if (step.status) summary.append(text("span", `chip ${step.status}`, labels.statuses?.[step.status] || step.status));
  summary.append(text("span", "line", step.summary));
  details.append(summary, text("pre", "detail", "…"));
  item.append(details);
  return item;
}

if (steps) {
  const run = steps.dataset.run;
  // A step's whole output, fetched the first time it is opened.
  steps.addEventListener("toggle", async (event) => {
    const details = event.target;
    if (!details.open || details.dataset.loaded) return;
    details.dataset.loaded = "yes";
    const pre = details.querySelector("pre.detail");
    try {
      const answer = await fetch(`/runs/${run}/steps/${details.dataset.step}`);
      pre.textContent = JSON.stringify(await answer.json(), null, 2);
    } catch {
      pre.textContent = "Impossible de charger le détail de cette étape. Rouvrez-la pour réessayer.";
      delete details.dataset.loaded;
    }
  }, true);

  if (steps.dataset.live) {
    const seen = new Set([...steps.querySelectorAll("li[data-id]")].map((li) => li.dataset.id));
    const events = new EventSource(`/runs/${run}/events`);
    events.addEventListener("step", (event) => {
      const step = JSON.parse(event.data);
      if (seen.has(String(step.id))) return;
      seen.add(String(step.id));
      steps.querySelector("li.empty")?.remove();
      steps.append(stepItem(step, steps.dataset.graph));
    });
    events.addEventListener("end", () => {
      events.close();
      location.reload(); // the run's outcome: what the client reads, the conversation
    });
  }
}
