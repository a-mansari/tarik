// A model call's whole prompt, loaded when it is opened: its instructions, its tools, then each message (role
// human/ai/tool; parts text/thinking/tool_use, tool_calls; the AI SDK's user/assistant/reasoning/tool-call too).

const list = document.getElementById("prompt-calls");
const WHO = { system: "Instructions", user: "Client", human: "Client", assistant: "Tarik", ai: "Tarik", tool: "Outil" };
// The same small avatars as a run's conversation (templates/avatar.html), by who speaks.
const KIND = { Client: "client", Tarik: "tarik", Outil: "tool" };
const ICONS = {
  client: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  tarik: '<rect x="4" y="7" width="16" height="13" rx="3"/><path d="M12 3v4M9 13h.01M15 13h.01M9 17h6"/>',
  tool: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z"/>',
  system: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/>',
};

// "who" with its avatar in front: the label of a message, or of the instructions and tools.
function who(label, kind) {
  const span = el("span", "who");
  const avatar = el("span", `avatar avatar-${kind}`);
  avatar.setAttribute("aria-hidden", "true");
  avatar.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[kind]}</svg>`;
  span.append(avatar, label);
  return span;
}

function el(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}
const json = (value) => (typeof value === "string" ? value : JSON.stringify(value, null, 2));

function folded(title, body, open = false) {
  const details = el("details");
  details.open = open;
  details.append(el("summary", "", title), el("pre", "", body));
  return details;
}

function part(p) {
  if (typeof p === "string") return el("pre", "said", p);
  switch (p.type) {
    case "text": return el("pre", "said", p.text);
    case "reasoning": case "thinking": return Object.assign(folded("Sa réflexion", p.text ?? p.thinking), { className: "thinking" });
    case "tool-call": case "tool_use": return el("p", "", `Utilise ${p.toolName ?? p.name} ${json(p.input ?? p.args ?? {})}`);
    case "tool-result": return folded(`Réponse de ${p.toolName ?? "l'outil"}`, json(p.output ?? p.result));
    default: return folded(p.type || "Partie", json(p));
  }
}

function messageItem(m) {
  if (typeof m === "string") return el("li", "", m); // a part removed since
  const role = m.role || "?";
  const li = el("li", role === "tool" ? "tool" : WHO[role] === "Client" ? "client" : "tarik");
  li.append(who(WHO[role] || role, KIND[WHO[role]] || "system"));
  const parts = Array.isArray(m.content) ? m.content : m.content ? [m.content] : [];
  if (role === "tool") li.append(folded(`Réponse de ${m.name || "l'outil"}`, parts.map((p) => (typeof p === "string" ? p : p.text ?? json(p))).join("\n")));
  else parts.forEach((p) => li.append(part(p)));
  const shown = parts.some((p) => p?.type === "tool_use"); // LangChain keeps a call in both places
  if (!shown) for (const call of m.tool_calls || []) li.append(el("p", "", `Utilise ${call.name} ${json(call.args)}`));
  return li;
}

function render(box, prompt) {
  box.replaceChildren();
  const system = el("li", "system");
  system.append(who("Instructions", "system"), folded("Lire les instructions envoyées", json(prompt.system)));
  const tools = el("li", "system");
  tools.append(who(`Outils (${prompt.tools.length})`, "tool"));
  const ul = el("ul", "tools-list");
  for (const t of prompt.tools) {
    const li = el("li");
    li.append(folded(t.name, t.description || "(sans description)"));
    ul.append(li);
  }
  tools.append(ul);
  const talk = el("ol", "talk");
  talk.append(system, tools, ...prompt.messages.map(messageItem));
  box.append(talk);
}

if (list) {
  async function load(details) {
    if (!details.open || details.dataset.loaded) return;
    details.dataset.loaded = "yes";
    const box = details.querySelector(".prompt");
    try {
      const answer = await fetch(`/prompts/${details.dataset.call}`);
      if (!answer.ok) throw new Error(answer.status);
      render(box, await answer.json());
    } catch {
      box.textContent = "Impossible de charger ce prompt. Refermez-le et rouvrez-le pour réessayer.";
      delete details.dataset.loaded;
    }
  }
  list.addEventListener("toggle", (event) => load(event.target), true);
  list.querySelectorAll("details[open]").forEach((details) => { load(details); details.scrollIntoView({ block: "start" }); });
}
