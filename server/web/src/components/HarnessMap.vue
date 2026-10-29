<!-- Tarik's harness as a flow, left to right (server/api/harness.py): clients and the Experio team write by email or on
the Discussion page, a routine starts at its schedule; each reaches Tarik, his reply going back the
same way, through the conversation it joins: Tarik only talks in conversations. Around Tarik what he reaches himself:
his skills above, his model below, his tools on the right in one lane per family (his read tools, each workflow's), every one acting in Experio but the
CGNC's. Every name is the one the code and the other pages use. Zoom and move as on a workflow's canvas
(CanvasGraph.vue); hovering a node or a lane lights its links while the pointer is on it, a click tells what it does, under the map. -->
<script setup>
import { computed, nextTick, reactive, ref } from "vue";

import { usePage } from "../api.js";

const { data } = usePage(() => "/harnais");

const WIDTH = 2050; // wide: the map zooms, no column squeezed
const TOP = 100; // under the column titles, themselves under the zoom bar
const ROW = 36; // a tool's or a skill's row
const HEAD = 44; // a lane's title
const PAD = 10; // under a lane's last row
const GAP = 26; // between two lanes
const LANE = 300; // a lane's width
const X = { source: 110, channel: 430, conversation: 740, tarik: 1050, tool: 1500, end: 1900 }; // each column's middle
const PILL = { source: 160, channel: 150, conversation: 170, tarik: 112, skill: 210, tool: 250, end: 160, model: 300 }; // widths
const WRAP = { model: 20 }; // characters on a line of these, the rest on a second one
// The families' colours, from the theme's own (app.css, light and dark): a tool or a skill takes its family's.
const HUES = ["--frame-1", "--frame-0", "--wait", "--bad", "--edge-resume", "--edge-cross", "--good", "--accent"];
// The zoom, as on a workflow's canvas (CanvasGraph.vue).
const MIN_ZOOM = 10;
const MAX_ZOOM = 250;
const ZOOM_TICKS = [10, 25, 50, 75, 100, 150, 200, 250];
const ICON = { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 2,
  "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" };

const lit = ref(null);
const picked = ref(null);

// A long name on two lines, cut after its comma, else at the space nearest its middle; the words unchanged.
function lines(name, most) {
  if (!most || name.length <= most) return [name];
  const comma = name.indexOf(", ");
  if (comma > 0) return [name.slice(0, comma + 1), name.slice(comma + 2)];
  const spaces = [...name.matchAll(/ /g)].map((m) => m.index);
  if (!spaces.length) return [name];
  const cut = spaces.reduce((best, at) => (Math.abs(at - name.length / 2) < Math.abs(best - name.length / 2) ? at : best));
  return [name.slice(0, cut), name.slice(cut + 1)];
}

const layout = computed(() => {
  if (!data.value?.nodes.length) return null;
  const nodes = Object.fromEntries(data.value.nodes.map((n) => {
    const text = lines(n.name, WRAP[n.kind]);
    return [n.id, { ...n, lines: text, h: ["tool", "skill"].includes(n.kind) ? 26 : 17 * text.length + 22 }];
  }));
  const edges = data.value.edges;
  const of = (kind) => data.value.nodes.filter((n) => n.kind === kind).map((n) => nodes[n.id]);
  const members = (lane) => data.value.nodes.filter((n) => n.group === lane.id).map((n) => nodes[n.id]);
  // A lane: its title, then its members one per row, from ``top`` down; returns where it ends.
  const stack = (lane, x, top, hue, w = LANE) => {
    Object.assign(lane, { x, y: top, w, hue });
    members(lane).forEach((m, index) => Object.assign(m, { x, y: top + HEAD + index * ROW + ROW / 2, hue }));
    lane.h = HEAD + members(lane).length * ROW + PAD;
    return top + lane.h;
  };

  // The tools' lanes down the tools' column, in Tarik capabilities' order; the skills' lane above Tarik.
  const lanes = of("group").filter((g) => g.id !== "skills");
  let y = TOP;
  lanes.forEach((lane, index) => {
    y = stack(lane, X.tool, y, `var(${HUES[index % HUES.length]})`) + GAP;
  });
  const bottom = y - GAP;
  const middle = (TOP + bottom) / 2;
  Object.assign(nodes.tarik, { x: X.tarik, y: middle });
  const skills = nodes.skills;
  // Left of the lines from Tarik to his tools.
  stack(skills, X.tarik - 90, middle - 120 - (HEAD + members(skills).length * ROW + PAD), "var(--frame-2)", 260);
  for (const kind of ["source", "channel"]) {
    of(kind).forEach((n, index, all) => Object.assign(n, { x: X[kind], y: middle + (index - (all.length - 1) / 2) * 170 }));
  }
  Object.assign(nodes.conversations, { x: X.conversation, y: middle });
  Object.assign(nodes.model, { x: X.tarik, y: middle + 200 });
  Object.assign(nodes.experio, { x: X.end, y: middle, h: 74 });
  const searched = edges.find((e) => e.target === "rag");
  Object.assign(nodes.rag, { x: X.end, y: searched ? nodes[searched.source].y : TOP });

  const half = (n) => (PILL[n.kind] || 0) / 2;
  const curve = (a, b, bend = 0) => {
    const [x1, x2] = a.x < b.x ? [a.x + half(a), b.x - half(b)] : [a.x - half(a), b.x + half(b)];
    const reach = (x2 - x1) / 2;
    return `M${x1},${a.y + bend} C${x1 + reach},${a.y + bend} ${x2 - reach},${b.y + bend} ${x2},${b.y + bend}`;
  };
  const down = (a, b) => {
    const [from, to] = [a.y + 58, b.y - b.h / 2 - 2];
    return `M${a.x},${from} C${a.x},${(from + to) / 2} ${b.x},${(from + to) / 2} ${b.x},${to}`;
  };
  // Up to a skill: out of Tarik's top, into the skill's pill from its right, clear of the channels' lines.
  const up = (a, b) => {
    const [sx, sy, ex] = [a.x + 8, a.y - 56, b.x + half(b) + 2];
    return `M${sx},${sy} C${sx + 20},${sy - 50} ${ex + 45},${b.y} ${ex},${b.y}`;
  };
  const drawn = edges.map((e, index) => {
    const a = nodes[e.source];
    const b = nodes[e.target];
    const bend = { request: -5, reply: 5 }[e.kind] || 0; // a request and its reply side by side
    let path = curve(a, b, bend);
    if (b.kind === "skill") path = up(a, b);
    else if (b.kind === "model") path = down(a, b);
    return { ...e, id: index, path, hue: e.kind === "leads" ? a.hue : b.hue };
  });
  return { nodes: Object.values(nodes).filter((n) => n.kind !== "group"), lanes: [...lanes, skills], edges: drawn, height: bottom + 60 };
});

// What the pointer is on (a node, or a lane: all its members), its links and the nodes at their other end.
const near = computed(() => {
  if (!lit.value || !layout.value) return null;
  const on = new Set([lit.value, ...layout.value.nodes.filter((n) => n.group === lit.value).map((n) => n.id)]);
  const links = layout.value.edges.filter((e) => on.has(e.source) || on.has(e.target));
  return new Set([...on, ...links.flatMap((e) => [e.source, e.target]), ...links.map((e) => `edge:${e.id}`)]);
});
const dim = (key) => (near.value && !near.value.has(key) ? "dim" : "");
const pick = (n) => {
  picked.value = n;
};
const keyed = (event, n) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    pick(n);
  }
};
const KINDS = {
  source: "Qui écrit",
  channel: "Canal",
  tarik: "Agent",
  model: "Modèle",
  conversation: "Conversation",
  group: "Famille",
  skill: "Compétence",
  tool: "Outil",
  end: "Où il agit",
};

// Zoom (the slider, the wheel at the pointer) and moving (dragging the background), on the SVG's viewBox.
const svg = ref(null);
const zoom = ref(100);
const pan = reactive({ x: 0, y: 0 });
const box = computed(() => {
  const height = layout.value.height;
  const [w, h] = [(WIDTH * 100) / zoom.value, (height * 100) / zoom.value];
  return `${(WIDTH - w) / 2 + pan.x} ${(height - h) / 2 + pan.y} ${w} ${h}`;
});
const clamped = (value) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Math.round(value)));
const fit = () => {
  zoom.value = 100;
  Object.assign(pan, { x: 0, y: 0 });
};
const onZoom = (event) => {
  zoom.value = clamped(Number(event.target.value));
};
const at = (event) => new DOMPoint(event.clientX, event.clientY).matrixTransform(svg.value.getScreenCTM().inverse());
async function onWheel(event) {
  const before = at(event);
  zoom.value = clamped(zoom.value * (event.deltaY < 0 ? 1.1 : 1 / 1.1));
  await nextTick();
  const after = at(event);
  pan.x += before.x - after.x;
  pan.y += before.y - after.y;
}
let dragged = null;
const onDown = (event) => {
  if (event.target.closest(".node, .lane")) return;
  dragged = { from: at(event) };
  event.currentTarget.setPointerCapture(event.pointerId);
};
const onMove = (event) => {
  if (!dragged) return;
  const now = at(event); // in the viewBox as it is now: moved by what has been dragged so far
  pan.x += dragged.from.x - now.x;
  pan.y += dragged.from.y - now.y;
};
const onUp = () => {
  dragged = null;
};
</script>

<template>
  <section v-if="layout" class="panel harness" aria-labelledby="harness-title">
    <h2 id="harness-title">Le harnais de Tarik</h2>
    <p class="muted">Comment une demande arrive, ce que Tarik en fait et comment la réponse repart.</p>
    <div class="canvas-wrap">
      <div class="canvas-tools">
        <button type="button" class="icon-button" aria-label="Tout voir" data-tip="Tout voir : le schéma entier, centré" @click="fit">
          <svg v-bind="ICON"><path d="M4 9V5a1 1 0 0 1 1-1h4M15 4h4a1 1 0 0 1 1 1v4M20 15v4a1 1 0 0 1-1 1h-4M9 20H5a1 1 0 0 1-1-1v-4"/><rect x="9" y="9" width="6" height="6" rx="1"/></svg>
        </button>
        <label class="zoom-slider" data-tip="Zoom : glisser, ou cliquer une graduation">
          <svg v-bind="ICON"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4M8 11h6"/></svg>
          <input type="range" :min="MIN_ZOOM" :max="MAX_ZOOM" step="5" list="harness-zoom-ticks" :value="zoom"
            aria-label="Zoom" :aria-valuetext="`${zoom} %`" @input="onZoom">
          <datalist id="harness-zoom-ticks"><option v-for="tick in ZOOM_TICKS" :key="tick" :value="tick" :label="`${tick} %`"></option></datalist>
          <svg v-bind="ICON"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4M8 11h6M11 8v6"/></svg>
          <span class="zoom-value">{{ zoom }} %</span>
        </label>
      </div>
      <svg
        ref="svg"
        class="canvas map"
        :viewBox="box"
        role="img"
        aria-label="Le chemin des demandes jusqu'à Tarik, ses outils et les réponses"
        @mouseleave="lit = null"
        @wheel.prevent="onWheel"
        @pointerdown="onDown"
        @pointermove="onMove"
        @pointerup="onUp"
        @pointercancel="onUp"
      >
        <defs>
          <marker v-for="m in ['request', 'reply', 'uses']" :id="`harness-${m}`" :key="m" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L8,4 L0,8 z" :class="`tip ${m}`" />
          </marker>
          <radialGradient id="harness-core">
            <stop offset="0%" stop-color="var(--accent)" stop-opacity=".95" />
            <stop offset="100%" stop-color="var(--accent)" stop-opacity=".72" />
          </radialGradient>
        </defs>
        <g class="titles">
          <text :x="X.source" y="66">Qui écrit</text>
          <text :x="X.channel" y="66">Canaux</text>
          <text :x="X.conversation" y="66">Conversations</text>
          <text :x="X.tool" y="66">Outils de Tarik</text>
          <text :x="X.end" y="66">Où ils agissent</text>
        </g>
        <g
          v-for="lane in layout.lanes"
          :key="lane.id"
          :class="['lane', dim(lane.id), { on: picked?.id === lane.id }]"
          :style="{ '--hue': lane.hue }"
          tabindex="0"
          role="button"
          :aria-label="`${KINDS.group} : ${lane.name}`"
          @mouseenter="lit = lane.id"
          @focus="lit = lane.id"
          @mouseleave="lit = null"
          @blur="lit = null"
          @click="pick(lane)"
          @keydown="keyed($event, lane)"
        >
          <rect :x="lane.x - lane.w / 2" :y="lane.y" :width="lane.w" :height="lane.h" rx="12" />
          <text :x="lane.x - lane.w / 2 + 14" :y="lane.y + HEAD / 2 + 2" dy=".35em">{{ lane.name }}</text>
        </g>
        <path
          v-for="e in layout.edges"
          :key="e.id"
          :d="e.path"
          :class="['edge', e.kind, dim(`edge:${e.id}`)]"
          :style="{ '--hue': e.hue }"
          :marker-end="e.kind === 'leads' ? 'url(#harness-uses)' : `url(#harness-${e.kind})`"
        />
        <g
          v-for="n in layout.nodes"
          :key="n.id"
          :class="['node', n.kind, dim(n.id), { off: n.off, on: picked?.id === n.id, experio: n.id === 'experio' }]"
          :style="{ '--hue': n.hue }"
          tabindex="0"
          role="button"
          :aria-label="`${KINDS[n.kind]} : ${n.name}`"
          @mouseenter="lit = n.id"
          @focus="lit = n.id"
          @mouseleave="lit = null"
          @blur="lit = null"
          @click="pick(n)"
          @keydown="keyed($event, n)"
        >
          <template v-if="n.kind === 'tarik'">
            <circle :cx="n.x" :cy="n.y" r="72" class="halo" />
            <circle :cx="n.x" :cy="n.y" r="54" fill="url(#harness-core)" />
            <text :x="n.x" :y="n.y" dy=".35em" text-anchor="middle" class="name">{{ n.name }}</text>
          </template>
          <template v-else>
            <rect :x="n.x - PILL[n.kind] / 2" :y="n.y - n.h / 2" :width="PILL[n.kind]" :height="n.h" :rx="Math.min(n.h / 2, 15)" />
            <text :x="n.x" :y="n.y - ((n.lines.length - 1) * 17) / 2" dy=".35em" text-anchor="middle">
              <tspan v-for="(line, index) in n.lines" :key="index" :x="n.x" :dy="index ? 17 : 0">{{ line }}</tspan>
            </text>
          </template>
        </g>
      </svg>
      <div class="legend">
        <span><svg class="key-line request" width="28" height="10"><line x1="1" y1="5" x2="27" y2="5"/></svg>Demande vers Tarik</span>
        <span><svg class="key-line reply" width="28" height="10"><line x1="1" y1="5" x2="27" y2="5"/></svg>Réponse</span>
        <span><svg class="key-line uses" width="28" height="10"><line x1="1" y1="5" x2="27" y2="5"/></svg>Ce que Tarik utilise lui-même</span>
        <span class="gestures">
          <span><svg v-bind="ICON"><path d="M9 9l6 12 2-5 5-2z"/><path d="M5 3v3M3 5h3M9 2v2M2 9h2"/></svg>Clic : voir</span>
          <span><svg v-bind="ICON"><path d="M12 2v20M2 12h20M12 2l-3 3M12 2l3 3M12 22l-3-3M12 22l3-3M2 12l3-3M2 12l3 3M22 12l-3-3M22 12l-3 3"/></svg>Glisser : déplacer</span>
          <span><svg v-bind="ICON"><rect x="6" y="2" width="12" height="20" rx="6"/><path d="M12 6v4"/></svg>Molette : zoom</span>
        </span>
      </div>
    </div>
    <p v-if="picked" class="told" :style="{ '--hue': picked.hue }" aria-live="polite">
      <span class="from">{{ KINDS[picked.kind] }}</span>
      <strong>{{ picked.name }}</strong>
      <span v-if="picked.description">{{ picked.description }}</span>
    </p>
  </section>
</template>

<style scoped>
.harness > p { margin: 0 0 .6rem; }
.map { display: block; height: max(34rem, 82vh); font-family: var(--font); cursor: grab; touch-action: none; user-select: none; }
.map:active { cursor: grabbing; }
.titles text { font-size: 15px; font-weight: 650; fill: var(--muted); text-anchor: middle; letter-spacing: .04em; text-transform: uppercase; }
.lane { cursor: pointer; outline: none; transition: opacity .2s; }
.lane rect { fill: var(--hue); fill-opacity: .08; stroke: var(--hue); stroke-opacity: .45; stroke-width: 1.4; }
.lane text { font-size: 19px; font-weight: 750; fill: var(--hue); }
.lane:hover rect, .lane:focus-visible rect, .lane.on rect { fill-opacity: .16; stroke-opacity: 1; }
.edge { fill: none; stroke-width: 1.4; transition: opacity .2s; }
.edge.request { stroke: var(--accent); stroke-width: 2; stroke-dasharray: 7 5; animation: flow 1.2s linear infinite; }
.edge.reply { stroke: var(--frame-1); stroke-width: 2; stroke-dasharray: 7 5; animation: flow 1.2s linear infinite; }
.edge.uses { stroke: var(--node-border); opacity: .5; }
.edge.leads { stroke: var(--hue); opacity: .55; }
.tip.request { fill: var(--accent); }
.tip.reply { fill: var(--frame-1); }
.tip.uses { fill: var(--node-border); }
@keyframes flow { to { stroke-dashoffset: -24; } }
.node { cursor: pointer; outline: none; transition: opacity .2s; }
.node text { font-size: 14px; fill: var(--fg); paint-order: stroke; stroke: var(--surface); stroke-width: 5px; stroke-linejoin: round; }
.node rect { fill: var(--surface); stroke: var(--hue, var(--node-border)); stroke-width: 1.6; }
.node.source rect { fill: var(--neutral-bg); stroke: var(--muted); }
.node.channel rect { fill: var(--accent-soft); stroke: var(--accent); stroke-width: 2; }
.node.channel.off rect { stroke-dasharray: 4 3; opacity: .6; }
.node.model rect { stroke: var(--wait); stroke-width: 2; }
.node.conversation rect { fill: var(--frame-1-bg); stroke: var(--frame-1); stroke-width: 2.4; }
.node.conversation text { font-weight: 650; }
.node.tool text, .node.skill text { font-size: 13.5px; stroke-width: 0; }
.node.end rect { stroke: var(--fg); stroke-width: 2.4; }
.node.end text { font-weight: 750; font-size: 17px; }
.node.experio rect { fill: var(--accent-soft); stroke: var(--accent); stroke-width: 3; }
.node.experio text { font-size: 24px; }
.node.tarik .halo { fill: var(--accent-soft); animation: breathe 4s ease-in-out infinite; transform-box: fill-box; transform-origin: center; }
.node.tarik .name { font-size: 24px; font-weight: 750; fill: var(--on-accent); stroke-width: 0; }
.node:hover rect, .node:focus-visible rect, .node.on rect { stroke-width: 3; }
.node:hover text, .node:focus-visible text, .node.on text { font-weight: 650; }
.edge.dim, .node.dim, .lane.dim { opacity: .12; }
@keyframes breathe { 50% { transform: scale(1.07); opacity: .7; } }
@media (prefers-reduced-motion: reduce) { .edge.request, .edge.reply, .node.tarik .halo { animation: none; } }
.key-line line { stroke-width: 2; }
.key-line.request line { stroke: var(--accent); stroke-dasharray: 5 3; }
.key-line.reply line { stroke: var(--frame-1); stroke-dasharray: 5 3; }
.key-line.uses line { stroke: var(--node-border); }
.told {
  display: grid; gap: .15rem; margin: .8rem 0 0; padding: .6rem .8rem; border-radius: 8px;
  border-left: 4px solid var(--hue, var(--accent)); background: var(--neutral-bg); overflow-wrap: anywhere;
}
.told .from { color: var(--muted); font-size: .8rem; }
</style>
