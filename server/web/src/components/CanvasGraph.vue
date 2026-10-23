<!-- A workflow drawn as a canvas, as n8n draws one: its steps as boxes from left to right, a file or an invoice as a
framed group, conditional paths dashed. Over it, one run (``run``): each step that ran coloured by its outcome (×n when
it ran for several invoices), live while the run goes on (``events``: the run page's followRun, shared with its steps
list; else its own). A click on a step opens what it did in that run, in the panel beside it. -->
<script setup>
import cytoscape from "cytoscape";
import cytoscapeDagre from "cytoscape-dagre";
import { onMounted, onUnmounted, reactive, ref } from "vue";
import { onBeforeRouteLeave, onBeforeRouteUpdate } from "vue-router";

import { graphName, session } from "../api.js";
import { followRun } from "../live.js";

const props = defineProps({
  graph: { type: String, required: true },
  run: { type: String, default: "" },
  events: { type: Object, default: null },
});

// --- Setup: what the page shows, the drawing's own state, small helpers ---

const container = ref(null);
const failed = ref(false);
// What the page shows around the drawing: the panel, the tools, the replay; the drawing itself is cytoscape's.
const panel = ref({ type: "intro" });
const tools = reactive({ dirty: false, note: "", saving: false, confirm: false, foldHidden: true, allOpen: false });
const replay = reactive({ instances: [], focus: "", max: 0, value: 0, label: "", whole: true });
const momentSteps = ref([]);
const details = reactive({}); // step id -> its whole output, once opened
const loaded = new Set(); // the steps whose output was fetched (or is being)

const labels = () => session.labels;
const statusLabel = (status) => labels().statuses?.[status] || status;
const stepLabel = (step) => labels().steps?.[props.graph]?.[step.node] || labels().steps?.traitement?.[step.node] || step.node;

// Icons (stroke, currentColor), each button named for screen readers (aria-label) and on hover (data-tip).
const ICON = { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 2,
  "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" };

// Outcomes from worst to best: a collapsed group takes the worst of the steps inside it.
const SEVERITY = ["error", "stop", "question", "wait", "ok", ""];
const worst = (statuses) => SEVERITY.find((s) => statuses.includes(s)) ?? "ok";
const RUNNING = ["queued", "running"]; // a run's statuses while it goes on
const GRID = 20; // the map's step: nodes and bends snap to it
const OVERRIDES = "segment-weights segment-distances control-point-weights control-point-distances edge-distances curve-style source-endpoint target-endpoint";
// cytoscape's angles for an edge's ends: 0deg up, 90deg right, 180deg down, 270deg left.
const ANGLE = { up: "0deg", right: "90deg", down: "180deg", left: "270deg" };
const MOVABLE = "node[kind != 'handle'][kind != 'ghost']"; // a step or a sub-workflow, not a drawing aid
const CONFIRM_MS = 5000; // how long the reset button waits for its confirmation
// Opened zoomed out to fit, unless that is too small to read: then at START_ZOOM, its start START_MARGIN from the left.
const READABLE_ZOOM = 0.7;
const START_ZOOM = 0.85;
const START_MARGIN = 40;
const HOME_PADDING = 30;
const FIT_PADDING = 24;

// The drawing's state: cytoscape's, never reactive.
let cy = null;
let shape = null; // the whole graph, as the server sends it
let groups = new Map(); // group id -> its node
let parentOf = new Map(); // node id -> its group's id (null: the top level)
let leaves = []; // every node id but the groups'
let auto = {}; // node id -> its place in the automatic layout
let edgeIds = new Set(); // the whole graph's edges
let layout = {}; // how the team arranged it, shared
const opened = new Set(); // groups shown whole; every other group is one node, as n8n shows a sub-workflow
const steps = new Map(); // node id (as in the whole graph) -> the run's steps at that node
const sequence = []; // every step of the run, in the order it ended
let children = []; // the runs started in its steps
let finished = false; // the run has ended: each part's last step went on to its end
let focus = ""; // the lane shown alone ("": all of them)
let moment = null; // the moment replayed up to (null: the whole run)
let ownEvents = null; // the run's events when followed here, not handed by the run page
let gone = false; // unmounted: what is still awaited is dropped
let confirmTimer = null;
let panelEdge = null; // the edge the panel shows
const dark = matchMedia("(prefers-color-scheme: dark)");
const restyle = () => cy?.style(styles());

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const fetchJson = async (url, options) => (await fetch(url, options)).json();
const layoutUrl = () => `/graphs/${props.graph}/layout`;

// A part's path ("a|b") as the prefix of its nodes' ids ("a:b:"); "" for the top level.
const prefixOf = (path) => (path ? path.split("|").join(":") + ":" : "");
const nodeOf = (step) => prefixOf(step.path) + step.node;
const inside = (id, group) => id.startsWith(group + ":");
const isPart = (kind) => kind === "subflow" || kind === "group"; // a sub-workflow, folded or open
const snap = (value) => Math.round(value / GRID) * GRID;
const snapPoint = (p) => ({ x: snap(p.x), y: snap(p.y) });
// A lane ("a|b": part b run inside part a's run a) holds its own steps and those of the parts run inside it.
const inLane = (step, lane) => step.lane === lane || step.lane.startsWith(`${lane}|`);
const parentLane = (lane) => lane.split("|").slice(0, -1).join("|");

function pushTo(map, key, value) {
  if (!map.has(key)) map.set(key, []);
  map.get(key).push(value);
}

function styles() {
  return [
    { selector: "node", style: {
      "shape": "round-rectangle", "label": "data(display)", "text-wrap": "wrap", "text-max-width": 130,
      "text-valign": "center", "text-halign": "center", "font-family": css("--font"), "font-size": 13,
      "color": css("--fg"), "background-color": css("--node-bg"), "border-width": 1.5, "border-color": css("--node-border"),
      "width": 150, "height": 44, "padding": 6,
    } },
    { selector: "node[kind = 'end']", style: {
      "shape": "ellipse", "width": 18, "height": 18, "label": "data(display)", "font-size": 12,
      "text-valign": "bottom", "text-margin-y": 4, "color": css("--muted"), "background-color": css("--edge"), "border-width": 0,
    } },
    { selector: "node[kind = 'group']", style: {
      "shape": "round-rectangle", "background-color": css("--accent-soft"), "background-opacity": 0.35,
      "border-style": "dashed", "border-color": css("--accent"), "label": "data(display)", "text-valign": "top",
      "text-halign": "center", "text-margin-y": -4, "font-size": 12, "font-weight": 600, "color": css("--muted"), "padding": 14,
    } },
    { selector: "node[kind = 'subflow']", style: {
      "shape": "round-rectangle", "width": 170, "height": 58, "border-width": 2, "border-style": "double",
      "border-color": css("--accent"), "background-color": css("--accent-soft"), "font-weight": 600,
    } },
    { selector: "node[kind = 'handle']", style: {
      "shape": "ellipse", "width": 12, "height": 12, "label": "", "background-color": css("--accent"),
      "border-width": 2, "border-color": css("--surface"),
    } },
    { selector: "node[kind = 'ghost']", style: {
      "shape": "round-rectangle", "label": "", "background-opacity": 0, "border-width": 2, "border-style": "dashed",
      "border-color": css("--accent"), "events": "no",
    } },
    { selector: "edge", style: {
      "curve-style": "straight", "taxi-direction": "rightward", "width": 1.6, "line-color": css("--edge"),
      "target-arrow-shape": "triangle", "target-arrow-color": css("--edge"), "arrow-scale": 0.9,
    } },
    { selector: "edge.conditional", style: { "line-style": "dashed" } },
    // A run over the drawing: what did not run fades, what ran takes its outcome's colour.
    { selector: ".dim", style: { "opacity": 0.65 } }, // faded, yet its label still readable (4.5:1) in both themes
    { selector: "node.ran", style: { "border-width": 2.5, "border-color": css("--good"), "background-color": css("--good-bg") } },
    { selector: "node.ran.wait, node.ran.question", style: { "border-color": css("--wait"), "background-color": css("--wait-bg") } },
    { selector: "node.ran.stop, node.ran.error", style: { "border-color": css("--bad"), "background-color": css("--bad-bg") } },
    { selector: "node[kind = 'group'].ran", style: { "border-color": css("--accent"), "background-color": css("--accent-soft"), "background-opacity": 0.6 } },
    { selector: "node[kind = 'end'].ran", style: { "background-color": css("--accent") } },
    { selector: "edge.ran", style: { "width": 2.5, "line-color": css("--accent"), "target-arrow-color": css("--accent") } },
    { selector: "node.now", style: { "border-width": 4, "border-color": css("--accent"), "underlay-color": css("--accent"), "underlay-opacity": 0.18, "underlay-padding": 8 } },
    { selector: "edge.now", style: { "width": 4 } },
    // A way in or out a part rarely takes (an invoice resumed at a later step, a step ending it): shown when a run took it.
    { selector: "edge.hidden", style: { "display": "none" } },
    { selector: "edge.hidden.ran", style: { "display": "element" } },
    { selector: "edge:selected", style: { "width": 3, "line-color": css("--accent"), "target-arrow-color": css("--accent") } },
    { selector: "node:selected", style: { "overlay-color": css("--accent"), "overlay-opacity": 0.12, "border-color": css("--accent"), "border-width": 3 } },
  ];
}

// --- Layout: every step's one place, and how the team arranged steps and edges ---

// Edges are kept under the whole graph's own (see keyOf): one kept under a folded shortcut is left behind.
const kept = (found) => {
  for (const id of Object.keys(found.edges || {})) if (!edgeIds.has(id)) delete found.edges[id];
  return found;
};

function changed() {
  tools.dirty = true;
  tools.note = "Disposition modifiée, pas encore enregistrée.";
}
function settled(message) {
  tools.dirty = false;
  tools.note = message;
}

const leafAt = (id) => layout.nodes?.[id] || auto[id];
const middle = (values) => (Math.min(...values) + Math.max(...values)) / 2;
// Where a node shows: a step at its place; a sub-workflow, folded, at the centre of the steps inside it.
function placeOf(id) {
  if (!groups.has(id)) return leafAt(id);
  const points = leaves.filter((leaf) => inside(leaf, id)).map(leafAt).filter(Boolean);
  return { x: middle(points.map((q) => q.x)), y: middle(points.map((q) => q.y)) }; // exactly its frame's centre
}

// Every step's one place, whatever is folded: the automatic layout of the whole workflow with every sub-workflow
// open, made once, under where the team moved steps (layout.nodes). A sub-workflow has no place of its own: folded,
// it shows at the centre of its steps; so folding or unfolding one moves nothing, and moving it moves its steps.
function automatic() {
  const known = new Set(leaves);
  const nodes = shape.nodes.filter((n) => n.kind !== "group")
    .map((n) => ({ data: { id: n.id }, classes: n.kind === "end" ? "end" : "" }));
  const edges = shape.edges.filter((e) => !e.hidden && known.has(e.source) && known.has(e.target))
    .map((e) => ({ data: { ...e } }));
  const whole = cytoscape({
    headless: true, styleEnabled: true,
    // A step's box with its padding (150 × 44, 6 around); an end's dot.
    style: [{ selector: "node", style: { width: 162, height: 56 } }, { selector: "node.end", style: { width: 18, height: 18 } }],
    elements: [...nodes, ...edges],
  });
  whole.layout({ name: "dagre", rankDir: "LR", nodeSep: 16, rankSep: 40, edgeSep: 8 }).run();
  const places = Object.fromEntries(whole.nodes().map((n) => [n.id(), snapPoint(n.position())]));
  whole.destroy();
  return places;
}

// Where an edge's bends and style are kept: under the whole graph's edge it stands for (all parts open), never
// the folded shortcut, so opening or folding a part moves its boxes and leaves every edge as arranged. A folded
// edge standing for several takes the one already arranged, else the first.
function keyOf(edge) {
  const under = edge.data("under") || [edge.id()];
  return under.find((id) => layout.edges?.[id]) || under[0];
}

const unarranged = () => ({ points: [], style: "straight" });
function edgeLayout(edge) {
  layout.edges ||= {};
  return (layout.edges[keyOf(edge)] ||= unarranged());
}

// --- Drawing: the nodes and edges on screen, the grid under them, the view ---

// The grid drawn under the canvas, following its pan and zoom.
function grid() {
  const size = GRID * cy.zoom();
  const major = size * 5; // a major line every 5 steps
  const pan = cy.pan();
  const offset = (value, step) => ((value % step) + step) % step; // kept positive
  const at = (step) => `${offset(pan.x, step)}px ${offset(pan.y, step)}px`;
  container.value.style.backgroundSize = `${size}px ${size}px, ${major}px ${major}px, ${major}px ${major}px`;
  container.value.style.backgroundPosition = `${at(size)}, ${at(major)}, ${at(major)}`;
}

function build() {
  cy.elements().remove();
  const visible = visibleNodes();
  cy.add(visible.map(nodeElement));
  cy.add(shownEdges().map((e) => ({ data: e, classes: edgeClasses(e) })));
  cy.nodes().forEach((node) => node.position({ ...placeOf(node.id()) })); // each at its one place
  const frames = openFrames();
  cy.add(frames.map(frameElement));
  nestInFrames([...frames, ...visible.map((n) => n.id)], frames);
  cy.edges().forEach(drawEdge);
  paint();
  grid();
}

// The nodes on screen: each not inside a folded group; an open group shows as its frame instead (openFrames).
function visibleNodes() {
  return shape.nodes.filter((n) => shownAs(n.id) === n.id && !(n.kind === "group" && opened.has(n.id)));
}

// A node as drawn: a group drawn as a node is folded, a sub-workflow box that tells its number of steps.
function nodeElement(n) {
  const folded = n.kind === "group";
  return {
    data: { ...n, parent: undefined, kind: folded ? "subflow" : n.kind, display: folded ? foldedDisplay(n.label, n.id) : n.label },
  };
}

// The edges on screen, each between the nodes its ends show as. Several drawn as one are merged: it stands for
// them all (under), conditional or hidden only when each of them is.
function shownEdges() {
  const seen = new Map();
  for (const e of shape.edges) {
    const source = shownAs(e.source);
    const target = shownAs(e.target);
    if (source === target) continue; // a path inside a collapsed group
    const id = `${source}->${target}`;
    const merged = seen.get(id);
    if (!merged) {
      seen.set(id, { id, source, target, conditional: e.conditional, hidden: e.hidden, under: [e.id] });
      continue;
    }
    merged.conditional &&= e.conditional;
    merged.hidden &&= e.hidden; // hidden only when every edge it stands for is
    merged.under.push(e.id);
  }
  return [...seen.values()];
}

const edgeClasses = (e) => [e.conditional && "conditional", e.hidden && "hidden"].filter(Boolean).join(" ");

// The open groups on screen, outermost first.
function openFrames() {
  const depth = (group) => group.split(":").length;
  return [...opened].filter((g) => shownAs(g) === g).sort((x, y) => depth(x) - depth(y));
}

function frameElement(id) {
  const group = groups.get(id);
  return { data: { ...group, parent: undefined, display: group.label, kind: "group" } };
}

// Each frame and node on screen put in the open frame around it, if any.
function nestInFrames(ids, frames) {
  for (const id of ids) {
    const parent = parentOf.get(id);
    if (parent && frames.includes(parent)) cy.getElementById(id).move({ parent });
  }
}

// A list of points as cytoscape places them: each as its share along the source→target line and its distance from
// it, positive to the left of that line (cytoscape's vectorNormInverse, the same whichever way it pairs the two
// steps). Worked out from the positions alone: nothing waits for cytoscape to have drawn the edge once.
function relative(edge, points) {
  const s = edge.source().position();
  const t = edge.target().position();
  const vx = t.x - s.x;
  const vy = t.y - s.y;
  const length2 = vx * vx + vy * vy || 1; // both ends at one place: no division by zero
  const length = Math.sqrt(length2);
  return {
    weights: points.map((p) => ((p.x - s.x) * vx + (p.y - s.y) * vy) / length2).join(" "),
    distances: points.map((p) => ((p.y - s.y) * vx - (p.x - s.x) * vy) / length).join(" "),
  };
}

// An edge as arranged: its style (straight by default, right angles, or curved), through its bend points when it has
// some. Curved is an S as n8n draws one: out of its step to the right, into the next from the left.
function drawEdge(edge) {
  const saved = layout.edges?.[keyOf(edge)] || unarranged();
  edge.removeStyle(OVERRIDES);
  if (saved.points.length) {
    const r = relative(edge, saved.points);
    edge.style({ "curve-style": "segments", "edge-distances": "node-position", "segment-weights": r.weights, "segment-distances": r.distances });
    return;
  }
  if (saved.style === "bezier") {
    edge.style(bezierStyle(edge));
    return;
  }
  edge.style("curve-style", saved.style || "straight");
}

// A curved edge: out of the side of the source that faces the target, into the side of the target that faces the
// source: left/right when the cards are further apart sideways, top/bottom when further apart up and down. Its pull
// is half the gap between the two sides, never less than 30, so the S stretches and flattens with the distance.
function bezierStyle(edge) {
  const source = edge.source();
  const target = edge.target();
  const s = source.position();
  const t = target.position();
  const sideways = Math.abs(t.x - s.x) - (source.width() + target.width()) / 2;
  const upright = Math.abs(t.y - s.y) - (source.height() + target.height()) / 2;
  const across = sideways >= upright;
  const dir = across ? Math.sign(t.x - s.x) || 1 : Math.sign(t.y - s.y) || 1;
  const reach = Math.max(30, (across ? sideways : upright) / 2);
  let out, into, controls, exit, entry;
  if (across) {
    out = { x: s.x + dir * source.width() / 2, y: s.y };
    into = { x: t.x - dir * target.width() / 2, y: t.y };
    controls = [{ x: out.x + dir * reach, y: out.y }, { x: into.x - dir * reach, y: into.y }];
    exit = dir > 0 ? ANGLE.right : ANGLE.left;
    entry = dir > 0 ? ANGLE.left : ANGLE.right;
  } else {
    out = { x: s.x, y: s.y + dir * source.height() / 2 };
    into = { x: t.x, y: t.y - dir * target.height() / 2 };
    controls = [{ x: out.x, y: out.y + dir * reach }, { x: into.x, y: into.y - dir * reach }];
    exit = dir > 0 ? ANGLE.down : ANGLE.up;
    entry = dir > 0 ? ANGLE.up : ANGLE.down;
  }
  const r = relative(edge, controls);
  return {
    "curve-style": "unbundled-bezier", "edge-distances": "node-position", "control-point-weights": r.weights,
    "control-point-distances": r.distances, "source-endpoint": exit, "target-endpoint": entry,
  };
}

// The selected edge's bend points, as small handles to drag; double-click one to remove it.
function showHandles(edge) {
  cy.remove("[kind = 'handle']");
  if (!edge) return;
  edgeLayout(edge).points.forEach((p, index) => {
    cy.add({ data: { id: `handle:${index}`, kind: "handle", edge: edge.id(), index }, position: { x: p.x, y: p.y } });
  });
}

// Opened as n8n opens a long workflow: readable, its start on the left; "Tout voir" fits it whole.
function home() {
  if (!cy) return;
  cy.fit(undefined, HOME_PADDING);
  if (cy.zoom() >= READABLE_ZOOM) return;
  const start = cy.getElementById("__start__").position();
  cy.zoom(START_ZOOM);
  cy.pan({ x: START_MARGIN - start.x * START_ZOOM, y: container.value.clientHeight / 2 - start.y * START_ZOOM });
}
const fit = () => cy?.fit(undefined, FIT_PADDING);

// --- A run over the drawing: what ran, its outcome, the way it went ---

function paint() {
  if (!props.run) return;
  const { list, now, ended } = replayed();
  collectSteps(list);
  cy.elements().not("[kind = 'handle'], [kind = 'ghost']").addClass("dim").removeClass("ran ok question wait stop error now");
  for (const [shownId, entries] of stepsByShown()) markRan(shownId, entries);
  const went = taken(list, ended);
  if (!went) markEndsNear();
  cy.edges().forEach((edge) => {
    if (wentAlong(edge, went)) edge.removeClass("dim").addClass("ran");
  });
  // With lanes: a start or an end is reached when the run went along an edge to or from it.
  if (went) cy.nodes("[kind = 'end']").filter((end) => end.id() === "__start__" || end.connectedEdges(".ran").length).forEach(reached);
  markNow(now);
  tell(now);
}

// The steps shown, by the node they ran at (into steps).
function collectSteps(list) {
  steps.clear();
  for (const step of list) {
    const id = nodeOf(step);
    if (groups.has(id)) continue; // a part's own closing step: its inner steps already tell what it did
    pushTo(steps, id, step);
  }
}

// Node on screen -> [node id, its steps] for each node that ran in it (inside it, for a collapsed group).
function stepsByShown() {
  const byShown = new Map();
  for (const [id, ran] of steps) pushTo(byShown, shownAs(id), [id, ran]);
  return byShown;
}

// A node that ran: coloured by its outcome, and labelled with how many times it ran.
function markRan(shownId, entries) {
  const node = cy.getElementById(shownId);
  if (!node.length) return;
  const all = entries.flatMap(([, ran]) => ran);
  const folded = node.data("kind") === "subflow";
  const status = folded ? worst(all.map((s) => s.status || "ok")) : all[all.length - 1].status || "ok";
  node.removeClass("dim").addClass(`ran ${status}`);
  // How many times it ran: a step's own count; a collapsed group's, the most any of its first-level steps ran.
  const times = folded ? mostRunsInside(shownId, entries) : all.length;
  const base = folded ? foldedDisplay(node.data("label"), shownId) : node.data("label");
  node.data("display", times > 1 ? `${base}\n×${times}` : base);
  node.ancestors().removeClass("dim").addClass("ran");
}

function mostRunsInside(group, entries) {
  const firstLevel = entries.filter(([id]) => parentOf.get(id) === group);
  return Math.max(...firstLevel.map(([, ran]) => ran.length), 1);
}

const reached = (end) => end.removeClass("dim").addClass("ran");

// Without lanes: the run's start always, any other start or end once a step (or an end) next to it ran,
// marked before the edges (an opened part's start and end sit between steps), until none is left to mark.
function markEndsNear() {
  for (let marked = true; marked; ) {
    marked = false;
    cy.nodes("[kind = 'end']").not(".ran").forEach((end) => {
      const touched = end.connectedEdges().some((edge) => edge.source().hasClass("ran") || edge.target().hasClass("ran"));
      if (!touched && end.id() !== "__start__") return;
      reached(end);
      marked = true;
    });
  }
}

// With lanes, an edge standing for one the run took; without, one between two nodes that ran.
function wentAlong(edge, went) {
  if (went) return edge.data("under").some((id) => went.has(id));
  return edge.source().hasClass("ran") && edge.target().hasClass("ran");
}

// The moment replayed: its steps stand out, and the edges into them.
function markNow(now) {
  const current = new Set(now.map((step) => shownAs(nodeOf(step))));
  cy.nodes().filter((node) => current.has(node.id())).addClass("now");
  cy.edges(".ran").filter((edge) => current.has(edge.target().id())).addClass("now");
}

// The whole graph's edges the run went along. In each run of a part (its lane: one invoice, one email thread), each
// step is reached from the step of the round before with an edge to it (parallel branches each keep their own), or
// from the part's start (its first step, or the one it resumed at); a step leading nowhere else goes to the part's
// end. A part's own closing step stands for the part: entered by its start, left from its end. Runs recorded
// before rounds: the latest earlier step with an edge to it. Before lanes: null, edges lit between steps that ran.
function taken(list = sequence, ended = finished) {
  if (!list.length || list.some((s) => s.lane == null)) return null;
  const lanes = new Map();
  for (const step of list) pushTo(lanes, step.lane, step);
  const rounds = !list.some((s) => s.round == null); // recorded with their rounds: parallel branches apart
  const went = new Set();
  went.from = new Map(); // step id -> the step that led to it (null: the part's start)
  for (const laneSteps of lanes.values()) takenInLane(laneSteps, rounds, ended, went);
  return went;
}

// A step's way in and way out as the edges see them: its own node both ways; a part, entered and left.
function waysOf(step) {
  const id = nodeOf(step);
  return groups.has(id) ? [`${id}:__start__`, `${id}:__end__`] : [id, id];
}

// One run of a part: the edges it took, added to went, and what led to each step, to went.from.
function takenInLane(laneSteps, rounds, ended, went) {
  const prefix = prefixOf(laneSteps[0].path);
  const start = `${prefix}__start__`;
  const end = `${prefix}__end__`;
  const before = []; // [step, its way out] in the order they ended
  const led = new Set(); // steps that led to another
  for (const step of laneSteps) {
    const [into, out] = waysOf(step);
    // What led to it: a step of the round before (or, without rounds, the latest step) with an edge to it.
    const candidates = before.filter(([s]) => !rounds || s.round === step.round - 1).reverse();
    const from = candidates.find(([, o]) => edgeIds.has(`${o}->${into}`));
    if (from) {
      went.add(`${from[1]}->${into}`);
      led.add(from[0]);
      went.from.set(step.id, from[0].id);
    } else if (edgeIds.has(`${start}->${into}`)) {
      went.add(`${start}->${into}`); // where the part began: its first step, or the one it resumed at
      went.from.set(step.id, null);
    }
    before.push([step, out]);
  }
  if (!ended) return;
  // Each step that led nowhere else ended the part, when the run is over and an edge goes from it to the end.
  for (const [step, out] of before) {
    if (!led.has(step) && edgeIds.has(`${out}->${end}`)) went.add(`${out}->${end}`);
  }
}

// --- Folding: which sub-workflows show whole, which as one box ---

// Where a node of the whole graph shows: itself, or the outermost collapsed group around it.
function shownAs(id) {
  let shown = id;
  for (let p = parentOf.get(id); p; p = parentOf.get(p)) if (!opened.has(p)) shown = p;
  return shown;
}
const stepCount = (group) => shape.nodes.filter((n) => n.kind === "step" && inside(n.id, group)).length;
const foldedDisplay = (label, group) => `▸ ${label}\n${stepCount(group)} étapes`;
const allOpened = () => [...groups.keys()].every((g) => opened.has(g));

function toggle(group) {
  if (opened.has(group)) {
    opened.delete(group);
    for (const g of [...opened]) if (inside(g, group)) opened.delete(g); // what was open inside folds with it
  } else {
    opened.add(group);
  }
  build();
  const node = cy.getElementById(group);
  if (!node.length) return;
  // The view stays as it is (zoom and place): only what is folded changes. Its panel follows.
  node.select();
  show(node);
}

// Every sub-workflow opened at once, or all folded back: while any is folded the button opens them all.
function foldState() {
  tools.foldHidden = !groups.size;
  tools.allOpen = Boolean(groups.size && allOpened());
}
function foldAll() {
  if (allOpened()) opened.clear();
  else for (const g of groups.keys()) opened.add(g);
  build(); // the view stays as it is, as when one is folded or opened
  foldState();
}

// --- Replay: one instance, one moment, what the run's canvas shows ---

const focusedSteps = () => (focus ? focused(focus) : sequence);

function replayed() {
  const list = focusedSteps();
  if (moment == null) return { list, now: [], ended: finished };
  const all = moments(list);
  const upto = Math.min(moment, all.length - 1);
  return { list: all.slice(0, upto + 1).flat(), now: all[upto] || [], ended: false };
}

// The run's moments, in the order they ended: the steps of one round of one lane each (parallel steps of a round
// together); without rounds, one step each.
function moments(list) {
  const by = new Map();
  for (const step of list) pushTo(by, step.round == null ? `#${step.id}` : `${step.lane}@${step.round}`, step);
  // A part's closing step (the part ending, recorded in the part around it) tells nothing new: it goes with the
  // moment before, so each moment shows a step that ran.
  const merged = [];
  for (const together of [...by.values()].sort((a, b) => a[0].id - b[0].id)) {
    const closingOnly = together.every((step) => groups.has(nodeOf(step)));
    if (merged.length && closingOnly) merged[merged.length - 1].push(...together);
    else merged.push(together);
  }
  return merged;
}

// The parts run several times (two invoices, two email threads): each run of one is an instance to show alone.
function instances() {
  const found = [];
  for (const [path, under] of lanesByPath()) {
    if (![...under.values()].some((lanes) => lanes.size > 1)) continue; // never repeated: nothing to tell apart
    const label = groups.get(path.split("|").join(":"))?.label || path;
    let index = 0;
    for (const lanes of under.values()) {
      for (const lane of lanes) {
        index += 1; // its rank among its part's runs, for one with nothing to be named by
        found.push(instance(lane, path, label, index));
      }
    }
  }
  return found.sort((a, b) => a.depth - b.depth || a.first - b.first);
}

// Each part's runs: path -> the lane it ran in -> its own lanes.
function lanesByPath() {
  const byPath = new Map();
  for (const step of sequence) {
    if (!step.path || step.lane == null) continue;
    const parent = parentLane(step.lane);
    if (!byPath.has(step.path)) byPath.set(step.path, new Map());
    const under = byPath.get(step.path);
    if (!under.has(parent)) under.set(parent, new Set());
    under.get(parent).add(step.lane);
  }
  return byPath;
}

// One run of a part, named from its own steps (not the parts inside it): a file by its name, an invoice by its
// number, a thread by its address; else its kind and rank.
function instance(lane, path, label, index) {
  const mine = sequence.filter((step) => inLane(step, lane));
  const words = mine.filter((step) => step.lane === lane).map((step) => step.summary || "").join(" ");
  const named = nameIn(words) || String(index);
  const name = named.toLowerCase().startsWith(label.toLowerCase()) ? named : `${label} — ${named}`;
  const status = worst(mine.map((step) => step.status || "ok"));
  return { lane, depth: path.split("|").length, label, name, status, first: mine[0]?.id || 0 };
}

// A file name, an invoice number or an email address in an instance's own summaries, the first found in that order.
function nameIn(words) {
  const file = words.match(/[\w-]+\.(pdf|png|jpe?g|xlsx?|csv|txt)/i)?.[0];
  const invoice = words.match(/Facture (\d+)/)?.[0];
  const address = words.match(/[\w.+-]+@[\w-]+(\.[\w-]+)+/)?.[0];
  return file || invoice || address;
}

// One instance's steps, and the way to it and on from it in the parts around it: in each, the steps that led to the
// instance's part, and those that followed it.
function focused(lane) {
  const went = taken(sequence) || { from: new Map() };
  const keep = new Set(sequence.filter((step) => inLane(step, lane)).map((step) => step.id));
  for (let child = lane; child.includes(":"); child = parentLane(child)) {
    const closing = closingStep(child);
    if (!closing) continue;
    keepLedTo(closing.id, went.from, keep);
    keepFollowing(closing.id, went.from, keep);
  }
  return sequence.filter((step) => keep.has(step.id));
}

// A lane's closing step in the lane around it: the parent's steps for that part, matched in the order their lanes ended.
function closingStep(child) {
  const parent = parentLane(child);
  const node = child.split("|").pop().split(":")[0];
  const depth = child.split("|").length;
  const prefix = parent ? `${parent}|${node}:` : `${node}:`;
  const siblings = sequence.filter((s) => s.lane.startsWith(prefix) && s.lane.split("|").length === depth);
  const lanes = [...new Set(siblings.map((s) => s.lane))];
  const lastOf = (l) => Math.max(...sequence.filter((s) => inLane(s, l)).map((s) => s.id));
  lanes.sort((a, b) => lastOf(a) - lastOf(b));
  const closings = sequence.filter((s) => s.lane === parent && s.node === node);
  return closings[lanes.indexOf(child)];
}

// What led to a step, back to its part's start.
function keepLedTo(id, from, keep) {
  for (let at = id; at != null; at = from.get(at)) keep.add(at);
}

// What followed a step, every step on from it.
function keepFollowing(id, from, keep) {
  const after = [id];
  while (after.length) {
    const at = after.pop();
    for (const [nextId, fromId] of from) {
      if (fromId !== at || keep.has(nextId)) continue;
      keep.add(nextId);
      after.push(nextId);
    }
  }
}

// The moment's steps, listed under the canvas.
function tell(now) {
  momentSteps.value = now.filter((s) => !groups.has(nodeOf(s))).map((step) => ({ ...step, name: stepLabel(step) }));
}

function controls() {
  const found = instances();
  replay.instances = found.map((i) => ({ lane: i.lane, text: `${"  ".repeat(i.depth - 1)}${i.name} · ${statusLabel(i.status)}` }));
  replay.focus = focus;
  // One position per moment, then one more at the end: the whole run, landed on its end.
  const count = moments(focusedSteps()).length;
  replay.max = count;
  replay.value = moment == null ? count : Math.min(moment, count - 1);
  replay.label = replayLabel(count);
  replay.whole = moment == null;
}

function replayLabel(count) {
  if (moment == null) return `Toute l'exécution (${count} moment${count > 1 ? "s" : ""})`;
  return `Moment ${replay.value + 1} / ${count}`;
}

function go(to) {
  moment = to;
  controls();
  paint();
}
function onFocus(event) {
  focus = event.target.value;
  moment = null;
  controls();
  paint();
}
const lastPosition = () => replay.max; // the whole run
function onMoment(event) {
  const to = Number(event.target.value);
  go(to >= lastPosition() ? null : to);
}
const previous = () => go(Math.max((moment ?? lastPosition()) - 1, 0));
function next() {
  const to = moment == null ? 0 : moment + 1; // from the end: the replay starts again
  go(to >= lastPosition() ? null : to);
}

// --- The panel: the step or the edge clicked ---

// The panel of the step clicked: what it is, and what it did in the run; a sub-workflow can be folded from it.
function show(node) {
  const kind = node.data("kind");
  const id = node.id();
  const group = isPart(kind);
  // A step that runs another workflow (Emails' follow-up runs Traitement) opens that workflow's own canvas.
  const whole = labels().subworkflows?.[id.split(":").pop()];
  const shownPanel = {
    type: "node", id, group, eyebrow: group ? "Sous-workflow" : "Étape", label: node.data("label"),
    description: node.data("description"), fold: foldLabel(kind, id), whole: whole && whole !== props.graph ? whole : "",
  };
  if (!props.run) {
    shownPanel.into = node.incomers("node").map((n) => n.data("label")).filter(Boolean);
    shownPanel.out = node.outgoers("node").map((n) => n.data("label")).filter(Boolean);
    shownPanel.hint = group ? "Double-cliquez dessus pour le déplier ou le replier." : "Choisissez une exécution pour voir ce que cette étape a fait.";
    panel.value = shownPanel;
    return;
  }
  const list = stepsAt(id, group);
  if (!list.length) {
    shownPanel.empty = group ? "Rien n'a été exécuté dans ce sous-workflow pendant cette exécution." : "Cette étape n'a pas été exécutée dans cette exécution.";
    panel.value = shownPanel;
    return;
  }
  shownPanel.started = startedBy(id);
  shownPanel.steps = list.map((step) => ({ ...step, name: group ? stepLabel(step) : "" }));
  panel.value = shownPanel;
}

function foldLabel(kind, id) {
  if (kind === "subflow") return `Déplier (${stepCount(id)} étapes)`;
  if (kind === "group") return "Replier";
  return "";
}

// The run's steps at a node: a step's own; a sub-workflow's, every step inside it, in the order they ended.
function stepsAt(id, group) {
  if (!group) return steps.get(id) || [];
  const insideIt = [...steps.entries()].filter(([stepId]) => inside(stepId, id));
  return insideIt.flatMap(([, ran]) => ran).sort((x, y) => x.id - y.id);
}

// The runs a step started (a workflow the agent ran, the agent a thread was handed to): each its own page.
function startedBy(id) {
  return children.filter((child) => child.node === id.split(":").pop())
    .map((child) => ({ run_id: child.run_id, words: `${graphName(child.graph)} · ${child.when} · ${statusLabel(child.status)}` }));
}

// A step's whole output, fetched the first time it is opened.
async function loadDetail(step, event) {
  if (!event.target.open || loaded.has(step)) return;
  loaded.add(step);
  try {
    details[step] = JSON.stringify(await fetchJson(`/runs/${props.run}/steps/${step}`), null, 2);
  } catch {
    loaded.delete(step); // opened again: tried again
  }
}

// An edge's bend points changed: drawn again, its handles and panel too.
function redrawEdge(edge) {
  drawEdge(edge);
  showHandles(edge);
  showEdge(edge);
  changed();
}

function showEdge(edge) {
  panelEdge = edge;
  const saved = edgeLayout(edge);
  panel.value = {
    type: "edge", title: `${edge.source().data("label")} → ${edge.target().data("label")}`,
    conditional: edge.hasClass("conditional"), style: saved.style, points: saved.points.length,
  };
}
const ROUTES = [["straight", "Droite"], ["taxi", "Angles droits"], ["bezier", "Courbe"]];
function route(style) {
  const edge = panelEdge;
  edgeLayout(edge).style = style;
  drawEdge(edge);
  showEdge(edge);
  changed();
}
function addPoint() {
  const edge = panelEdge;
  const points = edgeLayout(edge).points;
  const t = edge.target().position();
  const from = points.length ? points[points.length - 1] : edge.source().position(); // halfway between the last point and the target
  points.push(snapPoint({ x: (from.x + t.x) / 2, y: (from.y + t.y) / 2 }));
  redrawEdge(edge);
}
function clearPoints() {
  edgeLayout(panelEdge).points = [];
  redrawEdge(panelEdge);
}

// --- Saved layouts: kept for the whole team ---

async function save() {
  tools.saving = true;
  try {
    const answer = await fetch(layoutUrl(), { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(layout) });
    if (!answer.ok) throw new Error((await answer.json()).error);
    layout = await answer.json();
    settled("Disposition enregistrée pour toute l'équipe.");
  } catch (error) {
    tools.note = `La disposition n'a pas pu être enregistrée (${error.message}). Réessayez.`;
  }
  tools.saving = false;
}
async function reset() {
  // Asked here, on the page: the browser's own confirm is not used. The icon then says "Confirmer ?" (CSS) for a while.
  if (!tools.confirm) {
    tools.confirm = true;
    confirmTimer = setTimeout(() => { tools.confirm = false; }, CONFIRM_MS);
    return;
  }
  tools.confirm = false;
  layout = await fetchJson(layoutUrl(), { method: "DELETE" });
  settled("Disposition automatique rétablie.");
  build();
  home();
}
// What was moved since the last save, put back as it was saved (the view stays where it is).
async function undo() {
  layout = kept(await fetchJson(layoutUrl()));
  settled("Modifications annulées : la disposition enregistrée est revenue.");
  const view = { zoom: cy.zoom(), pan: { ...cy.pan() } };
  build();
  cy.viewport(view);
}

// --- Gestures on the canvas: click, drag, bend, fold ---

function wireCanvas() {
  cy.on("viewport", grid);
  wireSelection();
  wireDragging();
  wireHandles();
  cy.on("dbltap", "node", (event) => {
    if (isPart(event.target.data("kind"))) toggle(event.target.id());
  });
  cy.on("add remove", "node", () => foldState());
}

function wireSelection() {
  cy.on("tap", "node[kind != 'handle']", (event) => {
    showHandles(null);
    show(event.target);
  });
  cy.on("tap", "edge", (event) => {
    showHandles(event.target);
    showEdge(event.target);
  });
  cy.on("tap", (event) => {
    if (event.target === cy) showHandles(null);
  });
}

function wireDragging() {
  cy.on("position", MOVABLE, (event) => {
    event.target.connectedEdges().forEach(followEnds);
  });
  // While a step is dragged, a dashed outline shows where it will snap when dropped.
  cy.on("drag", MOVABLE, (event) => showGhost(event.target));
  // A step dropped snaps to the grid and keeps that place. A sub-workflow dropped (its folded box, or its open frame)
  // moves every step inside it by as much, the hidden ones too, and the bend points of the edges it holds.
  let grabbed = null; // where the node dragged was when grabbed
  cy.on("grab", MOVABLE, (event) => { grabbed = { ...event.target.position() }; });
  cy.on("dragfree", MOVABLE, (event) => {
    cy.remove("#ghost");
    const node = event.target;
    layout.nodes ||= {};
    if (!groups.has(node.id())) dropStep(node);
    else if (grabbed) dropPart(node.id(), node.position(), grabbed);
    grabbed = null;
    changed();
  });
}

// A curve or a bent edge is placed from where its ends are: redrawn as they move, so it adapts while dragging
// instead of being stretched until the drop.
function followEnds(edge) {
  const saved = layout.edges?.[keyOf(edge)];
  if (saved && (saved.style === "bezier" || saved.points.length)) drawEdge(edge);
}

function showGhost(node) {
  if (node.isParent()) return;
  const placed = snapPoint(node.position());
  let ghost = cy.getElementById("ghost");
  if (!ghost.length) ghost = cy.add({ data: { id: "ghost", kind: "ghost" }, position: placed, grabbable: false, selectable: false });
  ghost.position(placed);
  ghost.style({ width: node.width(), height: node.height() });
}

function dropStep(node) {
  const id = node.id();
  layout.nodes[id] = snapPoint(node.position());
  node.position({ ...layout.nodes[id] });
  node.connectedEdges().forEach(drawEdge);
}

function dropPart(id, p, grabbed) {
  const dx = snap(p.x - grabbed.x);
  const dy = snap(p.y - grabbed.y);
  const moved = (q) => ({ x: q.x + dx, y: q.y + dy });
  for (const leaf of leaves.filter((l) => inside(l, id))) {
    const q = leafAt(leaf);
    if (q) layout.nodes[leaf] = moved(q);
  }
  for (const [edgeId, edge] of Object.entries(layout.edges || {})) {
    const [source, target] = edgeId.split("->");
    if (edge.points?.length && inside(source, id) && inside(target, id)) edge.points = edge.points.map(moved);
  }
  build();
}

function wireHandles() {
  const edgeOf = (handle) => cy.getElementById(handle.data("edge"));
  // A bend handle dragged: the edge follows it live, then it snaps to the grid.
  cy.on("drag", "node[kind = 'handle']", (event) => {
    const handle = event.target;
    const edge = edgeOf(handle);
    edgeLayout(edge).points[handle.data("index")] = { ...handle.position() };
    drawEdge(edge);
  });
  cy.on("dragfree", "node[kind = 'handle']", (event) => {
    const handle = event.target;
    const edge = edgeOf(handle);
    const placed = snapPoint(handle.position());
    handle.position(placed);
    edgeLayout(edge).points[handle.data("index")] = placed;
    drawEdge(edge);
    changed();
  });
  cy.on("dbltap", "node[kind = 'handle']", (event) => {
    const edge = edgeOf(event.target);
    edgeLayout(edge).points.splice(event.target.data("index"), 1);
    redrawEdge(edge);
  });
}

// --- Loading and live following ---

function addStep(step) {
  if (!sequence.some((s) => s.id === step.id)) sequence.push(step);
}

async function draw() {
  shape = await fetchJson(`/graphs/${props.graph}`);
  if (gone) return;
  readShape();
  layout = kept(await fetchJson(layoutUrl()));
  if (gone) return;
  cy = cytoscape({ container: container.value, style: styles(), wheelSensitivity: 0.25, minZoom: 0.1, maxZoom: 2.5 });
  wireCanvas();
  if (props.run) {
    await loadRun();
    if (gone) return;
  }
  build();
  home();
}

// The whole graph's lookups, made once it is fetched.
function readShape() {
  groups = new Map(shape.nodes.filter((n) => n.kind === "group").map((n) => [n.id, n]));
  parentOf = new Map(shape.nodes.map((n) => [n.id, n.parent || null]));
  leaves = shape.nodes.filter((n) => n.kind !== "group").map((n) => n.id);
  auto = automatic();
  edgeIds = new Set(shape.edges.map((e) => e.id));
}

async function loadRun() {
  const found = await fetchJson(`/runs/${props.run}`);
  if (gone) return;
  children = found.children || [];
  finished = !RUNNING.includes(found.status);
  (found.steps || []).forEach(addStep);
  controls();
  if (!finished) followLive(props.events || (ownEvents = followRun(props.run))); // the run page's, shared with its steps list
}

// Steps arriving together are drawn once, at the next frame, not once each.
function followLive(events) {
  let pending = false;
  const repaint = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      if (gone) return;
      controls();
      paint();
    });
  };
  events.on("step", (step) => {
    addStep(step);
    repaint();
  });
  events.on("end", () => {
    finished = true;
    repaint();
  });
}

const unsaved = (event) => { if (tools.dirty) event.preventDefault(); };
onMounted(() => {
  addEventListener("beforeunload", unsaved);
  // Back to the drawing's colours when the viewer switches between light and dark.
  dark.addEventListener("change", restyle);
  draw().catch(() => {
    cy?.destroy();
    cy = null;
    failed.value = true;
  });
});
// Leaving the page (or picking another run of it) with a layout moved and not saved: asked first, as the browser asks.
const leave = () => !tools.dirty || confirm("La disposition modifiée n'est pas enregistrée. Quitter la page ?");
onBeforeRouteLeave(leave);
onBeforeRouteUpdate(leave);
onUnmounted(() => {
  gone = true;
  removeEventListener("beforeunload", unsaved);
  dark.removeEventListener("change", restyle);
  clearTimeout(confirmTimer);
  ownEvents?.close();
  cy?.destroy();
});
</script>

<script>
cytoscape.use(cytoscapeDagre); // once, whatever number of canvases
</script>

<template>
  <!-- A run's canvas: one of its instances alone (an invoice, an email thread), and the run replayed moment by moment. -->
  <div v-if="run" class="run-tools">
    <label class="focus" :hidden="!replay.instances.length">Afficher
      <select aria-label="Afficher une seule instance" :value="replay.focus" @change="onFocus">
        <option value="">Tout</option>
        <option v-for="i in replay.instances" :key="i.lane" :value="i.lane">{{ i.text }}</option>
      </select>
    </label>
    <div class="replay" role="group" aria-label="Rejouer l'exécution">
      <button type="button" aria-label="Moment précédent" @click="previous">◀</button>
      <input type="range" min="0" :max="replay.max" :value="replay.value" aria-label="Moment rejoué" @input="onMoment">
      <button type="button" aria-label="Moment suivant" @click="next">▶</button>
      <button type="button" :disabled="replay.whole" @click="go(null)">Tout</button>
      <span class="muted" role="status">{{ replay.label }}</span>
    </div>
  </div>
  <div class="flow">
    <div class="canvas-wrap">
      <div class="canvas-tools">
        <button type="button" @click="home">Au début</button>
        <button type="button" class="icon-button" aria-label="Tout voir" data-tip="Tout voir : le schéma entier, centré" @click="fit">
          <svg v-bind="ICON"><path d="M4 9V5a1 1 0 0 1 1-1h4M15 4h4a1 1 0 0 1 1 1v4M20 15v4a1 1 0 0 1-1 1h-4M9 20H5a1 1 0 0 1-1-1v-4"/><rect x="9" y="9" width="6" height="6" rx="1"/></svg>
        </button>
        <button type="button" id="fold-all" class="icon-button" :class="{ folding: tools.allOpen }" :hidden="tools.foldHidden"
          :aria-label="tools.allOpen ? 'Tout replier' : 'Tout déplier'"
          :data-tip="tools.allOpen ? 'Tout replier : refermer chaque sous-workflow' : 'Tout déplier : ouvrir chaque sous-workflow'" @click="foldAll">
          <svg v-bind="ICON" class="unfold"><path d="M7 15l5 5 5-5"/><path d="M7 9l5-5 5 5"/></svg>
          <svg v-bind="ICON" class="fold"><path d="M7 20l5-5 5 5"/><path d="M7 4l5 5 5-5"/></svg>
        </button>
        <template v-if="session.admin">
          <button type="button" class="icon-button" :disabled="!tools.dirty || tools.saving" aria-label="Enregistrer la disposition"
            data-tip="Enregistrer la disposition, pour toute l'équipe" @click="save">
            <svg v-bind="ICON"><path d="M5 3h11l3 3v13a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2z"/><path d="M8 3v5h7V3"/><rect x="8" y="13" width="8" height="5" rx="1"/></svg>
          </button>
          <button type="button" class="icon-button" :disabled="!tools.dirty" aria-label="Annuler les modifications"
            data-tip="Annuler les modifications : revenir à la disposition enregistrée" @click="undo">
            <svg v-bind="ICON"><path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/></svg>
          </button>
          <button type="button" class="icon-button" :data-confirm="tools.confirm ? 'yes' : ''" aria-label="Disposition automatique"
            data-tip="Disposition automatique : oublier la disposition enregistrée" @click="reset">
            <svg v-bind="ICON"><path d="m4 20 10-10"/><path d="m12 8 2 2"/><path d="M17 3v4M15 5h4M20 10v3M18.5 11.5h3M9 3v2M8 4h2"/></svg>
            <span class="confirm-text">Confirmer ?</span>
          </button>
        </template>
        <span class="canvas-note" role="status">{{ tools.note }}</span>
      </div>
      <div ref="container" class="canvas" role="img" :aria-label="`Schéma du workflow ${graphName(graph)}`"><template
        v-if="failed">Le schéma n'a pas pu être dessiné. Rechargez la page.</template></div>
      <ol v-if="run" class="moment-steps" aria-live="polite">
        <li v-for="step in momentSteps" :key="step.id">
          <span class="num muted">{{ step.when }}</span><b>{{ step.name }}</b>
          <span v-if="step.status" :class="`chip ${step.status}`">{{ statusLabel(step.status) }}</span>
          <span v-if="step.summary">{{ step.summary }}</span>
        </li>
      </ol>
      <!-- What the lines, colours and gestures mean, each shown as it looks on the canvas. -->
      <div class="legend">
        <template v-if="run">
          <span><i class="l-ok"></i>Exécutée</span><span><i class="l-wait"></i>Attend quelqu'un</span><span><i class="l-bad"></i>Arrêtée ou en erreur</span><span><i class="l-none"></i>Pas exécutée</span>
        </template>
        <template v-else>
          <span><svg class="line" width="28" height="10" aria-hidden="true"><line x1="1" y1="5" x2="27" y2="5"/></svg>Toujours suivi</span>
          <span><svg class="line dashed" width="28" height="10" aria-hidden="true"><line x1="1" y1="5" x2="27" y2="5"/></svg>Selon le cas</span>
          <span><svg v-bind="ICON" class="stop"><path d="M8 3h8l5 5v8l-5 5H8l-5-5V8z"/><path d="M8 12h8"/></svg>Une étape pas OK arrête sa facture ou son fichier</span>
          <span><svg v-bind="ICON"><path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/></svg>Après une réponse, la facture reprend à l'étape concernée</span>
        </template>
        <span class="gestures">
          <span><svg v-bind="ICON"><path d="M9 9l6 12 2-5 5-2z"/><path d="M5 3v3M3 5h3M9 2v2M2 9h2"/></svg>Clic : voir</span>
          <span><b class="key">▸</b>Double-clic : déplier</span>
          <span><svg v-bind="ICON"><path d="M12 2v20M2 12h20M12 2l-3 3M12 2l3 3M12 22l-3-3M12 22l3-3M2 12l3-3M2 12l3 3M22 12l-3-3M22 12l-3 3"/></svg>Glisser : ranger</span>
          <span><svg v-bind="ICON"><rect x="6" y="2" width="12" height="20" rx="6"/><path d="M12 6v4"/></svg>Molette : zoom</span>
        </span>
      </div>
    </div>
    <aside class="side panel">
      <template v-if="panel.type === 'intro'">
        <p class="eyebrow">Étape</p>
        <p class="muted">Cliquez sur une étape du schéma pour voir {{ run ? "ce qu'elle a fait dans cette exécution" : "d'où elle vient et où elle mène" }}.</p>
      </template>
      <template v-else-if="panel.type === 'node'">
        <p class="eyebrow">{{ panel.eyebrow }}</p>
        <h2>{{ panel.label }}</h2>
        <p v-if="panel.description" class="what">{{ panel.description }}</p>
        <p class="mono muted">{{ panel.id }}</p>
        <div v-if="panel.fold || panel.whole" class="panel-actions">
          <button v-if="panel.fold" type="button" class="panel-button" @click="toggle(panel.id)">{{ panel.fold }}</button>
          <RouterLink v-if="panel.whole" class="panel-button" :to="`/ui/workflows/${panel.whole}`">Ouvrir le workflow</RouterLink>
        </div>
        <template v-if="!run">
          <p v-if="panel.into.length">Vient de : {{ panel.into.join(", ") }}</p>
          <p v-if="panel.out.length">Mène à : {{ panel.out.join(", ") }}</p>
          <p class="muted">{{ panel.hint }}</p>
        </template>
        <p v-else-if="panel.empty" class="muted">{{ panel.empty }}</p>
        <template v-else>
          <template v-if="panel.started.length">
            <p>{{ panel.started.length > 1 ? "Exécutions lancées par cette étape :" : "Exécution lancée par cette étape :" }}</p>
            <div class="panel-actions">
              <RouterLink v-for="child in panel.started" :key="child.run_id" class="panel-button" :to="`/ui/executions/${child.run_id}`">{{ child.words }}</RouterLink>
            </div>
          </template>
          <ol class="panel-steps">
            <li v-for="step in panel.steps" :key="step.id">
              <div class="head-line">
                <span class="num muted">{{ step.when }}</span>
                <b v-if="step.name">{{ step.name }}</b>
                <span v-if="step.status" :class="`chip ${step.status}`">{{ statusLabel(step.status) }}</span>
              </div>
              <p v-if="step.summary">{{ step.summary }}</p>
              <details @toggle="loadDetail(step.id, $event)">
                <summary>Tout ce qu'elle a produit</summary>
                <pre class="detail">{{ details[step.id] ?? "…" }}</pre>
              </details>
            </li>
          </ol>
        </template>
      </template>
      <template v-else>
        <p class="eyebrow">Liaison</p>
        <h2>{{ panel.title }}</h2>
        <p class="muted">{{ panel.conditional ? "Suivie selon le cas." : "Toujours suivie." }}</p>
        <p>Tracé</p>
        <div class="panel-actions">
          <button v-for="[style, name] in ROUTES" :key="style" type="button" class="panel-button"
            :aria-pressed="String(panel.style === style && !panel.points)" @click="route(style)">{{ name }}</button>
        </div>
        <p>Points de passage : {{ panel.points }}</p>
        <div class="panel-actions">
          <button type="button" class="panel-button" @click="addPoint">Ajouter un point de passage</button>
          <button v-if="panel.points" type="button" class="panel-button" @click="clearPoints">Retirer les points</button>
        </div>
        <p class="muted">Faites glisser un point pour déplacer la liaison ; double-cliquez dessus pour le retirer.</p>
      </template>
    </aside>
  </div>
</template>
