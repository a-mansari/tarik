// A model call's whole prompt, loaded when it is opened: its instructions, its tools, then each message. Reads both
// n8n's messages (role user/assistant/tool, parts text/reasoning/tool-call/tool-result) and LangChain's (role
// human/ai/tool, parts text/thinking/tool_use, tool_calls).

const list = document.getElementById("prompt-calls");
const WHO = { system: "Instructions", user: "Client", human: "Client", assistant: "Tarik", ai: "Tarik", tool: "Outil" };

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
  li.append(el("span", "who", WHO[role] || role));
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
  system.append(el("span", "who", "Instructions"), folded("Lire les instructions envoyées", json(prompt.system)));
  const tools = el("li", "system");
  tools.append(el("span", "who", `Outils (${prompt.tools.length})`));
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
  const cabinet = list.dataset.cabinet;
  async function load(details) {
    if (!details.open || details.dataset.loaded) return;
    details.dataset.loaded = "yes";
    const box = details.querySelector(".prompt");
    try {
      const answer = await fetch(`/prompts/${encodeURIComponent(cabinet)}/${details.dataset.call}`);
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
