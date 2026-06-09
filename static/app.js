import * as pdfjsLib from "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.7.76/build/pdf.min.mjs";

pdfjsLib.GlobalWorkerOptions.workerSrc =
  "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.7.76/build/pdf.worker.min.mjs";

const state = {
  pdfs: [],
  currentId: null,
  pdfDoc: null,
  extract: null,
  pageNum: 1,
  scale: 1.25,
  enabledTypes: new Set(),
  typeColors: new Map(),
  pollTimers: new Map(),
  showCharBounds: false,
  hideContainers: true,
  backend: "adobe",  // "adobe" | "doclayout" | "mineru"
  extractByBackend: {},  // pdfId -> { adobe, doclayout, mineru }
};

const els = {
  list: document.getElementById("pdf-list"),
  fileInput: document.getElementById("file-input"),
  canvas: document.getElementById("pdf-canvas"),
  stage: document.getElementById("canvas-stage"),
  bboxLayer: document.getElementById("bbox-layer"),
  prevBtn: document.getElementById("prev-page"),
  nextBtn: document.getElementById("next-page"),
  pageInfo: document.getElementById("page-info"),
  zoomIn: document.getElementById("zoom-in"),
  zoomOut: document.getElementById("zoom-out"),
  zoomInfo: document.getElementById("zoom-info"),
  docName: document.getElementById("doc-name"),
  docStatus: document.getElementById("doc-status"),
  typeList: document.getElementById("type-list"),
  filterAll: document.getElementById("filter-all"),
  filterNone: document.getElementById("filter-none"),
  hoverInfo: document.getElementById("hover-info"),
  empty: document.getElementById("empty-state"),
  charToggle: document.getElementById("char-bounds-toggle"),
  hideContainers: document.getElementById("hide-containers-toggle"),
  backend: document.getElementById("backend"),
  runDoclayout: document.getElementById("run-doclayout"),
  runMineru: document.getElementById("run-mineru"),
  runDatalab: document.getElementById("run-datalab"),
  runLlamaparse: document.getElementById("run-llamaparse"),
  runUpstage: document.getElementById("run-upstage"),
  runGlm: document.getElementById("run-glm"),
};

const ctx = els.canvas.getContext("2d");

// ---------- Color palette for element types ----------
const PALETTE = [
  "#ef4444", "#3b82f6", "#10b981", "#f59e0b", "#8b5cf6", "#ec4899",
  "#06b6d4", "#84cc16", "#f97316", "#a855f7", "#14b8a6", "#eab308",
  "#6366f1", "#22c55e", "#dc2626", "#0891b2",
];

function colorForType(t) {
  if (!state.typeColors.has(t)) {
    state.typeColors.set(t, PALETTE[state.typeColors.size % PALETTE.length]);
  }
  return state.typeColors.get(t);
}

// Adobe Path looks like "//Document/H1", "//Document/P[2]", "//Document/Table[1]/TR/TD"
function typeFromPath(path) {
  if (!path) return "Unknown";
  const seg = path.split("/").filter(Boolean).pop() || "Unknown";
  return seg.replace(/\[\d+\]$/, "");
}

// ---------- PDF list ----------
async function refreshList() {
  const resp = await fetch("/api/pdfs");
  state.pdfs = await resp.json();
  renderList();
  // Restart polling for any still-processing items
  for (const p of state.pdfs) {
    if (p.status === "processing") pollStatus(p.id);
  }
}

function renderList() {
  els.list.innerHTML = "";
  for (const pdf of state.pdfs) {
    const li = document.createElement("li");
    if (pdf.id === state.currentId) li.classList.add("active");
    li.title = pdf.error || pdf.filename;
    li.innerHTML = `
      <span class="name">${escapeHtml(pdf.filename)}</span>
      <span class="status ${pdf.status}">${pdf.status}</span>
      <span class="del" title="Delete">×</span>
    `;
    li.querySelector(".name").addEventListener("click", () => selectPdf(pdf.id));
    li.querySelector(".status").addEventListener("click", () => selectPdf(pdf.id));
    li.querySelector(".del").addEventListener("click", (e) => {
      e.stopPropagation();
      deletePdf(pdf.id);
    });
    els.list.appendChild(li);
  }
}

async function deletePdf(id) {
  if (!confirm("Delete this PDF?")) return;
  await fetch(`/api/pdfs/${id}`, { method: "DELETE" });
  if (state.currentId === id) {
    state.currentId = null;
    state.pdfDoc = null;
    state.extract = null;
    document.body.classList.remove("has-pdf");
    els.bboxLayer.innerHTML = "";
    ctx.clearRect(0, 0, els.canvas.width, els.canvas.height);
    els.docName.textContent = "";
    els.docStatus.textContent = "";
    els.pageInfo.textContent = "— / —";
    els.typeList.innerHTML = "";
  }
  refreshList();
}

function pollStatus(id) {
  if (state.pollTimers.has(id)) return;
  const tick = async () => {
    try {
      const r = await fetch(`/api/pdfs/${id}/status`);
      if (!r.ok) return stop();
      const s = await r.json();
      const found = state.pdfs.find((p) => p.id === id);
      if (found) {
        found.status = s.status;
        found.error = s.error;
        renderList();
      }
      if (s.status === "done") {
        stop();
        if (id === state.currentId) loadExtract(id);
      } else if (s.status === "failed") {
        stop();
        if (id === state.currentId) {
          els.docStatus.textContent = `extract failed: ${s.error || ""}`;
        }
      }
    } catch {
      stop();
    }
  };
  const stop = () => {
    const t = state.pollTimers.get(id);
    if (t) clearInterval(t);
    state.pollTimers.delete(id);
  };
  const timer = setInterval(tick, 2500);
  state.pollTimers.set(id, timer);
  tick();
}

// ---------- Upload ----------
els.fileInput.addEventListener("change", async (e) => {
  const files = Array.from(e.target.files || []);
  for (const f of files) {
    const fd = new FormData();
    fd.append("file", f);
    const resp = await fetch("/api/pdfs", { method: "POST", body: fd });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(`Upload failed: ${err.error || resp.status}`);
      continue;
    }
    const created = await resp.json();
    await refreshList();
    if (!state.currentId) selectPdf(created.id);
    pollStatus(created.id);
  }
  els.fileInput.value = "";
});

// ---------- PDF selection ----------
async function selectPdf(id) {
  state.currentId = id;
  state.pdfDoc = null;
  state.extract = null;
  state.pageNum = 1;
  state.enabledTypes = new Set();
  state.typeColors = new Map();
  els.bboxLayer.innerHTML = "";
  els.typeList.innerHTML = "";
  els.hoverInfo.innerHTML = "";
  renderList();

  const meta = state.pdfs.find((p) => p.id === id);
  els.docName.textContent = meta ? meta.filename : "";
  els.docStatus.textContent = "loading…";

  state.pdfDoc = await pdfjsLib.getDocument(`/api/pdfs/${id}/file`).promise;
  document.body.classList.add("has-pdf");
  await renderPage();

  if (meta && meta.status === "done") {
    loadExtract(id);
  } else if (meta && meta.status === "processing") {
    els.docStatus.textContent = "extracting…";
    pollStatus(id);
  } else if (meta && meta.status === "failed") {
    els.docStatus.textContent = `extract failed: ${meta.error || ""}`;
  }
}

async function loadExtract(id) {
  const backendPaths = { adobe: "extract", doclayout: "doclayout", mineru: "mineru", datalab: "datalab", llamaparse: "llamaparse", upstage: "upstage", glm: "glm" };
  const path = backendPaths[state.backend] || "extract";
  const r = await fetch(`/api/pdfs/${id}/${path}`);
  if (!r.ok) {
    state.extract = null;
    els.bboxLayer.innerHTML = "";
    els.typeList.innerHTML = "";
    const hints = {
      adobe: "no Adobe extract available",
      doclayout: "no DocLayout results yet — click 'Run DocLayout'",
      mineru: "no MinerU results yet — click 'Run MinerU'",
      datalab: "no Datalab results yet — click 'Run Datalab'",
      llamaparse: "no LlamaParse results yet — click 'Run LlamaParse'",
      upstage: "no Upstage results yet — click 'Run Upstage'",
      glm: "no GLM results yet — click 'Run GLM'",
    };
    els.docStatus.textContent = hints[state.backend] || "no results";
    return;
  }
  state.extract = await r.json();
  state.extractByBackend[id] = state.extractByBackend[id] || {};
  state.extractByBackend[id][state.backend] = state.extract;
  buildTypeIndex();
  els.docStatus.textContent = `${(state.extract.elements || []).length} elements (${state.backend})`;
  renderBoxes();
}

// Container/structural element types that visually wrap their children.
// Hidden by default to avoid the "giant box over a list" effect.
const CONTAINER_TYPES = new Set([
  "Document", "Sect", "Aside", "L", "Lbl", "LBody", "Li",
  "TOC", "TOCI", "Table", "TR",
]);

function buildTypeIndex() {
  const counts = new Map();
  for (const el of state.extract.elements || []) {
    const t = typeFromPath(el.Path);
    counts.set(t, (counts.get(t) || 0) + 1);
  }
  // Enable leaf types by default; hide containers.
  state.enabledTypes = new Set(
    [...counts.keys()].filter((t) => !CONTAINER_TYPES.has(t))
  );
  renderTypeList(counts);
}

function renderTypeList(counts) {
  els.typeList.innerHTML = "";
  const types = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  for (const [t, c] of types) {
    const li = document.createElement("li");
    const color = colorForType(t);
    li.innerHTML = `
      <input type="checkbox" ${state.enabledTypes.has(t) ? "checked" : ""} />
      <span class="swatch" style="background:${color}"></span>
      <span class="label">${escapeHtml(t)}</span>
      <span class="count">${c}</span>
    `;
    const cb = li.querySelector("input");
    cb.addEventListener("change", () => {
      if (cb.checked) state.enabledTypes.add(t);
      else state.enabledTypes.delete(t);
      renderBoxes();
    });
    li.addEventListener("click", (e) => {
      if (e.target === cb) return;
      cb.checked = !cb.checked;
      cb.dispatchEvent(new Event("change"));
    });
    els.typeList.appendChild(li);
  }
}

els.filterAll.addEventListener("click", () => {
  els.typeList.querySelectorAll("input").forEach((cb) => {
    if (!cb.checked) { cb.checked = true; cb.dispatchEvent(new Event("change")); }
  });
});
els.filterNone.addEventListener("click", () => {
  els.typeList.querySelectorAll("input").forEach((cb) => {
    if (cb.checked) { cb.checked = false; cb.dispatchEvent(new Event("change")); }
  });
});
els.charToggle.addEventListener("change", () => {
  state.showCharBounds = els.charToggle.checked;
  renderBoxes();
});
els.hideContainers.addEventListener("change", () => {
  state.hideContainers = els.hideContainers.checked;
  renderBoxes();
});

els.backend.addEventListener("change", () => {
  state.backend = els.backend.value;
  if (state.currentId) loadExtract(state.currentId);
});

els.runDoclayout.addEventListener("click", async () => {
  if (!state.currentId) return alert("Pick a PDF first");
  els.runDoclayout.disabled = true;
  els.docStatus.textContent = "DocLayout running…";
  try {
    const r = await fetch(`/api/pdfs/${state.currentId}/doclayout`, { method: "POST" });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.error || `HTTP ${r.status}`);
    }
    pollDoclayout(state.currentId);
  } catch (e) {
    els.docStatus.textContent = `DocLayout failed: ${e.message}`;
    els.runDoclayout.disabled = false;
  }
});

els.runMineru.addEventListener("click", async () => {
  if (!state.currentId) return alert("Pick a PDF first");
  els.runMineru.disabled = true;
  els.docStatus.textContent = "MinerU running… (cloud processing, ~30s+)";
  try {
    const r = await fetch(`/api/pdfs/${state.currentId}/mineru`, { method: "POST" });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.error || `HTTP ${r.status}`);
    }
    pollBackend(state.currentId, "mineru");
  } catch (e) {
    els.docStatus.textContent = `MinerU failed: ${e.message}`;
    els.runMineru.disabled = false;
  }
});

els.runDatalab.addEventListener("click", async () => {
  if (!state.currentId) return alert("Pick a PDF first");
  els.runDatalab.disabled = true;
  els.docStatus.textContent = "Datalab running… (cloud processing, ~30s+)";
  try {
    const r = await fetch(`/api/pdfs/${state.currentId}/datalab`, { method: "POST" });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.error || `HTTP ${r.status}`);
    }
    pollBackend(state.currentId, "datalab");
  } catch (e) {
    els.docStatus.textContent = `Datalab failed: ${e.message}`;
    els.runDatalab.disabled = false;
  }
});

els.runLlamaparse.addEventListener("click", async () => {
  if (!state.currentId) return alert("Pick a PDF first");
  els.runLlamaparse.disabled = true;
  els.docStatus.textContent = "LlamaParse running… (cloud processing, ~30s+)";
  try {
    const r = await fetch(`/api/pdfs/${state.currentId}/llamaparse`, { method: "POST" });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.error || `HTTP ${r.status}`);
    }
    pollBackend(state.currentId, "llamaparse");
  } catch (e) {
    els.docStatus.textContent = `LlamaParse failed: ${e.message}`;
    els.runLlamaparse.disabled = false;
  }
});

els.runUpstage.addEventListener("click", async () => {
  if (!state.currentId) return alert("Pick a PDF first");
  els.runUpstage.disabled = true;
  els.docStatus.textContent = "Upstage running… (cloud processing, ~30s+)";
  try {
    const r = await fetch(`/api/pdfs/${state.currentId}/upstage`, { method: "POST" });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.error || `HTTP ${r.status}`);
    }
    pollBackend(state.currentId, "upstage");
  } catch (e) {
    els.docStatus.textContent = `Upstage failed: ${e.message}`;
    els.runUpstage.disabled = false;
  }
});

els.runGlm.addEventListener("click", async () => {
  if (!state.currentId) return alert("Pick a PDF first");
  els.runGlm.disabled = true;
  els.docStatus.textContent = "GLM running… (per-page LLM call, slow)";
  try {
    const r = await fetch(`/api/pdfs/${state.currentId}/glm`, { method: "POST" });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.error || `HTTP ${r.status}`);
    }
    pollBackend(state.currentId, "glm");
  } catch (e) {
    els.docStatus.textContent = `GLM failed: ${e.message}`;
    els.runGlm.disabled = false;
  }
});

function pollBackend(id, backend) {
  const key = `${backend}:${id}`;
  if (state.pollTimers.has(key)) return;
  const btnMap = { mineru: els.runMineru, doclayout: els.runDoclayout, datalab: els.runDatalab, llamaparse: els.runLlamaparse, upstage: els.runUpstage, glm: els.runGlm };
  const btn = btnMap[backend] || els.runDoclayout;
  const tick = async () => {
    try {
      const r = await fetch(`/api/pdfs/${id}/${backend}/status`);
      if (!r.ok) return stop();
      const s = await r.json();
      if (s.status === "done") {
        stop();
        btn.disabled = false;
        if (id === state.currentId) {
          state.backend = backend;
          els.backend.value = backend;
          loadExtract(id);
        }
      } else if (s.status === "failed") {
        stop();
        btn.disabled = false;
        if (id === state.currentId) {
          els.docStatus.textContent = `${backend} failed: ${s.error || ""}`;
        }
      }
    } catch {
      stop();
    }
  };
  const stop = () => {
    const t = state.pollTimers.get(key);
    if (t) clearInterval(t);
    state.pollTimers.delete(key);
  };
  const timer = setInterval(tick, 2500);
  state.pollTimers.set(key, timer);
  tick();
}

function pollDoclayout(id) {
  const key = `doclayout:${id}`;
  if (state.pollTimers.has(key)) return;
  const tick = async () => {
    try {
      const r = await fetch(`/api/pdfs/${id}/doclayout/status`);
      if (!r.ok) return stop();
      const s = await r.json();
      if (s.status === "done") {
        stop();
        els.runDoclayout.disabled = false;
        if (id === state.currentId) {
          // Auto-switch to doclayout view when it finishes
          state.backend = "doclayout";
          els.backend.value = "doclayout";
          loadExtract(id);
        }
      } else if (s.status === "failed") {
        stop();
        els.runDoclayout.disabled = false;
        if (id === state.currentId) {
          els.docStatus.textContent = `DocLayout failed: ${s.error || ""}`;
        }
      }
    } catch {
      stop();
    }
  };
  const stop = () => {
    const t = state.pollTimers.get(key);
    if (t) clearInterval(t);
    state.pollTimers.delete(key);
  };
  const timer = setInterval(tick, 2500);
  state.pollTimers.set(key, timer);
  tick();
}

// ---------- Render PDF page ----------
async function renderPage() {
  if (!state.pdfDoc) return;
  const page = await state.pdfDoc.getPage(state.pageNum);
  const viewport = page.getViewport({ scale: state.scale });
  const dpr = window.devicePixelRatio || 1;
  els.canvas.width = Math.floor(viewport.width * dpr);
  els.canvas.height = Math.floor(viewport.height * dpr);
  els.canvas.style.width = `${viewport.width}px`;
  els.canvas.style.height = `${viewport.height}px`;
  els.stage.style.width = `${viewport.width}px`;
  els.stage.style.height = `${viewport.height}px`;
  els.bboxLayer.style.width = `${viewport.width}px`;
  els.bboxLayer.style.height = `${viewport.height}px`;
  const transform = dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null;
  await page.render({ canvasContext: ctx, viewport, transform }).promise;

  els.pageInfo.textContent = `${state.pageNum} / ${state.pdfDoc.numPages}`;
  els.zoomInfo.textContent = `${Math.round(state.scale * 100)}%`;
  els.prevBtn.disabled = state.pageNum <= 1;
  els.nextBtn.disabled = state.pageNum >= state.pdfDoc.numPages;
  renderBoxes();
}

// ---------- BBox overlay ----------
function isContainer(a, others, padding = 1.0) {
  // a contains b if a's box fully encloses b's (with small padding tolerance)
  // Returns true if a contains at least 2 other elements on the same page.
  const [ax0, ay0, ax1, ay1] = a.Bounds;
  let n = 0;
  for (const b of others) {
    if (b === a || b.Page !== a.Page || !b.Bounds) continue;
    const [bx0, by0, bx1, by1] = b.Bounds;
    if (bx0 >= ax0 - padding && by0 >= ay0 - padding &&
        bx1 <= ax1 + padding && by1 <= ay1 + padding) {
      // exclude near-identical boxes (within ~2pt all sides)
      if (Math.abs(bx0 - ax0) + Math.abs(by0 - ay0) +
          Math.abs(bx1 - ax1) + Math.abs(by1 - ay1) > 8) {
        n++;
        if (n >= 2) return true;
      }
    }
  }
  return false;
}

function renderBoxes() {
  els.bboxLayer.innerHTML = "";
  if (!state.extract) return;
  const pageIdx = state.pageNum - 1;
  const pageMeta = (state.extract.pages || [])[pageIdx];
  if (!pageMeta) return;
  const pageHeightPts = pageMeta.height;
  const pageWidthPts = pageMeta.width;
  // CSS px per PDF point at current zoom
  const viewportWidth = parseFloat(els.bboxLayer.style.width);
  const ptToPx = viewportWidth / pageWidthPts;

  const allElements = state.extract.elements || [];
  const pageElements = allElements.filter((e) => e.Page === pageIdx && e.Bounds);
  const containerSet = state.hideContainers
    ? new Set(pageElements.filter((e) => isContainer(e, pageElements)))
    : new Set();

  for (const el of allElements) {
    if (el.Page !== pageIdx) continue;
    const bounds = el.Bounds;
    if (!bounds || bounds.length !== 4) continue;
    const type = typeFromPath(el.Path);
    if (!state.enabledTypes.has(type)) continue;
    if (containerSet.has(el)) continue;

    const [x0, y0, x1, y1] = bounds; // PDF coords, bottom-left origin
    const left = x0 * ptToPx;
    const top = (pageHeightPts - y1) * ptToPx;
    const width = (x1 - x0) * ptToPx;
    const height = (y1 - y0) * ptToPx;

    const div = document.createElement("div");
    div.className = "bbox";
    div.style.left = `${left}px`;
    div.style.top = `${top}px`;
    div.style.width = `${width}px`;
    div.style.height = `${height}px`;
    const color = colorForType(type);
    div.style.borderColor = color;
    div.dataset.type = type;
    div.dataset.path = el.Path || "";
    div.dataset.text = el.Text || "";
    div.addEventListener("mouseenter", () => showInfo(el, type));
    els.bboxLayer.appendChild(div);

    if (state.showCharBounds && Array.isArray(el.CharBounds)) {
      for (const cb of el.CharBounds) {
        if (!cb || cb.length !== 4) continue;
        const [cx0, cy0, cx1, cy1] = cb;
        const c = document.createElement("div");
        c.className = "charbox";
        c.style.left = `${cx0 * ptToPx}px`;
        c.style.top = `${(pageHeightPts - cy1) * ptToPx}px`;
        c.style.width = `${(cx1 - cx0) * ptToPx}px`;
        c.style.height = `${(cy1 - cy0) * ptToPx}px`;
        els.bboxLayer.appendChild(c);
      }
    }
  }
}

function showInfo(el, type) {
  els.hoverInfo.innerHTML = `
    <div class="info-type">${escapeHtml(type)}</div>
    <div class="info-path">${escapeHtml(el.Path || "")}</div>
    <div class="info-text">${escapeHtml(el.Text || "(no text)")}</div>
  `;
}

// ---------- Navigation ----------
els.prevBtn.addEventListener("click", () => { if (state.pageNum > 1) { state.pageNum--; renderPage(); } });
els.nextBtn.addEventListener("click", () => {
  if (state.pdfDoc && state.pageNum < state.pdfDoc.numPages) { state.pageNum++; renderPage(); }
});
els.zoomIn.addEventListener("click", () => { state.scale = Math.min(4, state.scale * 1.2); renderPage(); });
els.zoomOut.addEventListener("click", () => { state.scale = Math.max(0.25, state.scale / 1.2); renderPage(); });

document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") return;
  if (e.key === "ArrowLeft") els.prevBtn.click();
  else if (e.key === "ArrowRight") els.nextBtn.click();
  else if (e.key === "+" || e.key === "=") els.zoomIn.click();
  else if (e.key === "-") els.zoomOut.click();
});

// ---------- Utilities ----------
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// ---------- Init ----------
refreshList();
