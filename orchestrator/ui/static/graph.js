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
        "color": css("--fg"), "background-color": css("--surface"), "border-width": 1.5, "border-color": css("--line"),
        "width": 150, "height": 44, "padding": 6,
      } },
      { selector: "node[kind = 'end']", style: {
        "shape": "ellipse", "width": 18, "height": 18, "label": "data(display)", "font-size": 9,
        "text-valign": "bottom", "text-margin-y": 4, "color": css("--muted"), "background-color": css("--line"), "border-width": 0,
      } },
      { selector: "node[kind = 'group']", style: {
        "shape": "round-rectangle", "background-color": css("--accent-soft"), "background-opacity": 0.35,
        "border-style": "dashed", "border-color": css("--line"), "label": "data(display)", "text-valign": "top",
        "text-halign": "center", "text-margin-y": -4, "font-size": 12, "font-weight": 600, "color": css("--muted"), "padding": 14,
      } },
      { selector: "node[kind = 'subflow']", style: {
        "shape": "round-rectangle", "width": 170, "height": 58, "border-width": 2, "border-style": "double",
        "border-color": css("--accent"), "background-color": css("--accent-soft"), "font-weight": 600,
      } },
      { selector: "edge", style: {
        "curve-style": "taxi", "taxi-direction": "rightward", "width": 1.5, "line-color": css("--line"),
        "target-arrow-shape": "triangle", "target-arrow-color": css("--line"), "arrow-scale": 0.8,
      } },
      { selector: "edge.conditional", style: { "line-style": "dashed" } },
      // A run over the drawing: what did not run fades, what ran takes its outcome's colour.
      { selector: ".dim", style: { "opacity": 0.38 } },
      { selector: "node.ran", style: { "border-width": 2.5, "border-color": css("--good"), "background-color": css("--good-bg") } },
      { selector: "node.ran.wait, node.ran.question", style: { "border-color": css("--wait"), "background-color": css("--wait-bg") } },
      { selector: "node.ran.stop, node.ran.error", style: { "border-color": css("--bad"), "background-color": css("--bad-bg") } },
      { selector: "node[kind = 'group'].ran", style: { "border-color": css("--accent"), "background-color": css("--accent-soft"), "background-opacity": 0.6 } },
      { selector: "node[kind = 'end'].ran", style: { "background-color": css("--accent") } },
      { selector: "edge.ran", style: { "width": 2.5, "line-color": css("--accent"), "target-arrow-color": css("--accent") } },
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

    // Where a node of the whole graph shows: itself, or the outermost collapsed group around it.
    function shownAs(id) {
      let shown = id;
      for (let p = parentOf.get(id); p; p = parentOf.get(p)) if (!opened.has(p)) shown = p;
      return shown;
    }
    const inside = (id, group) => id.startsWith(group + ":");
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
      paint();
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
      cy.elements().addClass("dim").removeClass("ran ok question wait stop error");
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
      if (opened.has(group)) {
        opened.delete(group);
        for (const g of [...opened]) if (inside(g, group)) opened.delete(g); // what was open inside folds with it
      } else {
        opened.add(group);
      }
      build();
      const node = cy.getElementById(group);
      if (!node.length) return;
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
      panel.append(text("p", "mono muted", id));
      if (group) {
        const actions = text("div", "panel-actions");
        actions.append(button(kind === "subflow" ? `Déplier (${stepCount(id)} étapes)` : "Replier", () => toggle(id)));
        const whole = labels.subworkflows?.[id.split(":").pop()];
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

    cy.on("tap", "node", (event) => show(event.target));
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
