// The server's data for the pages (app/server.py, /ui/api/...): JSON in, JSON out. A page's own data comes from
// usePage, read again when its address changes; a change goes through send. Without a session, back to the login.
import { reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";

export const API = "/ui/api";

// Who is on the pages, the cabinets of the sidebar, and the words of labels.json: read once, again after a change.
export const session = reactive({ user: null, admin: false, cabinets: [], labels: {} });

export async function loadSession() {
  const answer = await fetch(`${API}/me`);
  if (answer.status === 401) return false;
  Object.assign(session, await answer.json());
  return true;
}

function toLogin() {
  const here = location.pathname + location.search;
  if (location.pathname !== "/login") location.assign(`/login?next=${encodeURIComponent(here)}`);
}

// A JSON answer and its status; a lost session sends the page to the login.
export async function read(path, options = {}) {
  const answer = await fetch(path.startsWith("/") && !path.startsWith(API) ? `${API}${path}` : path, options);
  if (answer.status === 401 && !options.keepOnLogin) toLogin();
  const type = answer.headers.get("content-type") || "";
  const data = type.includes("json") ? await answer.json() : null;
  return { status: answer.status, ok: answer.ok, data };
}

// A change: ``body`` as JSON (or a FormData for files), with this page's Origin, as the server asks of a change.
export function send(path, body, method = "POST", options = {}) {
  const isForm = body instanceof FormData;
  return read(path, {
    method,
    body: isForm ? body : JSON.stringify(body ?? {}),
    headers: isForm ? {} : { "content-type": "application/json" },
    ...options,
  });
}

// A page's data from ``path()`` (a function of the route), read again when it changes; ``reload`` reads it again.
export function usePage(path) {
  const route = useRoute();
  const data = ref(null), status = ref(0), loading = ref(true);
  const reload = async () => {
    const found = await read(path(route));
    status.value = found.status;
    if (found.data !== null) data.value = found.data;
    loading.value = false;
  };
  watch(() => path(route), () => { loading.value = true; reload(); }, { immediate: true });
  return { data, status, loading, reload };
}

// labels.json's words: a graph's name, a status's, a step's.
export const graphName = (graph) => session.labels.graphs?.[graph] || graph;
export const graphDescription = (graph) => session.labels.descriptions?.graphs?.[graph] || "";
export const statusName = (status) => session.labels.statuses?.[status] || status;
export const stepName = (graph, node) => session.labels.steps?.[graph]?.[node] || node;
