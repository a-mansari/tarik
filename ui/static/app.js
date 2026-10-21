// The live parts of the pages: the runs list refreshes itself, a run going on shows each step as it ends, and a
// step's whole output loads when it is opened. Each follows the server's changes (engine/live.py) instead of asking
// again and again: nothing is fetched until something it shows changed, and no stream is held while its tab is hidden.

// A server stream (EventSource) held only while its tab is shown: a hidden tab gives its connection back. The browser
// opens 6 at most per site over HTTP/1.1, and with one per open page a 7th page's own requests (a run's canvas)
// waited forever. ``on(name, listener)`` listens on the stream now and on each one reopened; ``close()`` ends it.
window.liveStream = (url, { reopened = () => {} } = {}) => {
  const listeners = [];
  let source = null, stopped = false;
  const open = () => {
    source = new EventSource(url);
    for (const [name, listener] of listeners) source.addEventListener(name, listener);
  };
  const shut = () => { source?.close(); source = null; };
  document.addEventListener("visibilitychange", () => {
    if (stopped) return;
    if (document.hidden) shut();
    else if (!source) { open(); reopened(); }
  });
  if (!document.hidden) open();
  return {
    on(name, listener) { listeners.push([name, listener]); source?.addEventListener(name, listener); return this; },
    close() { stopped = true; shut(); },
  };
};

// Follow the changes of ``query`` (cabinet=… or session=…): ``refresh`` runs on each, and once when the tab is shown
// again (its stream was closed meanwhile: what changed is not known), never twice at once. Returns the stream, to
// close when there is nothing left to follow.
window.followChanges = (query, refresh) => {
  let missed = false, busy = false;
  const run = async () => {
    if (busy) { missed = true; return; }
    busy = true;
    try { await refresh(); } catch {} // the next change tries again
    busy = false;
    if (missed) { missed = false; run(); }
  };
  return window.liveStream(`/ui/live?${query}`, { reopened: run }).on("change", run);
};

const rows = document.getElementById("runs-rows");
if (rows && rows.dataset.cabinet) {
  window.followChanges(`cabinet=${encodeURIComponent(rows.dataset.cabinet)}`, async () => {
    const answer = await fetch(rows.dataset.live);
    if (answer.ok) rows.innerHTML = await answer.text();
  });
}

// A list read again from its own page at each change it shows (data-follow: cabinet=… or session=…): the
// discussions, the LLM Calls, with the texts marked data-follow-text beside it (a count). ``data-append`` keeps the
// items there (one opened stays open) and adds the new ones.
for (const node of document.querySelectorAll("[data-follow]")) {
  window.followChanges(node.dataset.follow, async () => {
    const answer = await fetch(location.href);
    if (!answer.ok) return;
    const page = new DOMParser().parseFromString(await answer.text(), "text/html");
    for (const shown of document.querySelectorAll("[data-follow-text]")) { // a count beside the list
      const text = page.getElementById(shown.id)?.textContent;
      if (text !== undefined) shown.textContent = text;
    }
    const fresh = page.getElementById(node.id);
    if (!fresh) return;
    if (!("append" in node.dataset)) { node.innerHTML = fresh.innerHTML; return; }
    for (const item of fresh.children) if (!document.getElementById(item.id)) node.append(item.cloneNode(true));
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
    // One stream, the canvas shares it; reopened when the tab is shown again, it replays the run's steps from the first.
    const events = (window.runEvents ||= window.liveStream(`/runs/${run}/events`));
    events.on("step", (event) => {
      const step = JSON.parse(event.data);
      if (seen.has(String(step.id))) return;
      seen.add(String(step.id));
      steps.querySelector("li.empty")?.remove();
      steps.append(stepItem(step, steps.dataset.graph));
    });
    events.on("end", () => {
      events.close();
      location.reload(); // the run's outcome: what the client reads, the conversation
    });
  }
}

// The dashboard's numbers, read again at each change of its cabinet (the server reads them once per change for all).
const metrics = document.getElementById("metrics");
if (metrics?.dataset.cabinet) {
  window.metricsStream = window.followChanges(`cabinet=${encodeURIComponent(metrics.dataset.cabinet)}`, async () => {
    const answer = await fetch("/ui/accueil?partial=metrics");
    if (answer.ok) document.getElementById("metrics").outerHTML = await answer.text();
  });
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

// On a phone the pages' menu scrolls sideways: it opens on the page shown.
const currentPage = document.querySelector(".bar nav a[aria-current='page']");
if (currentPage) currentPage.parentElement.scrollLeft = currentPage.offsetLeft - currentPage.parentElement.offsetLeft - 16;
