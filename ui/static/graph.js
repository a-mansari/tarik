// A workflow drawn as a canvas, as n8n draws one: its steps as boxes from left to right, a file or an invoice as a
// framed group, conditional paths dashed. Over it, one run: each step that ran coloured by its outcome (×n when it
// ran for several invoices), live while the run goes on. A click on a step opens what it did in that run.

(function () {
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

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

  const nodeOf = (step) => (step.path ? step.path.split("|").join(":") + ":" : "") + step.node;

  function text(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = value;
    return node;
  }

  
  // Outcomes from worst to best: a collapsed group takes the worst of the steps inside it.
  const SEVERITY = ["error", "stop", "question", "wait", "ok", ""];
  const worst = (statuses) => SEVERITY.find((s) => statuses.includes(s)) ?? "ok";

  async function draw(container) {
    const graph = container.dataset.graph, run = container.dataset.run || "", panel = document.getElementById(container.dataset.panel);
    const labels = JSON.parse(document.getElementById("labels")?.textContent || "{}");
    const shape = await (await fetch(`/graphs/${graph}`)).json();
    const groups = new Map(shape.nodes.filter((n) => n.kind === "group").map((n) => [n.id, n]));
    const parentOf = new Map(shape.nodes.map((n) => [n.id, n.parent || null]));
    const opened = new Set(); // groups shown whole; every other group is one node, as n8n shows a sub-workflow
    const steps = new Map(); // node id (as in the whole graph) -> the run's steps at that node
    const sequence = []; // every step of the run, in the order it ended
    let children = []; // the runs started in its steps
    let finished = false; // the run has ended: each part's last step went on to its end
    const cy = cytoscape({ container, style: styles(), wheelSensitivity: 0.25, minZoom: 0.1, maxZoom: 2.5 });
    const GRID = 20; // the map's step: nodes and bends snap to it
    const inside = (id, group) => id.startsWith(group + ":");
    const snap = (value) => Math.round(value / GRID) * GRID;

    // Every step's one place, whatever is folded: the automatic layout of the whole workflow with every sub-workflow
    // open, made once, under where the team moved steps (layout.nodes). A sub-workflow has no place of its own: folded,
    // it shows at the centre of its steps; so folding or unfolding one moves nothing, and moving it moves its steps.
    const leaves = shape.nodes.filter((n) => n.kind !== "group").map((n) => n.id);
    const auto = (() => {
      const known = new Set(leaves);
      const whole = cytoscape({
        headless: true, styleEnabled: true,
        style: [{ selector: "node", style: { width: 162, height: 56 } }, { selector: "node.end", style: { width: 18, height: 18 } }],
        elements: [
          ...shape.nodes.filter((n) => n.kind !== "group").map((n) => ({ data: { id: n.id }, classes: n.kind === "end" ? "end" : "" })),
          ...shape.edges.filter((e) => !e.hidden && known.has(e.source) && known.has(e.target)).map((e) => ({ data: { ...e } })),
        ],
      });
      whole.layout({ name: "dagre", rankDir: "LR", nodeSep: 16, rankSep: 40, edgeSep: 8 }).run();
      return Object.fromEntries(whole.nodes().map((n) => [n.id(), { x: snap(n.position("x")), y: snap(n.position("y")) }]));
    })();
    const leafAt = (id) => layout.nodes?.[id] || auto[id];
    // Where a node shows: a step at its place; a sub-workflow, folded, at the centre of the steps inside it.
    function placeOf(id) {
      if (!groups.has(id)) return leafAt(id);
      const points = leaves.filter((leaf) => inside(leaf, id)).map(leafAt).filter(Boolean);
      const xs = points.map((q) => q.x), ys = points.map((q) => q.y);
      return { x: (Math.min(...xs) + Math.max(...xs)) / 2, y: (Math.min(...ys) + Math.max(...ys)) / 2 }; // exactly its frame's centre
    }
    // Edges are kept under the whole graph's own (see keyOf): one kept under a folded shortcut is left behind.
    const edgeIds = new Set(shape.edges.map((e) => e.id));
    const kept = (found) => {
      for (const id of Object.keys(found.edges || {})) if (!edgeIds.has(id)) delete found.edges[id];
      return found;
    };
    let layout = kept(await (await fetch(`/graphs/${graph}/layout`)).json()); // how the team arranged it, shared
    let dirty = false;
    const saveButton = document.getElementById(container.dataset.save || "");
    const undoButton = document.getElementById(container.dataset.undo || "");
    const resetButton = document.getElementById(container.dataset.reset || "");
    const note = document.getElementById(container.dataset.note || "");

    function changed() {
      dirty = true;
      if (saveButton) saveButton.disabled = false;
      if (undoButton) undoButton.disabled = false;
      if (note) note.textContent = "Disposition modifiée, pas encore enregistrée.";
    }
    function settled(message) {
      dirty = false;
      if (saveButton) saveButton.disabled = true;
      if (undoButton) undoButton.disabled = true;
      if (note) note.textContent = message;
    }

    // The grid drawn under the canvas, following its pan and zoom.
    function grid() {
      const size = GRID * cy.zoom(), major = size * 5, pan = cy.pan();
      const at = (step) => `${((pan.x % step) + step) % step}px ${((pan.y % step) + step) % step}px`;
      container.style.backgroundSize = `${size}px ${size}px, ${major}px ${major}px, ${major}px ${major}px`;
      container.style.backgroundPosition = `${at(size)}, ${at(major)}, ${at(major)}`;
    }
    cy.on("viewport", grid);

    // Where a node of the whole graph shows: itself, or the outermost collapsed group around it.
    function shownAs(id) {
      let shown = id;
      for (let p = parentOf.get(id); p; p = parentOf.get(p)) if (!opened.has(p)) shown = p;
      return shown;
    }
    const stepCount = (group) => shape.nodes.filter((n) => n.kind === "step" && inside(n.id, group)).length;

    function build() {
      cy.elements().remove();
      const visible = shape.nodes.filter((n) => shownAs(n.id) === n.id && !(n.kind === "group" && opened.has(n.id)));
      const collapsed = (n) => n.kind === "group";
      cy.add(visible.map((n) => ({
        data: { ...n, parent: undefined, kind: collapsed(n) ? "subflow" : n.kind,
          display: collapsed(n) ? `▸ ${n.label}\n${stepCount(n.id)} étapes` : n.label },
      })));
      const seen = new Map();
      for (const e of shape.edges) {
        const source = shownAs(e.source), target = shownAs(e.target);
        if (source === target) continue; // a path inside a collapsed group
        const id = `${source}->${target}`;
        if (seen.has(id)) {
          const shown = seen.get(id);
          shown.conditional &&= e.conditional;
          shown.hidden &&= e.hidden; // hidden only when every edge it stands for is
          shown.under.push(e.id);
          continue;
        }
        seen.set(id, { id, source, target, conditional: e.conditional, hidden: e.hidden, under: [e.id] });
      }
      cy.add([...seen.values()].map((e) => ({ data: e, classes: [e.conditional && "conditional", e.hidden && "hidden"].filter(Boolean).join(" ") })));
      cy.nodes().forEach((node) => node.position({ ...placeOf(node.id()) })); // each at its one place
      const frames = [...opened].filter((g) => shownAs(g) === g).sort((x, y) => x.split(":").length - y.split(":").length);
      cy.add(frames.map((g) => ({ data: { ...groups.get(g), parent: undefined, display: groups.get(g).label, kind: "group" } })));
      for (const g of frames) {
        const parent = parentOf.get(g);
        if (parent && frames.includes(parent)) cy.getElementById(g).move({ parent });
      }
      for (const n of visible) {
        const parent = parentOf.get(n.id);
        if (parent && frames.includes(parent)) cy.getElementById(n.id).move({ parent });
      }
      cy.edges().forEach(drawEdge);
      paint();
      grid();
    }

    // A list of points as cytoscape places them: each as its share along the source→target line and its distance from
    // it, positive to the left of that line (cytoscape's vectorNormInverse, the same whichever way it pairs the two
    // steps). Worked out from the positions alone: nothing waits for cytoscape to have drawn the edge once.
    function relative(edge, points) {
      const s = edge.source().position(), t = edge.target().position();
      const vx = t.x - s.x, vy = t.y - s.y, length2 = vx * vx + vy * vy || 1, length = Math.sqrt(length2);
      return {
        weights: points.map((p) => ((p.x - s.x) * vx + (p.y - s.y) * vy) / length2).join(" "),
        distances: points.map((p) => ((p.y - s.y) * vx - (p.x - s.x) * vy) / length).join(" "),
      };
    }

    const OVERRIDES = "segment-weights segment-distances control-point-weights control-point-distances edge-distances curve-style source-endpoint target-endpoint";

    // An edge as arranged: its style (straight by default, right angles, or curved), through its bend points when it has
    // some. Curved is an S as n8n draws one: out of its step to the right, into the next from the left.
    function drawEdge(edge) {
      const saved = layout.edges?.[keyOf(edge)] || { points: [], style: "straight" };
      edge.removeStyle(OVERRIDES);
      if (saved.points.length) {
        const r = relative(edge, saved.points);
        edge.style({ "curve-style": "segments", "edge-distances": "node-position", "segment-weights": r.weights, "segment-distances": r.distances });
        return;
      }
      if (saved.style === "bezier") {
        // Out of the side of the source that faces the target, into the side of the target that faces the source:
        // left/right when the cards are further apart sideways, top/bottom when further apart up and down. Its pull is
        // half the gap between the two sides, never less than 30, so the S stretches and flattens with the distance.
        const source = edge.source(), target = edge.target(), s = source.position(), t = target.position();
        const sideways = Math.abs(t.x - s.x) - (source.width() + target.width()) / 2;
        const upright = Math.abs(t.y - s.y) - (source.height() + target.height()) / 2;
        const across = sideways >= upright;
        const dir = across ? Math.sign(t.x - s.x) || 1 : Math.sign(t.y - s.y) || 1;
        const reach = Math.max(30, (across ? sideways : upright) / 2);
        const out = across ? { x: s.x + dir * source.width() / 2, y: s.y } : { x: s.x, y: s.y + dir * source.height() / 2 };
        const into = across ? { x: t.x - dir * target.width() / 2, y: t.y } : { x: t.x, y: t.y - dir * target.height() / 2 };
        const controls = across
          ? [{ x: out.x + dir * reach, y: out.y }, { x: into.x - dir * reach, y: into.y }]
          : [{ x: out.x, y: out.y + dir * reach }, { x: into.x, y: into.y - dir * reach }];
        // cytoscape's angles: 0deg up, 90deg right, 180deg down, 270deg left.
        const exit = across ? (dir > 0 ? "90deg" : "270deg") : (dir > 0 ? "180deg" : "0deg");
        const entry = across ? (dir > 0 ? "270deg" : "90deg") : (dir > 0 ? "0deg" : "180deg");
        const r = relative(edge, controls);
        edge.style({
          "curve-style": "unbundled-bezier", "edge-distances": "node-position", "control-point-weights": r.weights,
          "control-point-distances": r.distances, "source-endpoint": exit, "target-endpoint": entry,
        });
        return;
      }
      edge.style("curve-style", saved.style || "straight");
    }

    // Where an edge's bends and style are kept: under the whole graph's edge it stands for (all parts open), never
    // the folded shortcut, so opening or folding a part moves its boxes and leaves every edge as arranged. A folded
    // edge standing for several takes the one already arranged, else the first.
    function keyOf(edge) {
      const under = edge.data("under") || [edge.id()];
      return under.find((id) => layout.edges?.[id]) || under[0];
    }

    function edgeLayout(edge) {
      layout.edges ||= {};
      return (layout.edges[keyOf(edge)] ||= { points: [], style: "straight" });
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
      cy.fit(undefined, 30);
      if (cy.zoom() >= 0.7) return;
      const start = cy.getElementById("__start__").position();
      cy.zoom(0.85);
      cy.pan({ x: 40 - start.x * 0.85, y: container.clientHeight / 2 - start.y * 0.85 });
    }

    function paint() {
      if (!run) return;
      const { list, now, ended } = shown();
      steps.clear();
      for (const step of list) {
        const id = nodeOf(step);
        if (groups.has(id)) continue; // a part's own closing step: its inner steps already tell what it did
        if (!steps.has(id)) steps.set(id, []);
        steps.get(id).push(step);
      }
      cy.elements().not("[kind = 'handle'], [kind = 'ghost']").addClass("dim").removeClass("ran ok question wait stop error now");
      const byShown = new Map(); // node on screen -> the steps that ran in it (inside it, for a collapsed group)
      for (const [id, list] of steps) {
        const shown = shownAs(id);
        if (!byShown.has(shown)) byShown.set(shown, []);
        byShown.get(shown).push([id, list]);
      }
      for (const [shown, entries] of byShown) {
        const node = cy.getElementById(shown);
        if (!node.length) continue;
        const all = entries.flatMap(([, list]) => list);
        const status = node.data("kind") === "subflow" ? worst(all.map((s) => s.status || "ok")) : all[all.length - 1].status || "ok";
        node.removeClass("dim").addClass(`ran ${status}`);
        // How many times it ran: a step's own count; a collapsed group's, the most any of its first-level steps ran.
        const times = node.data("kind") === "subflow"
          ? Math.max(...entries.filter(([id]) => parentOf.get(id) === shown).map(([, list]) => list.length), 1)
          : all.length;
        const base = node.data("kind") === "subflow" ? `▸ ${node.data("label")}\n${stepCount(shown)} étapes` : node.data("label");
        node.data("display", times > 1 ? `${base}\n×${times}` : base);
        node.ancestors().removeClass("dim").addClass("ran");
      }
      const went = taken(list, ended);
      const reached = (end) => end.removeClass("dim").addClass("ran");
      if (!went) {
        // Without lanes: the run's start always, any other start or end once a step (or an end) next to it ran,
        // marked before the edges (an opened part's start and end sit between steps), until none is left to mark.
        for (let marked = true; marked; ) {
          marked = false;
          cy.nodes("[kind = 'end']").not(".ran").forEach((end) => {
            const touched = end.connectedEdges().some((edge) => edge.source().hasClass("ran") || edge.target().hasClass("ran"));
            if (touched || end.id() === "__start__") { reached(end); marked = true; }
          });
        }
      }
      cy.edges().forEach((edge) => {
        const along = went ? edge.data("under").some((id) => went.has(id)) : edge.source().hasClass("ran") && edge.target().hasClass("ran");
        if (along) edge.removeClass("dim").addClass("ran");
      });
      // With lanes: a start or an end is reached when the run went along an edge to or from it.
      if (went) cy.nodes("[kind = 'end']").filter((end) => end.id() === "__start__" || end.connectedEdges(".ran").length).forEach(reached);
      // The moment replayed: its steps stand out, and the edges into them.
      const current = new Set(now.map((step) => shownAs(nodeOf(step))));
      cy.nodes().filter((node) => current.has(node.id())).addClass("now");
      cy.edges(".ran").filter((edge) => current.has(edge.target().id())).addClass("now");
      tell(now);
    }

    // --- One instance, one moment: what the run's canvas shows ---

    let focus = ""; // the lane shown alone ("": all of them)
    let moment = null; // the moment replayed up to (null: the whole run)

    function shown() {
      const list = focus ? focused(focus) : sequence;
      if (moment == null) return { list, now: [], ended: finished };
      const all = moments(list), upto = Math.min(moment, all.length - 1);
      return { list: all.slice(0, upto + 1).flat(), now: all[upto] || [], ended: false };
    }

    // The run's moments, in the order they ended: the steps of one round of one lane each (parallel steps of a round
    // together); without rounds, one step each.
    function moments(list) {
      const by = new Map();
      for (const step of list) {
        const key = step.round == null ? `#${step.id}` : `${step.lane}@${step.round}`;
        if (!by.has(key)) by.set(key, []);
        by.get(key).push(step);
      }
      // A part's closing step (the part ending, recorded in the part around it) tells nothing new: it goes with the
      // moment before, so each moment shows a step that ran.
      const merged = [];
      for (const group of [...by.values()].sort((a, b) => a[0].id - b[0].id)) {
        if (merged.length && group.every((step) => groups.has(nodeOf(step)))) merged[merged.length - 1].push(...group);
        else merged.push(group);
      }
      return merged;
    }

    // The parts run several times (two invoices, two email threads): each run of one is an instance to show alone.
    function instances() {
      const byPath = new Map(); // path -> parent lane -> its lanes
      for (const step of sequence) {
        if (!step.path || step.lane == null) continue;
        const parent = step.lane.split("|").slice(0, -1).join("|");
        if (!byPath.has(step.path)) byPath.set(step.path, new Map());
        const under = byPath.get(step.path);
        if (!under.has(parent)) under.set(parent, new Set());
        under.get(parent).add(step.lane);
      }
      const found = [];
      for (const [path, under] of byPath) {
        if (![...under.values()].some((lanes) => lanes.size > 1)) continue; // never repeated: nothing to tell apart
        const part = groups.get(path.split("|").join(":"));
        let index = 0;
        for (const lanes of under.values()) {
          for (const lane of lanes) {
            index += 1; // its rank among its part's runs, for one with nothing to be named by
            const mine = sequence.filter((step) => step.lane === lane || step.lane.startsWith(`${lane}|`));
            // Named from its own steps (not the parts inside it): a file by its name, an invoice by its number, a
            // thread by its address; else its kind and rank.
            const words = mine.filter((step) => step.lane === lane).map((step) => step.summary || "").join(" ");
            const label = part?.label || path;
            const found_ = words.match(/[\w-]+\.(pdf|png|jpe?g|xlsx?|csv|txt)/i)?.[0] || words.match(/Facture (\d+)/)?.[0]
              || words.match(/[\w.+-]+@[\w-]+(\.[\w-]+)+/)?.[0] || String(index);
            const name = found_.toLowerCase().startsWith(label.toLowerCase()) ? found_ : `${label} — ${found_}`;
            const status = worst(mine.map((step) => step.status || "ok"));
            found.push({ lane, depth: path.split("|").length, label, name, status, first: mine[0]?.id || 0 });
          }
        }
      }
      return found.sort((a, b) => a.depth - b.depth || a.first - b.first);
    }

    // One instance's steps, and the way to it and on from it in the parts around it: in each, the steps that led to the
    // instance's part, and those that followed it.
    function focused(lane) {
      const went = taken(sequence) || { from: new Map() };
      const keep = new Set(sequence.filter((step) => step.lane === lane || step.lane.startsWith(`${lane}|`)).map((s) => s.id));
      for (let child = lane; child.includes(":"); ) {
        const parent = child.includes("|") ? child.slice(0, child.lastIndexOf("|")) : "";
        const node = child.slice(child.lastIndexOf("|") + 1).split(":")[0];
        // Its closing step in the parent: the parent's steps for that part, matched in the order their lanes ended.
        const lanes = [...new Set(sequence.filter((s) => s.lane.startsWith(parent ? `${parent}|${node}:` : `${node}:`)
          && s.lane.split("|").length === child.split("|").length).map((s) => s.lane))];
        const lastOf = (l) => Math.max(...sequence.filter((s) => s.lane === l || s.lane.startsWith(`${l}|`)).map((s) => s.id));
        lanes.sort((a, b) => lastOf(a) - lastOf(b));
        const closings = sequence.filter((s) => s.lane === parent && s.node === node);
        const closing = closings[lanes.indexOf(child)];
        if (closing) {
          for (let id = closing.id; id != null; id = went.from.get(id)) keep.add(id); // what led to it
          const after = [closing.id]; // and what followed it
          while (after.length) {
            const id = after.pop();
            for (const [next, from] of went.from) if (from === id && !keep.has(next)) { keep.add(next); after.push(next); }
          }
        }
        child = parent;
      }
      return sequence.filter((step) => keep.has(step.id));
    }

    // The moment's steps, listed under the canvas.
    function tell(now) {
      const box = document.getElementById("moment-steps");
      if (!box) return;
      box.replaceChildren();
      for (const step of now.filter((s) => !groups.has(nodeOf(s)))) {
        const li = document.createElement("li");
        li.append(text("span", "num muted", step.when));
        li.append(text("b", "", labels.steps?.[graph]?.[step.node] || labels.steps?.traitement?.[step.node] || step.node));
        if (step.status) li.append(text("span", `chip ${step.status}`, labels.statuses?.[step.status] || step.status));
        if (step.summary) li.append(text("span", "", step.summary));
        box.append(li);
      }
    }

    function controls() {
      const select = document.getElementById("focus"), range = document.getElementById("moment");
      if (!select || !range) return;
      const found = instances();
      select.closest("label").hidden = !found.length;
      const all = text("option", "", "Tout");
      all.value = "";
      select.replaceChildren(all, ...found.map((i) => {
        const option = text("option", "", `${"  ".repeat(i.depth - 1)}${i.name} · ${labels.statuses?.[i.status] || i.status}`);
        option.value = i.lane;
        option.selected = i.lane === focus;
        return option;
      }));
      // One position per moment, then one more at the end: the whole run, landed on its end.
      const count = moments(focus ? focused(focus) : sequence).length;
      range.max = count;
      range.value = moment == null ? count : Math.min(moment, count - 1);
      document.getElementById("moment-label").textContent =
        moment == null ? `Toute l'exécution (${count} moment${count > 1 ? "s" : ""})` : `Moment ${Number(range.value) + 1} / ${count}`;
      document.getElementById("moment-all").disabled = moment == null;
    }

    function wire() {
      const select = document.getElementById("focus"), range = document.getElementById("moment");
      if (!select || !range) return;
      const go = (to) => { moment = to; controls(); paint(); };
      select.addEventListener("change", () => { focus = select.value; moment = null; controls(); paint(); });
      const end = () => Number(range.max); // the last position: the whole run
      range.addEventListener("input", () => go(Number(range.value) >= end() ? null : Number(range.value)));
      document.getElementById("moment-prev").addEventListener("click", () => go(Math.max((moment ?? end()) - 1, 0)));
      document.getElementById("moment-next").addEventListener("click", () => {
        const next = moment == null ? 0 : moment + 1; // from the end: the replay starts again
        go(next >= end() ? null : next);
      });
      document.getElementById("moment-all").addEventListener("click", () => go(null));
    }

    // The whole graph's edges the run went along. In each run of a part (its lane: one invoice, one email thread), each
    // step is reached from the step of the round before with an edge to it (parallel branches each keep their own), or
    // from the part's start (its first step, or the one it resumed at); a step leading nowhere else goes to the part's
    // end. A part's own closing step stands for the part: entered by its start, left from its end. Runs recorded
    // before rounds: the latest earlier step with an edge to it. Before lanes: null, edges lit between steps that ran.
    function taken(list = sequence, ended = finished) {
      if (!list.length || list.some((s) => s.lane == null)) return null;
      const lanes = new Map();
      for (const step of list) {
        if (!lanes.has(step.lane)) lanes.set(step.lane, []);
        lanes.get(step.lane).push(step);
      }
      const edges = new Set(shape.edges.map((e) => e.id));
      const rounds = !list.some((s) => s.round == null); // recorded with their rounds: parallel branches apart
      const went = new Set();
      went.from = new Map(); // step id -> the step that led to it (null: the part's start)
      for (const list of lanes.values()) {
        const prefix = list[0].path ? list[0].path.split("|").join(":") + ":" : "";
        const start = `${prefix}__start__`, end = `${prefix}__end__`;
        const ends = (step) => {
          const id = nodeOf(step);
          return groups.has(id) ? [`${id}:__start__`, `${id}:__end__`] : [id, id]; // a part: entered, left
        };
        const before = []; // [step, its way out] in the order they ended
        const led = new Set(); // steps that led to another
        for (const step of list) {
          const [into, out] = ends(step);
          // What led to it: a step of the round before (or, without rounds, the latest step) with an edge to it.
          const candidates = before.filter(([s]) => !rounds || s.round === step.round - 1).reverse();
          const from = candidates.find(([, o]) => edges.has(`${o}->${into}`));
          if (from) {
            went.add(`${from[1]}->${into}`);
            led.add(from[0]);
            went.from.set(step.id, from[0].id);
          } else if (edges.has(`${start}->${into}`)) {
            went.add(`${start}->${into}`); // where the part began: its first step, or the one it resumed at
            went.from.set(step.id, null);
          }
          before.push([step, out]);
        }
        // Each step that led nowhere else ended the part, when the run is over and an edge goes from it to the end.
        if (ended) for (const [step, out] of before) if (!led.has(step) && edges.has(`${out}->${end}`)) went.add(`${out}->${end}`);
      }
      return went;
    }

    function add(step) {
      if (!sequence.some((s) => s.id === step.id)) sequence.push(step);
    }

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

    function button(label, action) {
      const b = text("button", "panel-button", label);
      b.type = "button";
      b.addEventListener("click", action);
      return b;
    }

    function show(node) {
      if (!panel) return;
      const kind = node.data("kind"), id = node.id();
      const group = kind === "subflow" || kind === "group";
      panel.replaceChildren();
      panel.append(text("p", "eyebrow", group ? "Sous-workflow" : "Étape"));
      panel.append(text("h2", "", node.data("label")));
      if (node.data("description")) panel.append(text("p", "what", node.data("description")));
      panel.append(text("p", "mono muted", id));
      // A step that runs another workflow (Emails' follow-up runs Traitement) opens that workflow's own canvas.
      const whole = labels.subworkflows?.[id.split(":").pop()];
      if (group || (whole && whole !== graph)) {
        const actions = text("div", "panel-actions");
        if (group) actions.append(button(kind === "subflow" ? `Déplier (${stepCount(id)} étapes)` : "Replier", () => toggle(id)));
        if (whole && whole !== graph) {
          const link = text("a", "panel-button", "Ouvrir le workflow");
          link.href = `/ui/workflows/${whole}`;
          actions.append(link);
        }
        panel.append(actions);
      }
      if (!run) {
        const into = node.incomers("node").map((n) => n.data("label")).filter(Boolean);
        const out = node.outgoers("node").map((n) => n.data("label")).filter(Boolean);
        if (into.length) panel.append(text("p", "", `Vient de : ${into.join(", ")}`));
        if (out.length) panel.append(text("p", "", `Mène à : ${out.join(", ")}`));
        panel.append(text("p", "muted", group ? "Double-cliquez dessus pour le déplier ou le replier." : "Choisissez une exécution pour voir ce que cette étape a fait."));
        return;
      }
      const list = group
        ? [...steps.entries()].filter(([sid]) => inside(sid, id)).flatMap(([, l]) => l).sort((x, y) => x.id - y.id)
        : steps.get(id) || [];
      if (!list.length) {
        panel.append(text("p", "muted", group ? "Rien n'a été exécuté dans ce sous-workflow pendant cette exécution." : "Cette étape n'a pas été exécutée dans cette exécution."));
        return;
      }
      // The runs this step started (a workflow the agent ran, the agent a thread was handed to): each its own page.
      const started = children.filter((child) => child.node === id.split(":").pop());
      if (started.length) {
        const links = text("div", "panel-actions");
        for (const child of started) {
          const link = text("a", "panel-button", `${labels.graphs?.[child.graph] || child.graph} · ${child.when} · ${labels.statuses?.[child.status] || child.status}`);
          link.href = `/ui/executions/${child.run_id}`;
          links.append(link);
        }
        panel.append(text("p", "", started.length > 1 ? "Exécutions lancées par cette étape :" : "Exécution lancée par cette étape :"), links);
      }
      const ol = text("ol", "panel-steps");
      for (const step of list) {
        const li = document.createElement("li");
        const head = text("div", "head-line");
        head.append(text("span", "num muted", step.when));
        if (group) head.append(text("b", "", labels.steps?.[graph]?.[step.node] || labels.steps?.traitement?.[step.node] || step.node));
        if (step.status) head.append(text("span", `chip ${step.status}`, labels.statuses?.[step.status] || step.status));
        li.append(head);
        if (step.summary) li.append(text("p", "", step.summary));
        const details = document.createElement("details");
        details.append(text("summary", "", "Tout ce qu'elle a produit"), text("pre", "detail", "…"));
        details.addEventListener("toggle", async () => {
          if (!details.open || details.dataset.loaded) return;
          details.dataset.loaded = "yes";
          const answer = await fetch(`/runs/${run}/steps/${step.id}`);
          details.querySelector("pre").textContent = JSON.stringify(await answer.json(), null, 2);
        });
        li.append(details);
        ol.append(li);
      }
      panel.append(ol);
    }

    cy.on("tap", "node[kind != 'handle']", (event) => { showHandles(null); show(event.target); });
    cy.on("tap", "edge", (event) => { showHandles(event.target); showEdge(event.target); });
    cy.on("tap", (event) => { if (event.target === cy) showHandles(null); });

    // A curve or a bent edge is placed from where its ends are: redrawn as they move, so it adapts while dragging
    // instead of being stretched until the drop.
    cy.on("position", "node[kind != 'handle'][kind != 'ghost']", (event) => {
      event.target.connectedEdges().forEach((edge) => {
        const saved = layout.edges?.[keyOf(edge)];
        if (saved && (saved.style === "bezier" || saved.points.length)) drawEdge(edge);
      });
    });
    // While a step is dragged, a dashed outline shows where it will snap when dropped.
    cy.on("drag", "node[kind != 'handle'][kind != 'ghost']", (event) => {
      const node = event.target;
      if (node.isParent()) return;
      const p = node.position(), placed = { x: snap(p.x), y: snap(p.y) };
      let ghost = cy.getElementById("ghost");
      if (!ghost.length) ghost = cy.add({ data: { id: "ghost", kind: "ghost" }, position: placed, grabbable: false, selectable: false });
      ghost.position(placed);
      ghost.style({ width: node.width(), height: node.height() });
    });
    // A step dropped snaps to the grid and keeps that place. A sub-workflow dropped (its folded box, or its open frame)
    // moves every step inside it by as much, the hidden ones too, and the bend points of the edges it holds.
    let grabbed = null;
    cy.on("grab", "node[kind != 'handle'][kind != 'ghost']", (event) => { grabbed = { ...event.target.position() }; });
    cy.on("dragfree", "node[kind != 'handle'][kind != 'ghost']", (event) => {
      cy.remove("#ghost");
      const target = event.target, id = target.id(), p = target.position();
      layout.nodes ||= {};
      if (!groups.has(id)) {
        layout.nodes[id] = { x: snap(p.x), y: snap(p.y) };
        target.position({ ...layout.nodes[id] });
        target.connectedEdges().forEach(drawEdge);
      } else if (grabbed) {
        const dx = snap(p.x - grabbed.x), dy = snap(p.y - grabbed.y);
        for (const leaf of leaves.filter((l) => inside(l, id))) {
          const q = leafAt(leaf);
          if (q) layout.nodes[leaf] = { x: q.x + dx, y: q.y + dy };
        }
        for (const [edgeId, edge] of Object.entries(layout.edges || {})) {
          const [source, target] = edgeId.split("->");
          if (edge.points?.length && inside(source, id) && inside(target, id)) {
            edge.points = edge.points.map((q) => ({ x: q.x + dx, y: q.y + dy }));
          }
        }
        build();
      }
      grabbed = null;
      changed();
    });
    // A bend handle dragged: the edge follows it live, then it snaps to the grid.
    cy.on("drag", "node[kind = 'handle']", (event) => {
      const handle = event.target, edge = cy.getElementById(handle.data("edge"));
      edgeLayout(edge).points[handle.data("index")] = { ...handle.position() };
      drawEdge(edge);
    });
    cy.on("dragfree", "node[kind = 'handle']", (event) => {
      const handle = event.target, edge = cy.getElementById(handle.data("edge")), p = handle.position();
      const placed = { x: snap(p.x), y: snap(p.y) };
      handle.position(placed);
      edgeLayout(edge).points[handle.data("index")] = placed;
      drawEdge(edge);
      changed();
    });
    cy.on("dbltap", "node[kind = 'handle']", (event) => {
      const edge = cy.getElementById(event.target.data("edge"));
      edgeLayout(edge).points.splice(event.target.data("index"), 1);
      drawEdge(edge);
      showHandles(edge);
      showEdge(edge);
      changed();
    });

    function showEdge(edge) {
      if (!panel) return;
      const saved = edgeLayout(edge);
      panel.replaceChildren();
      panel.append(text("p", "eyebrow", "Liaison"));
      panel.append(text("h2", "", `${edge.source().data("label")} → ${edge.target().data("label")}`));
      panel.append(text("p", "muted", edge.hasClass("conditional") ? "Suivie selon le cas." : "Toujours suivie."));
      const routes = text("div", "panel-actions");
      for (const [style, name] of [["straight", "Droite"], ["taxi", "Angles droits"], ["bezier", "Courbe"]]) {
        const b = button(name, () => { saved.style = style; drawEdge(edge); showEdge(edge); changed(); });
        b.setAttribute("aria-pressed", String(saved.style === style && !saved.points.length));
        routes.append(b);
      }
      panel.append(text("p", "", "Tracé"), routes);
      const bends = text("div", "panel-actions");
      bends.append(button("Ajouter un point de passage", () => {
        const points = saved.points, s = edge.source().position(), t = edge.target().position();
        const from = points.length ? points[points.length - 1] : s; // halfway between the last point and the target
        points.push({ x: snap((from.x + t.x) / 2), y: snap((from.y + t.y) / 2) });
        drawEdge(edge); showHandles(edge); showEdge(edge); changed();
      }));
      if (saved.points.length) {
        bends.append(button("Retirer les points", () => { saved.points = []; drawEdge(edge); showHandles(edge); showEdge(edge); changed(); }));
      }
      panel.append(text("p", "", `Points de passage : ${saved.points.length}`), bends);
      panel.append(text("p", "muted", "Faites glisser un point pour déplacer la liaison ; double-cliquez dessus pour le retirer."));
    }

    saveButton?.addEventListener("click", async () => {
      saveButton.disabled = true;
      try {
        const answer = await fetch(`/graphs/${graph}/layout`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(layout) });
        if (!answer.ok) throw new Error((await answer.json()).error);
        layout = await answer.json();
        settled("Disposition enregistrée pour toute l'équipe.");
      } catch (error) {
        saveButton.disabled = false;
        if (note) note.textContent = `La disposition n'a pas pu être enregistrée (${error.message}). Réessayez.`;
      }
    });
    resetButton?.addEventListener("click", async () => {
      if (resetButton.dataset.confirm !== "yes") { // asked here, on the page: the browser's own confirm is not used
        resetButton.dataset.confirm = "yes"; // the icon then says "Confirmer ?" (CSS), for 5 seconds
        setTimeout(() => { resetButton.dataset.confirm = ""; }, 5000);
        return;
      }
      resetButton.dataset.confirm = "";
      layout = await (await fetch(`/graphs/${graph}/layout`, { method: "DELETE" })).json();
      settled("Disposition automatique rétablie.");
      build();
      home();
    });
    // What was moved since the last save, put back as it was saved (the view stays where it is).
    undoButton?.addEventListener("click", async () => {
      layout = kept(await (await fetch(`/graphs/${graph}/layout`)).json());
      settled("Modifications annulées : la disposition enregistrée est revenue.");
      const view = { zoom: cy.zoom(), pan: { ...cy.pan() } };
      build();
      cy.viewport(view);
    });
    addEventListener("beforeunload", (event) => { if (dirty) event.preventDefault(); });

    cy.on("dbltap", "node", (event) => {
      const kind = event.target.data("kind");
      if (kind === "subflow" || kind === "group") toggle(event.target.id());
    });
    document.getElementById(container.dataset.fit || "")?.addEventListener("click", () => cy.fit(undefined, 24));
    // Every sub-workflow opened at once, or all folded back: while any is folded the button opens them all.
    const foldButton = document.getElementById(container.dataset.fold || "");
    function foldState() {
      if (!foldButton) return;
      foldButton.hidden = !groups.size;
      const all = groups.size && [...groups.keys()].every((g) => opened.has(g));
      foldButton.classList.toggle("folding", Boolean(all));
      const label = all ? "Tout replier" : "Tout déplier";
      foldButton.setAttribute("aria-label", label);
      foldButton.dataset.tip = all ? "Tout replier : refermer chaque sous-workflow" : "Tout déplier : ouvrir chaque sous-workflow";
    }
    foldButton?.addEventListener("click", () => {
      if ([...groups.keys()].every((g) => opened.has(g))) opened.clear();
      else for (const g of groups.keys()) opened.add(g);
      build(); // the view stays as it is, as when one is folded or opened
      foldState();
    });
    cy.on("add remove", "node", () => foldState());
    document.getElementById(container.dataset.home || "")?.addEventListener("click", home);

    if (run) {
      const found = await (await fetch(`/runs/${run}`)).json();
      children = found.children || [];
      finished = !["queued", "running"].includes(found.status);
      (found.steps || []).forEach(add);
      wire();
      controls();
      if (["queued", "running"].includes(found.status)) {
        const events = (window.runEvents ||= window.followRun(run)); // shared with the steps list
        events.on("step", (step) => { add(step); controls(); paint(); });
        events.on("end", () => { finished = true; paint(); });
      }
    }
    build();
    home();
    // Back to the drawing's colours when the viewer switches between light and dark.
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => cy.style(styles()));
  }

  document.querySelectorAll(".canvas[data-graph]").forEach((container) => {
    draw(container).catch(() => { container.textContent = "Le schéma n'a pas pu être dessiné. Rechargez la page."; });
  });
})();
