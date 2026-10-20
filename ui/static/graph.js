// A workflow drawn as a canvas, as n8n draws one: its steps as boxes from left to right, a file or an invoice as a
// framed group, conditional paths dashed. Over it, one run: each step that ran coloured by its outcome (×n when it
// ran for several invoices), live while the run goes on. A click on a step opens what it did in that run.

(function () {
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function styles() {
    return [
      { selector: "node", style: {
        "shape": "round-rectangle", "label": "data(display)", "text-wrap": "wrap", "text-max-width": 130,
        "text-valign": "center", "text-halign": "center", "font-family": css("--font"), "font-size": 11,
        "color": css("--fg"), "background-color": css("--node-bg"), "border-width": 1.5, "border-color": css("--node-border"),
        "width": 150, "height": 44, "padding": 6,
      } },
      { selector: "node[kind = 'end']", style: {
        "shape": "ellipse", "width": 18, "height": 18, "label": "data(display)", "font-size": 9,
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
      { selector: ".dim", style: { "opacity": 0.5 } },
      { selector: "node.ran", style: { "border-width": 2.5, "border-color": css("--good"), "background-color": css("--good-bg") } },
      { selector: "node.ran.wait, node.ran.question", style: { "border-color": css("--wait"), "background-color": css("--wait-bg") } },
      { selector: "node.ran.stop, node.ran.error", style: { "border-color": css("--bad"), "background-color": css("--bad-bg") } },
      { selector: "node[kind = 'group'].ran", style: { "border-color": css("--accent"), "background-color": css("--accent-soft"), "background-opacity": 0.6 } },
      { selector: "node[kind = 'end'].ran", style: { "background-color": css("--accent") } },
      { selector: "edge.ran", style: { "width": 2.5, "line-color": css("--accent"), "target-arrow-color": css("--accent") } },
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
    const cy = cytoscape({ container, style: styles(), wheelSensitivity: 0.25, minZoom: 0.1, maxZoom: 2.5 });
    const GRID = 20; // the map's step: nodes and bends snap to it
    const inside = (id, group) => id.startsWith(group + ":");
    const snap = (value) => Math.round(value / GRID) * GRID;
    const depth = (id) => id.split(":").length;
    const byDepth = [...groups.keys()].sort((a, b) => depth(b) - depth(a)); // innermost first

    // Where things sit (layout.nodes): a top-level node at its own place; a node inside a sub-workflow relative to the
    // sub-workflow's place, which is where the sub-workflow shows folded and the centre of its steps when open. Folded
    // or open, a sub-workflow is in one place: moving either moves both.
    function fetched(found) {
      if (found.relative) return found;
      // A layout saved before (every place absolute): each sub-workflow's place becomes the centre of its saved steps.
      // Only the nodes the workflow still has: a place kept for a node gone since would pull a centre away.
      const old = Object.fromEntries(Object.entries(found.nodes || {}).filter(([id]) => parentOf.has(id)));
      const anchors = new Map();
      for (const g of byDepth) {
        const inner = Object.keys(old).filter((id) => inside(id, g) && !groups.has(id)).map((id) => old[id]);
        if (inner.length) {
          const xs = inner.map((q) => q.x), ys = inner.map((q) => q.y);
          anchors.set(g, { x: snap((Math.min(...xs) + Math.max(...xs)) / 2), y: snap((Math.min(...ys) + Math.max(...ys)) / 2) });
        } else if (old[g]) anchors.set(g, old[g]);
      }
      const nodes = {};
      const relativeTo = (id, at) => {
        const parent = parentOf.get(id);
        if (!parent) return at;
        const base = anchors.get(parent);
        return base ? { x: at.x - base.x, y: at.y - base.y } : null;
      };
      for (const [id, at] of [...Object.entries(old).filter(([id]) => !groups.has(id)), ...anchors]) {
        const kept = relativeTo(id, at);
        if (kept) nodes[id] = kept;
      }
      return { ...found, nodes, relative: true };
    }
    let layout = fetched(await (await fetch(`/graphs/${graph}/layout`)).json()); // how the team arranged it, shared
    let dirty = false;
    const saveButton = document.getElementById(container.dataset.save || "");
    const undoButton = document.getElementById(container.dataset.undo || "");
    const resetButton = document.getElementById(container.dataset.reset || "");
    const note = document.getElementById(container.dataset.note || "");

    // A node's saved place on the canvas, from its own and its sub-workflows'; null when it or one of them has none.
    function savedAt(id) {
      const own = layout.nodes?.[id];
      if (!own) return null;
      const parent = parentOf.get(id);
      if (!parent) return own;
      const base = savedAt(parent);
      return base ? { x: base.x + own.x, y: base.y + own.y } : null;
    }

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
        if (seen.has(id)) { seen.get(id).conditional &&= e.conditional; continue; }
        seen.set(id, { id, source, target, conditional: e.conditional });
      }
      cy.add([...seen.values()].map((e) => ({ data: e, classes: e.conditional ? "conditional" : "" })));
      // Laid out flat (dagre lays groups out badly), then each opened group framed around its steps.
      cy.layout({ name: "dagre", rankDir: "LR", nodeSep: 16, rankSep: 40, edgeSep: 8, padding: 30 }).run();
      cy.nodes().forEach((node) => { // on the grid from the start, as if each had been dropped there
        const p = node.position();
        node.position({ x: snap(p.x), y: snap(p.y) });
      });
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
      // Where the team put things wins over the automatic layout; the rest stays where dagre put it.
      cy.nodes().forEach((node) => {
        const saved = savedAt(node.id());
        if (saved && !node.isParent()) node.position({ x: saved.x, y: saved.y });
      });
      cy.edges().forEach(drawEdge);
      paint();
      grid();
    }

    // An edge as arranged: its style (right angles, straight, curved), and through its bend points when it has some.
    // A list of points as cytoscape places them: each as its share along the source→target line and its distance from
    // it. Which side a positive distance bends to is checked once against where cytoscape actually draws it.
    function relative(edge, points, sign) {
      const s = edge.source().position(), t = edge.target().position();
      const vx = t.x - s.x, vy = t.y - s.y, length2 = vx * vx + vy * vy || 1, length = Math.sqrt(length2);
      return {
        weights: points.map((p) => ((p.x - s.x) * vx + (p.y - s.y) * vy) / length2).join(" "),
        distances: points.map((p) => sign * ((p.x - s.x) * vy - (p.y - s.y) * vx) / length).join(" "),
      };
    }

    // Placed, then checked against where cytoscape drew the point furthest from the line (a point on the line looks
    // right either way), and placed again on the other side when it landed on the wrong one.
    function oriented(edge, points, apply, drawn) {
      const s = edge.source().position(), t = edge.target().position(), vx = t.x - s.x, vy = t.y - s.y;
      const off = (p) => Math.abs((p.x - s.x) * vy - (p.y - s.y) * vx);
      const far = points.reduce((best, p, index) => (off(p) > off(points[best]) ? index : best), 0);
      apply(relative(edge, points, 1));
      const got = drawn()?.[far], wanted = points[far];
      if (got && Math.hypot(got.x - wanted.x, got.y - wanted.y) > 2) apply(relative(edge, points, -1));
    }

    const OVERRIDES = "segment-weights segment-distances control-point-weights control-point-distances edge-distances curve-style source-endpoint target-endpoint";

    // An edge as arranged: its style (straight by default, right angles, or curved), through its bend points when it has
    // some. Curved is an S as n8n draws one: out of its step to the right, into the next from the left.
    function drawEdge(edge) {
      const saved = layout.edges?.[edge.id()] || { points: [], style: "straight" };
      edge.removeStyle(OVERRIDES);
      if (saved.points.length) {
        oriented(edge, saved.points,
          (r) => edge.style({ "curve-style": "segments", "edge-distances": "node-position", "segment-weights": r.weights, "segment-distances": r.distances }),
          () => edge.segmentPoints());
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
        oriented(edge, controls,
          (r) => edge.style({
            "curve-style": "unbundled-bezier", "edge-distances": "node-position", "control-point-weights": r.weights,
            "control-point-distances": r.distances, "source-endpoint": exit, "target-endpoint": entry,
          }),
          () => edge.controlPoints());
        return;
      }
      edge.style("curve-style", saved.style || "straight");
    }

    function edgeLayout(edge) {
      layout.edges ||= {};
      return (layout.edges[edge.id()] ||= { points: [], style: "straight" });
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
      cy.elements().not("[kind = 'handle'], [kind = 'ghost']").addClass("dim").removeClass("ran ok question wait stop error");
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
      cy.edges().forEach((edge) => {
        if (edge.source().hasClass("ran") && edge.target().hasClass("ran")) edge.removeClass("dim").addClass("ran");
      });
      // The start and the end of what ran: the start always, the end once a step leading to it ran.
      cy.nodes("[kind = 'end']").forEach((end) => {
        const touched = end.connectedEdges().some((edge) => edge.source().hasClass("ran") || edge.target().hasClass("ran"));
        if (touched || end.id() === "__start__") end.removeClass("dim").addClass("ran");
      });
    }

    function add(step) {
      const id = nodeOf(step);
      if (groups.has(id)) return; // a part's own closing step: its inner steps already tell what it did
      if (!steps.has(id)) steps.set(id, []);
      const list = steps.get(id);
      if (!list.some((s) => s.id === step.id)) list.push(step);
    }

    function toggle(group) {
      const before = cy.getElementById(group).position(); // folded: its box; open: the centre of its frame
      const was = { x: before.x, y: before.y };
      if (opened.has(group)) {
        opened.delete(group);
        for (const g of [...opened]) if (inside(g, group)) opened.delete(g); // what was open inside folds with it
      } else {
        opened.add(group);
      }
      build();
      const node = cy.getElementById(group);
      if (!node.length) return;
      if (!savedAt(group)) { // no saved place yet: it opens and folds where it was
        const now = node.position(), dx = snap(was.x - now.x), dy = snap(was.y - now.y);
        const moved = node.isParent() ? node.descendants().not(":parent") : node;
        moved.forEach((n) => { const p = n.position(); n.position({ x: p.x + dx, y: p.y + dy }); });
        moved.connectedEdges().forEach(drawEdge);
      }
      // Opened: the view fits the group's frame; folded back: centred on its node. Its panel follows.
      cy.animate(opened.has(group) ? { fit: { eles: node, padding: 40 }, duration: 250 } : { center: { eles: node }, duration: 250 });
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
        const saved = layout.edges?.[edge.id()];
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
    // An open sub-workflow (and every one open inside it), kept as it now shows: its place the centre of its frame, each
    // node in it relative to that place, ``base`` the place of the sub-workflow around it (null at the top).
    function keep(group, base) {
      const frame = cy.getElementById(group), p = frame.position(), at = { x: snap(p.x), y: snap(p.y) };
      layout.nodes[group] = base ? { x: at.x - base.x, y: at.y - base.y } : at;
      frame.children().forEach((child) => {
        if (child.isParent()) return keep(child.id(), at);
        const q = child.position();
        layout.nodes[child.id()] = { x: q.x - at.x, y: q.y - at.y };
      });
    }
    // A bend point belongs to the sub-workflow holding both ends of its edge: it moves when that one does.
    function bendsFollow(before) {
      for (const [id, edge] of Object.entries(layout.edges || {})) {
        if (!edge.points?.length) continue;
        const [source, target] = id.split("->");
        const g = byDepth.find((x) => inside(source, x) && inside(target, x));
        const from = g && before.get(g), to = g && savedAt(g);
        if (!from || !to || (from.x === to.x && from.y === to.y)) continue;
        edge.points = edge.points.map((q) => ({ x: q.x + to.x - from.x, y: q.y + to.y - from.y }));
      }
    }
    // A node dropped snaps to the grid (a group's frame with all its steps), and its place is kept for saving: a
    // top-level one as it is, anything inside an open sub-workflow by keeping that whole sub-workflow as it shows.
    cy.on("dragfree", "node[kind != 'handle'][kind != 'ghost']", (event) => {
      cy.remove("#ghost");
      const target = event.target, before = new Map(byDepth.map((g) => [g, savedAt(g)]));
      const moved = target.isParent() ? target.descendants().not(":parent") : target;
      moved.forEach((node) => {
        const p = node.position();
        node.position({ x: snap(p.x), y: snap(p.y) });
      });
      layout.nodes ||= {};
      const outermost = target.ancestors().last();
      if (outermost.length) keep(outermost.id(), null);
      else if (target.isParent()) keep(target.id(), null);
      else layout.nodes[target.id()] = { ...target.position() };
      bendsFollow(before);
      cy.edges().forEach(drawEdge);
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
        layout = fetched(await answer.json());
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
      layout = fetched(await (await fetch(`/graphs/${graph}/layout`, { method: "DELETE" })).json());
      settled("Disposition automatique rétablie.");
      build();
      home();
    });
    // What was moved since the last save, put back as it was saved (the view stays where it is).
    undoButton?.addEventListener("click", async () => {
      layout = fetched(await (await fetch(`/graphs/${graph}/layout`)).json());
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
    document.getElementById(container.dataset.home || "")?.addEventListener("click", home);

    if (run) {
      const found = await (await fetch(`/runs/${run}`)).json();
      (found.steps || []).forEach(add);
      if (["queued", "running"].includes(found.status)) {
        const events = new EventSource(`/runs/${run}/events`);
        events.addEventListener("step", (event) => { add(JSON.parse(event.data)); paint(); });
        events.addEventListener("end", () => events.close());
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
