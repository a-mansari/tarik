// The live parts of the pages: each follows the server's changes (engine/live.py) instead of asking again and again:
// nothing is fetched until something it shows changed, and the whole browser holds one stream.
import { onUnmounted } from "vue";

// One server stream for the whole browser (engine/live.py: /ui/live). Over HTTP/1.1 a browser opens 6 connections at
// most per site: with one stream per page, a 7th page's own requests (a running run's canvas) waited forever. So the
// tabs share one: whichever holds the lock (Web Locks) opens it for every key any tab follows (each says its own over
// a BroadcastChannel), and passes on each change; when it closes, another takes over. Each key: "cabinet=…",
// "session=…" or "run=…". On each (re)opening, every page refreshes once: a change may have come in between.
export const liveHub = (() => {
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
    if (!query) { previous?.close(); source = null; gap = true; return; } // nothing followed: changes go unseen
    const next = (source = new EventSource(`/ui/live?${query}`));
    let opens = 0;
    next.addEventListener("open", () => {
      opens += 1;
      if (opens === 1) previous?.close();
      const reconnected = opens > 1;
      if (next === source && (missed || reconnected)) {
        fire(own.keys());
        channel.postMessage({ type: "opened" });
      }
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
    return { close: () => unfollow(key, listener, reopen) };
  }

  function unfollow(key, listener, reopen) {
    own.get(key)?.delete(listener);
    if (!own.get(key)?.size) own.delete(key);
    reopen();
  }

  function keyed(event) { // the change's [kind, id] pairs, as the keys followed
    return JSON.parse(event.data || "[]").map(([kind, id]) => `${kind}=${encodeURIComponent(id)}`);
  }
})();

// Follow the changes of ``query`` (cabinet=… or session=…): ``refresh`` runs on each, never twice at once. Returns
// what to close when there is nothing left to follow.
export function followChanges(query, refresh) {
  let missed = false, busy = false;
  const run = async () => {
    if (busy) { missed = true; return; }
    busy = true;
    try { await refresh(); } catch {} // the next change tries again
    busy = false;
    if (missed) { missed = false; run(); }
  };
  return liveHub.on(query, run);
}

// A run going on, followed on the shared stream: at each of its changes its new steps are fetched. ``on("step")``
// gets each step once, in order; ``on("end")`` once it has ended. Shared by the steps list and the canvas.
export function followRun(run, after = 0) {
  const listeners = { step: [], end: [] };
  // From the last step the page already shows (``after``): only newer ones are fetched.
  let last = after, ended = false;
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
  const following = followChanges(`run=${encodeURIComponent(run)}`, read);
  setTimeout(() => read().catch(() => {})); // where it stands now, once the page has said what it listens to
  return {
    on(name, listener) { listeners[name].push(listener); return this; },
    close() { following.close(); },
  };
}

// In a component: ``refresh`` at each change of ``query`` (cabinet=…, session=… or run=…), until it is left.
export function useLive(query, refresh) {
  const following = followChanges(query, refresh);
  onUnmounted(() => following.close());
  return following;
}
