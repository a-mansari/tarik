// The live parts of the pages: the runs list refreshes itself, a run going on shows each step as it ends, and a
// step's whole output loads when it is opened. Each follows the server's changes (engine/live.py) instead of asking
// again and again: nothing is fetched until something it shows changed, nor while its tab is hidden.

// Follow the changes of ``query`` (cabinet=… or session=…): ``refresh`` runs on each, once for those that came while
// the tab was hidden, and never twice at once. Returns the stream, to close when there is nothing left to follow.
window.followChanges = (query, refresh) => {
  const source = new EventSource(`/ui/live?${query}`);
  let missed = false, busy = false;
  const run = async () => {
    if (document.hidden) { missed = true; return; }
    if (busy) { missed = true; return; }
    busy = true;
    try { await refresh(); } catch {} // the next change tries again
    busy = false;
    if (missed) { missed = false; run(); }
  };
  source.addEventListener("change", run);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && missed) { missed = false; run(); } });
  return source;
};

const rows = document.getElementById("runs-rows");
if (rows && rows.dataset.cabinet) {
  window.followChanges(`cabinet=${encodeURIComponent(rows.dataset.cabinet)}`, async () => {
    const answer = await fetch(rows.dataset.live);
    if (answer.ok) rows.innerHTML = await answer.text();
  });
}

const steps = document.getElementById("steps");
const labels = JSON.parse(document.getElementById("labels")?.textContent || "{}");


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
  summary.append(text("span", "time num", step.when));
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
    const events = (window.runEvents ||= new EventSource(`/runs/${run}/events`)); // one stream, the canvas shares it
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

// Back to the top of the page, offered once the page is scrolled down.
const toTop = document.getElementById("to-top");
if (toTop) {
  const shown = () => { toTop.hidden = window.scrollY < 400; };
  window.addEventListener("scroll", shown, { passive: true });
  shown();
  toTop.addEventListener("click", () => {
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    window.scrollTo({ top: 0, behavior: still ? "auto" : "smooth" });
  });
}
