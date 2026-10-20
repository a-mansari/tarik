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

  
  async function draw(container) {
    const graph = container.dataset.graph, run = container.dataset.run || "", panel = document.getElementById(container.dataset.panel);
    const labels = JSON.parse(document.getElementById("labels")?.textContent || "{}");
    const shape = await (await fetch(`/graphs/${graph}`)).json();
    // Laid out flat (dagre lays groups out badly), then each step put into its group's frame where it was placed.
    const elements = [
      ...shape.nodes.filter((n) => n.kind !== "group").map((n) => ({ data: { ...n, parent: undefined, display: n.label } })),
      ...shape.edges.map((e) => ({ data: e, classes: e.conditional ? "conditional" : "" })),
    ];
    const cy = cytoscape({ container, elements, style: styles(), wheelSensitivity: 0.25, minZoom: 0.1, maxZoom: 2.5 });
    cy.layout({ name: "dagre", rankDir: "LR", nodeSep: 16, rankSep: 40, edgeSep: 8, padding: 30 }).run();
    const groups = shape.nodes.filter((n) => n.kind === "group").sort((a, b) => a.id.split(":").length - b.id.split(":").length);
    cy.add(groups.map((g) => ({ data: { ...g, parent: g.parent || undefined, display: g.label } })));
    for (const n of shape.nodes.filter((n) => n.kind !== "group" && n.parent)) cy.getElementById(n.id).move({ parent: n.parent });

    // Opened as n8n opens a long workflow: readable, its start on the left; "Tout voir" fits it whole.
    function home() {
      cy.fit(undefined, 30);
      if (cy.zoom() >= 0.7) return;
      const start = cy.getElementById("__start__").position();
      cy.zoom(0.85);
      cy.pan({ x: 40 - start.x * 0.85, y: container.clientHeight / 2 - start.y * 0.85 });
    }
    home();
    const steps = new Map(); // node id -> the run's steps at that node

    function paint() {
      if (!run) return;
      cy.elements().addClass("dim").removeClass("ran ok question wait stop error");
      for (const [id, list] of steps) {
        const node = cy.getElementById(id);
        if (!node.length) continue;
        const last = list[list.length - 1];
        node.removeClass("dim").addClass(`ran ${last.status || "ok"}`);
        node.data("display", list.length > 1 ? `${node.data("label")}\n×${list.length}` : node.data("label"));
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
      if (!steps.has(id)) steps.set(id, []);
      const list = steps.get(id);
      if (!list.some((s) => s.id === step.id)) list.push(step);
    }

    function show(node) {
      if (!panel) return;
      panel.replaceChildren();
      panel.append(text("p", "eyebrow", node.data("kind") === "group" ? "Groupe" : "Étape"));
      panel.append(text("h2", "", node.data("label")));
      panel.append(text("p", "mono muted", node.id()));
      if (!run) {
        const into = node.incomers("node").map((n) => n.data("label")).filter(Boolean);
        const out = node.outgoers("node").map((n) => n.data("label")).filter(Boolean);
        if (into.length) panel.append(text("p", "", `Vient de : ${into.join(", ")}`));
        if (out.length) panel.append(text("p", "", `Mène à : ${out.join(", ")}`));
        panel.append(text("p", "muted", "Choisissez une exécution pour voir ce que cette étape a fait."));
        return;
      }
      const list = node.data("kind") === "group"
        ? [...steps.entries()].filter(([id]) => id.startsWith(node.id() + ":")).flatMap(([, l]) => l)
        : steps.get(node.id()) || [];
      if (!list.length) {
        panel.append(text("p", "muted", "Cette étape n'a pas été exécutée dans cette exécution."));
        return;
      }
      const ol = text("ol", "panel-steps");
      for (const step of list) {
        const li = document.createElement("li");
        const head = text("div", "head-line");
        head.append(text("span", "num muted", step.when));
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
    document.getElementById(container.dataset.fit || "")?.addEventListener("click", () => cy.fit(undefined, 24));
    document.getElementById(container.dataset.home || "")?.addEventListener("click", home);

    if (run) {
      const found = await (await fetch(`/runs/${run}`)).json();
      (found.steps || []).forEach(add);
      paint();
      if (["queued", "running"].includes(found.status)) {
        const events = new EventSource(`/runs/${run}/events`);
        events.addEventListener("step", (event) => { add(JSON.parse(event.data)); paint(); });
        events.addEventListener("end", () => events.close());
      }
    }
    // Back to the drawing's colours when the viewer switches between light and dark.
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => cy.style(styles()));
  }

  document.querySelectorAll(".canvas[data-graph]").forEach((container) => {
    draw(container).catch(() => { container.textContent = "Le schéma n'a pas pu être dessiné. Rechargez la page."; });
  });
})();
