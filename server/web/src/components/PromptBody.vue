<!-- A model call's whole prompt: its instructions, its tools, then each message (role human/ai/tool; parts
text/thinking/tool_use, tool_calls; the AI SDK's user/assistant/reasoning/tool-call too). -->
<script setup>
import RunAvatar from "./RunAvatar.vue";

defineProps({ prompt: { type: Object, required: true } });

const WHO = { system: "Instructions", user: "Client", human: "Client", assistant: "Tarik", ai: "Tarik", tool: "Outil" };
// The same small avatars as a run's conversation, by who speaks.
const KIND = { Client: "client", Tarik: "tarik", Outil: "tool" };
const json = (value) => (typeof value === "string" ? value : JSON.stringify(value, null, 2));

// A part as shown: a text ("said"), a line ("line"), or folded under a title ("folded", "thinking" for its reflection).
function part(p) {
  if (typeof p === "string") return { as: "said", body: p };
  switch (p.type) {
    case "text": return { as: "said", body: p.text };
    case "reasoning":
    case "thinking":
      return { as: "folded", className: "thinking", title: "Sa réflexion", body: p.text ?? p.thinking };
    case "tool-call":
    case "tool_use":
      return { as: "line", body: `Utilise ${p.toolName ?? p.name} ${json(p.input ?? p.args ?? {})}` };
    case "tool-result":
      return { as: "folded", title: `Réponse de ${p.toolName ?? "l'outil"}`, body: json(p.output ?? p.result) };
    default:
      return { as: "folded", title: p.type || "Partie", body: json(p) };
  }
}

// A message's content as a list of parts: a list as it is, a single part in one, none as none.
function partsOf(content) {
  if (Array.isArray(content)) return content;
  return content ? [content] : [];
}

// The bubble's class: a tool's answer, the client's words, or Tarik's.
function bubbleClass(role, who) {
  if (role === "tool") return "tool";
  return who === "Client" ? "client" : "tarik";
}

function message(m) {
  if (typeof m === "string") return { plain: m }; // a part removed since
  const role = m.role || "?";
  const who = WHO[role] || role;
  const parts = partsOf(m.content);
  let shown;
  if (role === "tool") {
    const body = parts.map((p) => (typeof p === "string" ? p : p.text ?? json(p))).join("\n");
    shown = [{ as: "folded", title: `Réponse de ${m.name || "l'outil"}`, body }];
  } else {
    shown = parts.map(part);
  }
  const called = parts.some((p) => p?.type === "tool_use"); // LangChain keeps a call in both places
  if (!called) {
    for (const call of m.tool_calls || []) shown.push({ as: "line", body: `Utilise ${call.name} ${json(call.args)}` });
  }
  return { className: bubbleClass(role, who), who, kind: KIND[who] || "system", parts: shown };
}
</script>

<template>
  <ol class="talk">
    <li class="system">
      <span class="who"><RunAvatar kind="system" />Instructions</span>
      <details><summary>Lire les instructions envoyées</summary><pre>{{ json(prompt.system) }}</pre></details>
    </li>
    <li class="system">
      <span class="who"><RunAvatar kind="tool" />Outils ({{ prompt.tools.length }})</span>
      <ul class="tools-list">
        <li v-for="(tool, index) in prompt.tools" :key="index">
          <details><summary>{{ tool.name }}</summary><pre>{{ tool.description || "(sans description)" }}</pre></details>
        </li>
      </ul>
    </li>
    <template v-for="(m, index) in prompt.messages.map(message)" :key="index">
      <li v-if="m.plain !== undefined">{{ m.plain }}</li>
      <li v-else :class="m.className">
        <span class="who"><RunAvatar :kind="m.kind" />{{ m.who }}</span>
        <template v-for="(p, at) in m.parts" :key="at">
          <pre v-if="p.as === 'said'" class="said">{{ p.body }}</pre>
          <p v-else-if="p.as === 'line'">{{ p.body }}</p>
          <details v-else :class="p.className"><summary>{{ p.title }}</summary><pre>{{ p.body }}</pre></details>
        </template>
      </li>
    </template>
  </ol>
</template>
