// The live parts of the pages: the runs list refreshes itself, a run going on shows each step as it ends, and a
// step's whole output loads when it is opened. Each follows the server's changes (engine/live.py) instead of asking
// again and again: nothing is fetched until something it shows changed, and the whole browser holds one stream.

// One server stream for the whole browser (engine/live.py: /ui/live). Over HTTP/1.1 a browser opens 6 connections at
// most per site: with one stream per page, a 7th page's own requests (a running run's canvas) waited forever. So the
// tabs share one: whichever holds the lock (Web Locks) opens it for every key any tab follows (each says its own over
// a BroadcastChannel), and passes on each change; when it closes, another takes over. Each key: "cabinet=…",
// "session=…" or "run=…". On each (re)opening, every page refreshes once: a change may have come in between.
window.liveHub = (() => {
  const own = new Map(); // key -> this tab's listeners
  const tab = Math.random().toString(36).slice(2);
  const fire = (keys) => { for (const key of keys) for (const listener of own.get(key) || []) listener(); };
  if (!("locks" in navigator) || !("BroadcastChannel" in window)) { // an old browser: a stream of its own per tab
    let source = null;
    const reopen = () => {
      source?.close();
      source = own.size ? new EventSource(`/ui/live?${[...own.keys()].sort().join("&")}`) : null;
      source?.addEventListener("change", (event) => fire(keyed(event)));
    };
    return { on: (key, listener) => follow(key, listener, reopen) };
  }
  const channel = new BroadcastChannel("tarik-live");
  const others = new Map(); // when this tab leads: tab -> the keys it follows
  let leading = false, source = null, opened = "", gap = true; // gap: changes may have been missed (none followed)
  const say = () => channel.postMessage({ type: "keys", tab, keys: [...own.keys()] });
  // A new key set: the new stream opens before the old one closes (both may tell the same change: a refresh more),
  // so nothing is missed and no page refreshes.
  // Only after a gap (this tab just took the lead) or a reconnection (the stream dropped) does every page refresh.
  const reopen = () => {
    if (!leading) { say(); return; }
    const keys = new Set(own.keys());
    for (const followed of others.values()) for (const key of followed) keys.add(key);
    const query = [...keys].sort().join("&");
    if (query === opened) return;
    opened = query;
    const previous = source, missed = gap;
    gap = false;
    if (!query) { previous?.close(); source = null; return; }
    const next = (source = new EventSource(`/ui/live?${query}`));
    let opens = 0;
    next.addEventListener("open", () => {
      opens += 1;
      if (opens === 1) previous?.close();
      if (next === source && (missed || opens > 1)) { fire(own.keys()); channel.postMessage({ type: "opened" }); }
    });
    next.addEventListener("change", (event) => {
      const keys = keyed(event);
      fire(keys);
      channel.postMessage({ type: "change", keys });
    });
  };
  channel.onmessage = ({ data }) => {
    if (data.type === "change") fire(data.keys);
    else if (data.type === "opened") fire(own.keys());
    else if (data.type === "ask") say();
    else if (leading && data.type === "keys") { others.set(data.tab, data.keys); reopen(); }
    else if (leading && data.type === "gone") { others.delete(data.tab); reopen(); }
  };
  addEventListener("pagehide", () => channel.postMessage({ type: "gone", tab }));
  navigator.locks.request("tarik-live", () => new Promise(() => { // held until this tab closes
    leading = true;
    channel.postMessage({ type: "ask" }); // every other tab says what it follows
    reopen();
  }));
  return { on: (key, listener) => follow(key, listener, reopen) };

  function follow(key, listener, reopen) {
    if (!own.has(key)) own.set(key, new Set());
    own.get(key).add(listener);
    reopen();
    return { close() { own.get(key)?.delete(listener); if (!own.get(key)?.size) own.delete(key); reopen(); } };
  }

  function keyed(event) { // the change's [kind, id] pairs, as the keys followed
    return JSON.parse(event.data || "[]").map(([kind, id]) => `${kind}=${encodeURIComponent(id)}`);
  }
})();

// Follow the changes of ``query`` (cabinet=… or session=…): ``refresh`` runs on each, never twice at once. Returns
// what to close when there is nothing left to follow.
window.followChanges = (query, refresh) => {
  let missed = false, busy = false;
  const run = async () => {
    if (busy) { missed = true; return; }
    busy = true;
    try { await refresh(); } catch {} // the next change tries again
    busy = false;
    if (missed) { missed = false; run(); }
  };
  return window.liveHub.on(query, run);
};

// A run going on, followed on the shared stream: at each of its changes its new steps are fetched. ``on("step")``
// gets each step once, in order; ``on("end")`` once it has ended. Shared by the steps list and the canvas.
window.followRun = (run) => {
  const listeners = { step: [], end: [] };
  // From the last step the page already shows (rendered with it): only newer ones are fetched.
  const shown = [...document.querySelectorAll("#steps li[data-id]")].map((item) => Number(item.dataset.id));
  let last = Math.max(0, ...shown), ended = false;
  const read = async () => {
    const found = await (await fetch(`/runs/${run}?after=${last}`)).json();
    for (const step of found.steps || []) {
      last = Math.max(last, step.id);
      for (const listener of listeners.step) listener(step);
    }
    if (!ended && found.status && !["queued", "running"].includes(found.status)) {
      ended = true;
      following.close();
      for (const listener of listeners.end) listener(found);
    }
  };
  const following = window.followChanges(`run=${encodeURIComponent(run)}`, read);
  setTimeout(() => read().catch(() => {})); // where it stands now, once the page has said what it listens to
  return { on(name, listener) { listeners[name].push(listener); return this; } };
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
    const events = (window.runEvents ||= window.followRun(run)); // the canvas shares it
    events.on("step", (step) => {
      if (seen.has(String(step.id))) return;
      seen.add(String(step.id));
      steps.querySelector("li.empty")?.remove();
      steps.append(stepItem(step, steps.dataset.graph));
    });
    events.on("end", () => location.reload()); // the run's outcome: what the client reads, the conversation
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
