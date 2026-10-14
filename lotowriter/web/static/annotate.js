// Photo markup: arrows, circles, boxes and text labels drawn over a photo.
// Shapes are stored as fractions of the photo's width/height so they print at any size.
// Proportions match lotowriter/photos.py (server-side print rendering).
const STROKE = 0.012;
const ARROW_HEAD = 0.05;
const TEXT_SIZE = 0.06;
const COLORS = { red: "#e60000", yellow: "#ffd600", white: "#ffffff", black: "#000000" };
const OUTLINE = { red: "#ffffff", yellow: "#000000", white: "#000000", black: "#ffffff" };
const SVG = "http://www.w3.org/2000/svg";

function s(tag, attrs) {
  const e = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

function shapeNodes(a, W, H) {
  const L = Math.max(W, H);
  const width = Math.max(2, STROKE * L);
  const halo = Math.max(1, width / 3);
  const color = COLORS[a.color] || COLORS.red;
  const outline = OUTLINE[a.color] || "#fff";
  const nodes = [];
  if (a.type === "arrow") {
    const x1 = a.x1 * W, y1 = a.y1 * H, x2 = a.x2 * W, y2 = a.y2 * H;
    const ang = Math.atan2(y2 - y1, x2 - x1);
    const head = ARROW_HEAD * L;
    const pts = [[x2, y2], [x2 - head * Math.cos(ang - 0.45), y2 - head * Math.sin(ang - 0.45)],
      [x2 - head * Math.cos(ang + 0.45), y2 - head * Math.sin(ang + 0.45)]].map((p) => p.join(",")).join(" ");
    const bx = x2 - 0.8 * head * Math.cos(ang), by = y2 - 0.8 * head * Math.sin(ang);
    nodes.push(s("line", { x1, y1, x2: bx, y2: by, stroke: outline, "stroke-width": width + 2 * halo, "stroke-linecap": "round" }));
    nodes.push(s("polygon", { points: pts, fill: outline, stroke: outline, "stroke-width": 2 * halo, "stroke-linejoin": "round" }));
    nodes.push(s("line", { x1, y1, x2: bx, y2: by, stroke: color, "stroke-width": width, "stroke-linecap": "round" }));
    nodes.push(s("polygon", { points: pts, fill: color }));
  } else if (a.type === "ellipse" || a.type === "rect") {
    const x0 = Math.min(a.x, a.x + a.w) * W, y0 = Math.min(a.y, a.y + a.h) * H;
    const w = Math.abs(a.w) * W, h = Math.abs(a.h) * H;
    const geo = a.type === "ellipse" ? ["ellipse", { cx: x0 + w / 2, cy: y0 + h / 2, rx: w / 2, ry: h / 2 }]
      : ["rect", { x: x0, y: y0, width: w, height: h }];
    nodes.push(s(geo[0], { ...geo[1], fill: "none", stroke: outline, "stroke-width": width + 2 * halo }));
    nodes.push(s(geo[0], { ...geo[1], fill: "none", stroke: color, "stroke-width": width }));
  } else if (a.type === "text") {
    const size = Math.max(10, TEXT_SIZE * L);
    const t = s("text", { x: a.x * W, y: a.y * H, fill: color, stroke: outline, "stroke-width": Math.max(1, size / 12) * 2,
      "paint-order": "stroke", "font-size": size, "font-weight": "700", "font-family": "Arial, sans-serif",
      "dominant-baseline": "middle" });
    t.textContent = a.text;
    nodes.push(t);
  }
  return nodes;
}

export function overlay(shapes, W, H) {
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", class: "markup", "aria-hidden": "true" });
  for (const a of shapes || []) svg.append(...shapeNodes(a, W, H));
  return svg;
}

// Photo element with its markup on top. src may be a URL or a blob URL.
export function photoFigure(src, ph, alt) {
  const W = ph.width || 1000, H = ph.height || 750;
  const fig = document.createElement("div");
  fig.className = "figure";
  fig.style.aspectRatio = `${W} / ${H}`;
  const img = document.createElement("img");
  img.src = src;
  img.alt = alt;
  img.loading = "lazy";
  fig.append(img, overlay(ph.annotations, W, H));
  return fig;
}

// Full-screen editor. Resolves with the new shapes, or null if cancelled.
export function editMarkup({ src, photo, label, others = [] }) {
  return new Promise((resolve) => {
    const W = photo.width || 1000, H = photo.height || 750;
    let shapes = JSON.parse(JSON.stringify(photo.annotations || []));
    let tool = "arrow";
    let color = "red";
    let draft = null;

    const dlg = document.createElement("div");
    dlg.className = "markup-editor";
    dlg.setAttribute("role", "dialog");
    dlg.setAttribute("aria-label", "Mark up photo");

    const stage = document.createElement("div");
    stage.className = "markup-stage";
    stage.style.aspectRatio = `${W} / ${H}`;
    stage.style.width = `min(100%, calc((100dvh - 210px) * ${W / H}))`;
    const img = document.createElement("img");
    img.src = src;
    img.alt = "Photo being marked up";
    const svgHost = document.createElement("div");
    svgHost.className = "markup-layer";
    stage.append(img, svgHost);

    const textInput = document.createElement("input");
    textInput.type = "text";
    textInput.value = label || "";
    textInput.maxLength = 60;
    textInput.placeholder = "Label text";
    textInput.className = "markup-text";

    function draw() {
      svgHost.replaceChildren(overlay(draft ? [...shapes, draft] : shapes, W, H));
      undoBtn.disabled = !shapes.length;
      clearBtn.disabled = !shapes.length;
      textInput.style.display = tool === "text" ? "" : "none";
      toolBtns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.tool === tool)));
      colorBtns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.color === color)));
    }

    function pos(e) {
      const r = svgHost.getBoundingClientRect();
      return { x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
        y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)) };
    }

    let start = null;
    svgHost.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const p = pos(e);
      if (tool === "text") {
        const t = textInput.value.trim();
        if (t) { shapes.push({ type: "text", x: p.x, y: p.y, text: t, color }); draw(); }
        return;
      }
      svgHost.setPointerCapture(e.pointerId);
      start = p;
    });
    svgHost.addEventListener("pointermove", (e) => {
      if (!start) return;
      const p = pos(e);
      draft = tool === "arrow" ? { type: "arrow", x1: start.x, y1: start.y, x2: p.x, y2: p.y, color }
        : { type: tool, x: start.x, y: start.y, w: p.x - start.x, h: p.y - start.y, color };
      draw();
    });
    const finish = () => {
      if (draft) {
        const big = draft.type === "arrow" ? Math.hypot(draft.x2 - draft.x1, draft.y2 - draft.y1) > 0.03
          : Math.abs(draft.w) > 0.02 && Math.abs(draft.h) > 0.02;
        if (big) {
          if (draft.type !== "arrow") {  // normalise to positive width/height
            if (draft.w < 0) { draft.x += draft.w; draft.w = -draft.w; }
            if (draft.h < 0) { draft.y += draft.h; draft.h = -draft.h; }
          }
          shapes.push(draft);
        }
      }
      draft = null;
      start = null;
      draw();
    };
    svgHost.addEventListener("pointerup", finish);
    svgHost.addEventListener("pointercancel", finish);

    const btn = (text, attrs, onclick) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = text;
      for (const [k, v] of Object.entries(attrs)) b.setAttribute(k, v);
      b.addEventListener("click", onclick);
      return b;
    };
    const toolBtns = [["arrow", "➜ Arrow"], ["ellipse", "◯ Circle"], ["rect", "▭ Box"], ["text", "T Text"]]
      .map(([t, txt]) => { const b = btn(txt, { "data-tool": t }, () => { tool = t; draw(); }); return b; });
    const colorBtns = Object.keys(COLORS).map((c) => {
      const b = btn("", { "data-color": c, "aria-label": c, class: "swatch-btn" }, () => { color = c; draw(); });
      b.style.background = COLORS[c];
      return b;
    });
    const undoBtn = btn("Undo", {}, () => { shapes.pop(); draw(); });
    const clearBtn = btn("Clear all", {}, () => { if (confirm("Remove all markup from this photo?")) { shapes = []; draw(); } });
    const close = (result) => { dlg.remove(); document.body.classList.remove("modal-open"); resolve(result); };

    const tools = document.createElement("div");
    tools.className = "markup-tools";
    tools.append(...toolBtns, ...colorBtns);
    const extra = document.createElement("div");
    extra.className = "markup-tools";
    extra.append(textInput, undoBtn, clearBtn);
    for (const o of others) {
      extra.append(btn(`Copy markup from ${o.name}`, {}, () => { shapes = JSON.parse(JSON.stringify(o.annotations)); draw(); }));
    }
    const actions = document.createElement("div");
    actions.className = "markup-actions";
    actions.append(btn("Cancel", {}, () => close(null)), btn("Save markup", { class: "primary" }, () => close(shapes)));

    const help = document.createElement("p");
    help.className = "small";
    help.textContent = "Drag on the photo to draw. Text: type the label, then tap where it goes.";
    dlg.append(tools, extra, help, stage, actions);
    document.body.append(dlg);
    document.body.classList.add("modal-open");
    draw();
  });
}
