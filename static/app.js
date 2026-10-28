// OpenDAG frontend: plain JS, no build step.
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const slug = (s) => String(s).toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 40) || "node";

if (window.cytoscapeDagre) cytoscape.use(cytoscapeDagre);

const state = { rec: null, cy: null, selected: null, status: {}, config: {} };

// GSAP motion for page elements (never the graph canvas). Skipped if GSAP failed to load
// or the viewer prefers reduced motion; clearProps hands styling back to the CSS afterwards.
const MOTION = !!window.gsap && !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
function enter(targets, opts = {}) {
  if (!MOTION) return;
  const els = gsap.utils.toArray(targets).filter((el) => !el.hidden && el.offsetParent !== null);
  if (els.length) gsap.from(els, { y: 14, opacity: 0, duration: 0.45, stagger: 0.06, ease: "power2.out", clearProps: "transform,opacity", ...opts });
}

async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  } catch {
    throw new Error("Can't reach the OpenDAG server. Check your connection, then refresh the page.");
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
  return body;
}

// ---------- routing ----------
function show(view) {
  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== `view-${view}`));
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === view));
}

async function route() {
  $("#progress").hidden = true;
  if (net.graph) net.graph.pauseAnimation();  // stop rendering the 3D view when it's not on screen
  stopNetLabels();
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  try {
    if (parts[0] === "record" && parts[1]) {
      show("record");
      await loadRecord(parts[1]);
    } else if (parts[0] === "add") {
      show("home");
      enter(".hero > *", { stagger: 0.07 });
      await loadRecent();
    } else if (parts[0] === "network") {
      show("network");
      await loadNetwork();
    } else if (parts[0] === "about") {
      show("about");
      enter("#view-about > *", { stagger: 0.05 });
    } else {  // default page: the dataset ("#/" or the old "#/dataset")
      show("dataset");
      await loadDataset();
    }
  } catch (e) {
    show("home");  // errors are shown under the add-paper form
    showError(e.message);
  }
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);

// ---------- home / submit ----------
function showError(msg) {
  const el = $("#submit-error");
  el.textContent = msg;
  el.hidden = !msg;
}

function setProgress(step) {
  const order = ["fetching", "extracting", "critiquing", "evidence"];
  const idx = order.indexOf(step);
  document.querySelectorAll("#progress li").forEach((li) => {
    const i = order.indexOf(li.dataset.step);
    const cls = step === "done" || i < idx ? "done" : i === idx ? "active" : "";
    if (MOTION && cls !== li.className) {
      if (cls === "active") gsap.fromTo(li, { x: -6 }, { x: 0, duration: 0.35, ease: "back.out(2)", clearProps: "transform" });
      if (cls === "done") gsap.fromTo(li, { backgroundColor: "#dff3ea" }, { backgroundColor: "rgba(223,243,234,0)", duration: 0.9, clearProps: "backgroundColor" });
    }
    li.className = cls;
  });
}

$("#submit-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  showError("");
  const btn = ev.target.querySelector("button");
  btn.disabled = true;
  $("#progress").hidden = false;
  document.querySelectorAll("#progress li").forEach((li) => (li.className = ""));
  enter("#progress li", { x: -10, y: 0, stagger: 0.08 });
  setProgress("fetching");
  try {
    const r = await api("/api/submit", { method: "POST", body: JSON.stringify({ identifier: $("#identifier").value }) });
    if (r.record_id) {
      location.hash = `#/record/${r.record_id}`;
      return;
    }
    while (true) {
      await new Promise((ok) => setTimeout(ok, 1500));
      const job = await api(`/api/jobs/${r.job_id}`);
      setProgress(job.step);
      if (job.status === "done") { location.hash = `#/record/${job.record_id}`; break; }
      if (job.status === "error") throw new Error(job.error);
    }
  } catch (e) {
    showError(e.message);
    $("#progress").hidden = true;
  } finally {
    btn.disabled = false;
  }
});

function card(r) {
  return `<a class="card" href="#/record/${esc(r.id)}">
    <div class="eo">${esc(r.exposure)}<span class="arrow">→</span>${esc(r.outcome)}</div>
    <div class="small muted">${esc(r.design)} · ${esc(r.journal || "")} ${esc(r.year || "")}</div>
    <div class="small">${esc((r.title || "").slice(0, 110))}</div>
    <div class="small muted">${r.n_nodes} variables · ${r.n_missing} AI-suggested · <span class="badge ${esc(r.status)}">${esc(r.status)}</span></div>
  </a>`;
}

async function loadRecent() {
  const rows = await api("/api/dataset?include_drafts=true");
  $("#recent-list").innerHTML = rows.slice(0, 6).map(card).join("") || `<p class="muted">Nothing yet. Be the first to add a paper.</p>`;
  enter("#recent-list .card");
}

// ---------- record view ----------
function suggestionFor(nodeId) {
  return (state.rec.critique.missing_confounders || []).find((m) => slug(m.label) === nodeId);
}

function nodeClasses(n) {
  const c = [n.role];
  if (n.role === "confounder" || n.role === "other" || n.role === "collider" || n.role === "effect_modifier") {
    c.push(n.adjusted ? "adjusted" : "unadjusted");
  }
  if (n.ai_suggested) {
    const s = suggestionFor(n.id);
    c.push(s && state.status[s.label] === "accepted" ? "ai-accepted" : "ai");
  }
  return c.join(" ");
}

function visibleDag() {
  const rejected = new Set(
    (state.rec.critique.missing_confounders || []).filter((m) => state.status[m.label] === "rejected").map((m) => slug(m.label))
  );
  const nodes = state.rec.dag.nodes.filter((n) => !(n.ai_suggested && rejected.has(n.id)));
  const ids = new Set(nodes.map((n) => n.id));
  const edges = state.rec.dag.edges.filter((e) => ids.has(e.source) && ids.has(e.target));
  return { nodes, edges };
}

function drawGraph() {
  const { nodes, edges } = visibleDag();
  const exp = nodes.find((n) => n.role === "exposure");
  const elements = [
    ...nodes.map((n) => ({ data: { id: n.id, label: n.label }, classes: nodeClasses(n) })),
    ...edges.map((e, i) => {
      const main = exp && e.source === exp.id && nodes.find((n) => n.id === e.target)?.role === "outcome";
      return { data: { id: `e${i}_${e.source}_${e.target}`, source: e.source, target: e.target },
               classes: [e.ai_suggested ? "ai" : "", main ? "main" : ""].join(" ") };
    }),
  ];
  if (state.cy) state.cy.destroy();
  state.cy = cytoscape({
    container: $("#cy"),
    elements,
    wheelSensitivity: 0.3,
    style: [
      { selector: "node", style: {
          label: "data(label)", "text-wrap": "wrap", "text-max-width": 120, "font-size": 11,
          "text-valign": "center", "text-halign": "center", color: "#1d2327",
          shape: "round-rectangle", width: "label", height: "label", padding: 10,
          "background-color": "#fff", "border-width": 2, "border-color": "#9aa7b1" } },
      { selector: "node.exposure", style: { "background-color": "#1b8a6b", "border-color": "#1b8a6b", color: "#fff", "font-weight": 700 } },
      { selector: "node.outcome", style: { "background-color": "#c4462f", "border-color": "#c4462f", color: "#fff", "font-weight": 700 } },
      { selector: "node.adjusted", style: { shape: "rectangle", "background-color": "#2f5d8a", "border-color": "#1b3a59", "border-width": 3, color: "#fff" } },
      { selector: "node.mediator", style: { "background-color": "#efe6f6", "border-color": "#7b4fa0" } },
      { selector: "node.ai", style: { "border-style": "dashed", "border-color": "#d08a00", "border-width": 3, "background-color": "#fffaf0" } },
      { selector: "node.ai-accepted", style: { "border-color": "#d08a00", "border-width": 3 } },
      { selector: "edge", style: {
          width: 1.6, "line-color": "#a6b0b7", "target-arrow-color": "#a6b0b7",
          "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": 1.1 } },
      { selector: "edge.main", style: { width: 3, "line-color": "#5d6970", "target-arrow-color": "#5d6970" } },
      { selector: "edge.ai", style: { "line-style": "dashed", "line-color": "#e0b04a", "target-arrow-color": "#e0b04a" } },
      { selector: ":selected", style: { "overlay-color": "#2f5d8a", "overlay-opacity": 0.18, "overlay-padding": 6 } },
    ],
    layout: window.cytoscapeDagre
      ? { name: "dagre", rankDir: "LR", nodeSep: 25, rankSep: 90, padding: 20 }
      : { name: "breadthfirst", directed: true, padding: 20 },
  });
  state.cy.on("tap", "node", (ev) => selectNode(ev.target.id()));
  state.cy.on("tap", "edge", (ev) => select({ type: "edge", source: ev.target.data("source"), target: ev.target.data("target") }));
  state.cy.on("tap", (ev) => { if (ev.target === state.cy) select(null); });
  fillEdgeSelects(nodes);
}

function select(sel) {
  state.selected = sel;
  const editable = state.rec.status !== "approved";
  $("#delete-selected").disabled = !sel || !editable;
  $("#toggle-adjusted").disabled = !sel || sel.type !== "node" || !editable;
  if (!sel || sel.type !== "node") $("#node-detail").hidden = true;
}

function selectNode(id) {
  select({ type: "node", id });
  const n = state.rec.dag.nodes.find((x) => x.id === id);
  if (!n) return;
  const s = n.ai_suggested ? suggestionFor(id) : null;
  let html = `<h3>${esc(n.label)}</h3><dl>
    <dt>Role</dt><dd>${esc(n.role.replace("_", " "))}</dd>
    <dt>Adjusted</dt><dd>${n.adjusted ? "yes" : "no"}</dd>
    <dt>Source</dt><dd>${n.ai_suggested ? '<span class="tag">AI-suggested</span>' : "reported in paper"}</dd></dl>`;
  if (n.snippet) html += `<p class="small"><b>From the paper:</b> “${esc(n.snippet)}”</p>`;
  if (s) {
    html += `<p class="small"><b>Rationale:</b> ${esc(s.rationale)}</p>`;
    if (s.evidence?.length) html += `<p class="small"><b>Evidence (Tavily):</b></p><ul class="small">${s.evidence.map((e) => `<li><a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.title || e.url)}</a></li>`).join("")}</ul>`;
  }
  $("#node-detail").innerHTML = html;
  $("#node-detail").hidden = false;
  $(".side-scroll").scrollTo({ top: 0, behavior: "smooth" });
}

function fillEdgeSelects(nodes) {
  const opts = nodes.map((n) => `<option value="${esc(n.id)}">${esc(n.label)}</option>`).join("");
  $("#edge-src").innerHTML = opts;
  $("#edge-tgt").innerHTML = opts;
}

function renderSuggestions() {
  const list = state.rec.critique.missing_confounders || [];
  const editable = state.rec.status !== "approved";
  $("#suggestions").innerHTML = list.length ? list.map((m, i) => {
    const st = state.status[m.label] || "suggested";
    return `<div class="sugg ${st}" data-i="${i}">
      <div class="head"><span class="label">${esc(m.label)}</span>
        ${editable ? `<span><button class="mini secondary" data-acc="${i}">Accept</button>
        <button class="mini secondary danger" data-rej="${i}">Reject</button></span>` : ""}</div>
      <div class="small">${highlight(m.rationale, [state.rec.study.exposure.name, state.rec.study.outcome.name])}</div>
      ${m.evidence?.length ? `<ul>${m.evidence.map((e) => `<li><a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.title || e.url)}</a></li>`).join("")}</ul>` : `<div class="small muted">No evidence links found.</div>`}
    </div>`;
  }).join("") : `<p class="small muted">None suggested.</p>`;
  document.querySelectorAll("[data-acc]").forEach((b) => b.onclick = () => setSugg(list[b.dataset.acc].label, "accepted"));
  document.querySelectorAll("[data-rej]").forEach((b) => b.onclick = () => setSugg(list[b.dataset.rej].label, "rejected"));
}

function setSugg(label, st) {
  state.status[label] = state.status[label] === st ? "suggested" : st;
  renderSuggestions();
  renderKeyFacts(state.rec);
  drawGraph();
  dirty();
  const i = (state.rec.critique.missing_confounders || []).findIndex((m) => m.label === label);
  const card = document.querySelector(`.sugg[data-i="${i}"]`);
  if (!MOTION || !card) return;
  if (state.status[label] === "accepted") {
    gsap.fromTo(card, { backgroundColor: "#dff3ea" }, { backgroundColor: "rgba(223,243,234,0)", duration: 1, clearProps: "backgroundColor" });
  } else if (state.status[label] === "rejected") {
    gsap.fromTo(card, { x: 8 }, { x: 0, duration: 0.5, ease: "elastic.out(1, 0.4)", clearProps: "transform" });
  }
}

function dirty() { $("#edit-msg").textContent = "Unsaved changes."; }

// ---------- key figures ----------
// Wrap numbers (and optional key terms) in <mark>. Splits before escaping so entities stay intact.
function highlight(text, terms = []) {
  const words = terms.filter((t) => t && t.length > 2).map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const num = String.raw`\d(?:[\d,]*\d)?(?:\.\d+)?(?:\s?(?:%|years?|months?|weeks?|days?))?`;
  const re = new RegExp(`(${words.length ? words.join("|") + "|" : ""}${num})`, "gi");
  return String(text ?? "").split(re).map((part, i) => {
    if (i % 2 === 0) return esc(part);
    return /^\d/.test(part) ? `<mark class="num">${esc(part)}</mark>` : `<mark class="term">${esc(part)}</mark>`;
  }).join("");
}

const RATIO = /^(HR|OR|RR|IRR|aHR|aOR|aRR|SHR|PR)$/i;

function parseCI(ci) {
  const m = String(ci || "").match(/(-?\d+(?:\.\d+)?)\s*(?:-|–|—|,|to)\s*(-?\d+(?:\.\d+)?)/);
  return m ? [parseFloat(m[1]), parseFloat(m[2])] : null;
}

// One-row forest plot: estimate, 95% CI and the null line (1 for ratios, 0 for differences).
function forestPlot(eff) {
  const est = parseFloat(eff.value), ci = parseCI(eff.ci);
  if (!isFinite(est) || !ci) return "";
  const ratio = RATIO.test(eff.measure || "");
  const nul = ratio ? 1 : 0;
  const f = ratio ? Math.log : (x) => x;
  if (ratio && (est <= 0 || ci[0] <= 0)) return "";
  const lo = Math.min(ci[0], nul), hi = Math.max(ci[1], nul);
  const pad = (f(hi) - f(lo)) * 0.25 || 0.5;
  const a = f(lo) - pad, b = f(hi) + pad, W = 260, X = (v) => 10 + ((f(v) - a) / (b - a)) * (W - 20);
  const excludes = ci[0] > nul || ci[1] < nul;
  return `<svg class="forest" viewBox="0 0 ${W} 44" role="img" aria-label="95% CI ${ci[0]} to ${ci[1]}, null ${nul}">
      <title>${esc(eff.measure)} ${est} (95% CI ${ci[0]}–${ci[1]}); null = ${nul}</title>
      <line x1="${X(nul)}" x2="${X(nul)}" y1="4" y2="30" class="null"/>
      <text x="${X(nul)}" y="42" class="axis" text-anchor="middle">${nul}</text>
      <line x1="${X(ci[0])}" x2="${X(ci[1])}" y1="17" y2="17" class="ci"/>
      <line x1="${X(ci[0])}" x2="${X(ci[0])}" y1="12" y2="22" class="ci"/>
      <line x1="${X(ci[1])}" x2="${X(ci[1])}" y1="12" y2="22" class="ci"/>
      <rect x="${X(est) - 5}" y="12" width="10" height="10" rx="2" class="est"/>
    </svg>
    <div class="small ${excludes ? "sig" : "muted"}">${excludes ? "✓ 95% CI excludes the null" : "95% CI includes the null"}</div>`;
}

function tile(label, value, sub = "", cls = "") {
  return `<div class="tile ${cls}"><div class="t-label">${esc(label)}</div><div class="t-value">${value}</div>${sub ? `<div class="t-sub">${sub}</div>` : ""}</div>`;
}

function renderKeyFacts(r) {
  const s = r.study, eff = s.reported_effect;
  const nodes = r.dag.nodes;
  const adjusted = nodes.filter((n) => n.adjusted).length;
  const sugg = r.critique.missing_confounders || [];
  const acc = sugg.filter((m) => state.status[m.label] === "accepted").length;
  const rej = sugg.filter((m) => state.status[m.label] === "rejected").length;
  const risks = r.critique.bias_risks || [];
  const high = risks.filter((b) => b.severity === "high").length;
  const backdoors = (r.open_backdoors || []).length;

  $("#r-headline").innerHTML = `<span class="pill exposure">${esc(s.exposure.name)}</span><span class="arrow">→</span>
    <span class="pill outcome">${esc(s.outcome.name)}</span><span class="pill design">${esc(s.study_design)}</span>`;

  const hero = eff && eff.value
    ? `<div class="tile hero"><div class="t-label">Reported effect${eff.adjusted ? " (adjusted)" : ""}</div>
         <div class="t-hero"><span class="measure">${esc(eff.measure)}</span> <span id="eff-val">${esc(eff.value)}</span></div>
         ${eff.ci ? `<div class="t-sub">95% CI ${esc(eff.ci)}</div>` : ""}${forestPlot(eff)}</div>`
    : `<div class="tile hero"><div class="t-label">Reported effect</div><div class="t-value muted">Not extracted</div></div>`;

  $("#keyfacts").innerHTML = hero + `<div class="tiles">
    ${s.sample_size ? tile("Participants", esc(s.sample_size), s.follow_up ? `Follow-up: ${esc(s.follow_up)}` : "") : ""}
    ${tile("Adjusted for", `${adjusted}`, adjusted === 1 ? "variable" : "variables")}
    ${tile("Missing confounders", `${sugg.length}`, sugg.length ? `AI-suggested · ${acc} accepted · ${rej} rejected` : "AI-suggested", "ai")}
    ${tile("Bias risks", `${risks.length}`, high ? `<span class="sev high">▲ ${high} high</span>` : "none rated high", "ai")}
    ${tile("Open backdoor paths", `${backdoors}`, backdoors ? "unadjusted common causes" : "none found", backdoors ? "warn" : "")}
  </div>`;
}

function renderRecord({ entrance = false } = {}) {
  const r = state.rec;
  const p = r.paper, s = r.study;
  $("#r-title").textContent = p.title;
  $("#r-meta").innerHTML = `${esc(p.authors?.slice(0, 120))}${p.authors?.length > 120 ? "…" : ""}<br>
    <i>${esc(p.journal)}</i> ${esc(p.year || "")} ·
    ${p.doi ? `<a href="https://doi.org/${esc(p.doi)}" target="_blank" rel="noopener">doi:${esc(p.doi)}</a> · ` : ""}
    ${p.pmcid ? `<a href="https://europepmc.org/article/PMC/${esc(p.pmcid)}" target="_blank" rel="noopener">${esc(p.pmcid)}</a> · ` : ""}
    licence: ${esc(p.license || "n/a")}`;
  $("#r-status").textContent = r.status;
  $("#r-status").className = `badge ${r.status}`;
  renderKeyFacts(r);
  const keyTerms = [s.exposure.name, s.outcome.name];

  const eff = s.reported_effect;
  $("#study").innerHTML = `
    <dt>Design</dt><dd>${esc(s.study_design)}</dd>
    <dt>Population</dt><dd>${highlight(s.population)}</dd>
    ${s.sample_size ? `<dt>Participants</dt><dd>${highlight(s.sample_size)}</dd>` : ""}
    ${s.follow_up ? `<dt>Follow-up</dt><dd>${highlight(s.follow_up)}</dd>` : ""}
    <dt>Exposure</dt><dd><b>${esc(s.exposure.name)}</b><br><span class="small muted">${highlight(s.exposure.definition)}</span></dd>
    <dt>Outcome</dt><dd><b>${esc(s.outcome.name)}</b><br><span class="small muted">${highlight(s.outcome.definition)}</span></dd>
    <dt>Reported effect</dt><dd>${eff && eff.value ? `${esc(eff.measure)} <b>${esc(eff.value)}</b>${eff.ci ? ` (95% CI ${esc(eff.ci)})` : ""} ${eff.adjusted ? "<span class='small muted'>adjusted</span>" : ""}` : "<span class='muted'>not extracted</span>"}</dd>
    <dt>Adjusted for</dt><dd>${r.dag.nodes.filter((n) => n.adjusted).map((n) => esc(n.label)).join(", ") || "none listed"}</dd>`;

  $("#bias").innerHTML = (r.critique.bias_risks || []).map((b) =>
    `<div class="bias"><b>${esc(b.type)}</b><span class="sev ${esc(b.severity)}">${esc(b.severity)}</span><div class="small">${highlight(b.explanation, keyTerms)}</div></div>`
  ).join("") || `<p class="small muted">None flagged.</p>`;
  $("#assessment").innerHTML = highlight(r.critique.overall_assessment || "", keyTerms);
  $("#backdoors").innerHTML = r.open_backdoors?.length
    ? `<b>Open backdoor paths</b> via unadjusted common causes: ${r.open_backdoors.map((b) => `<mark class="term warn">${esc(b)}</mark>`).join(" ")}` : "";

  const issues = r.validation_issues || [];
  $("#issues").hidden = !issues.length;
  $("#issues").innerHTML = `<h3>Validation notes</h3><ul class="small">${issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`;

  $("#models").innerHTML = `Extraction: <code>${esc(r.models.extraction)}</code> · Critique: <code>${esc(r.models.critique)}</code> · via ${esc(r.models.provider)} · Evidence: Tavily`;
  $("#dagitty").textContent = r.dagitty;
  $("#dl-dagitty").href = `/api/records/${r.id}/dagitty`;
  $("#dl-dagitty").setAttribute("download", `${r.id}.dagitty`);
  $("#dl-json").href = `/api/records/${r.id}`;
  $("#dl-json").setAttribute("download", `${r.id}.json`);

  $("#editor").hidden = r.status === "approved";
  $("#edit-msg").textContent = "";
  renderSuggestions();
  drawGraph();
  select(null);
  if (entrance) {
    enter(".record-head > *", { stagger: 0.08 });
    enter("#keyfacts .tile", { delay: 0.05, stagger: 0.06 });
    enter("#view-record .side-scroll > .panel, #view-record .record-lower > .panel", { delay: 0.1, stagger: 0.07 });
    enter("#suggestions .sugg, #bias .bias", { delay: 0.45, stagger: 0.07, x: 10, y: 0 });
    countUp($("#eff-val"));
  }
}

// "HR 1.66" ticks up from 1.00 (ratio measures) or 0, keeping the paper's decimals.
function countUp(el) {
  if (!MOTION || !el) return;
  const text = el.textContent.trim();
  const target = parseFloat(text);
  if (!/^-?\d+(\.\d+)?$/.test(text)) return;  // only plain numbers like "1.66"
  const decimals = (text.split(".")[1] || "").length;
  const ratio = /HR|OR|RR|IRR/i.test(state.rec.study.reported_effect?.measure || "");
  const obj = { v: ratio ? 1 : 0 };
  gsap.to(obj, { v: target, duration: 1.1, delay: 0.3, ease: "power2.out",
    onUpdate: () => (el.textContent = obj.v.toFixed(decimals)), onComplete: () => (el.textContent = text) });
}

async function loadRecord(id) {
  state.rec = await api(`/api/records/${id}`);
  state.status = {};
  (state.rec.critique.missing_confounders || []).forEach((m) => (state.status[m.label] = m.status || "suggested"));
  renderRecord({ entrance: true });
}

// ---------- editing ----------
$("#add-node").onclick = () => {
  const label = $("#new-label").value.trim();
  if (!label) return;
  const id = slug(label);
  if (state.rec.dag.nodes.some((n) => n.id === id)) { $("#edit-msg").textContent = "That variable already exists."; return; }
  state.rec.dag.nodes.push({ id, label, role: $("#new-role").value, adjusted: $("#new-adjusted").checked, snippet: "", ai_suggested: false });
  const exp = state.rec.dag.nodes.find((n) => n.role === "exposure");
  const out = state.rec.dag.nodes.find((n) => n.role === "outcome");
  const role = $("#new-role").value;
  if (role === "confounder" && exp && out) state.rec.dag.edges.push({ source: id, target: exp.id }, { source: id, target: out.id });
  if (role === "mediator" && exp && out) state.rec.dag.edges.push({ source: exp.id, target: id }, { source: id, target: out.id });
  $("#new-label").value = "";
  drawGraph(); dirty();
};

$("#add-edge").onclick = () => {
  const source = $("#edge-src").value, target = $("#edge-tgt").value;
  if (!source || source === target) return;
  if (!state.rec.dag.edges.some((e) => e.source === source && e.target === target)) {
    state.rec.dag.edges.push({ source, target });
    drawGraph(); dirty();
  }
};

$("#delete-selected").onclick = () => {
  const sel = state.selected;
  if (!sel) return;
  if (sel.type === "node") {
    const n = state.rec.dag.nodes.find((x) => x.id === sel.id);
    if (n && (n.role === "exposure" || n.role === "outcome")) { $("#edit-msg").textContent = "Exposure and outcome can't be deleted."; return; }
    const s = n?.ai_suggested ? suggestionFor(n.id) : null;
    if (s) { setSugg(s.label, "rejected"); return; }
    state.rec.dag.nodes = state.rec.dag.nodes.filter((x) => x.id !== sel.id);
    state.rec.dag.edges = state.rec.dag.edges.filter((e) => e.source !== sel.id && e.target !== sel.id);
  } else {
    state.rec.dag.edges = state.rec.dag.edges.filter((e) => !(e.source === sel.source && e.target === sel.target));
  }
  drawGraph(); select(null); dirty();
};

$("#toggle-adjusted").onclick = () => {
  const n = state.rec.dag.nodes.find((x) => x.id === state.selected?.id);
  if (!n) return;
  n.adjusted = !n.adjusted;
  drawGraph(); dirty();
};

async function save() {
  state.rec = await api(`/api/records/${state.rec.id}`, {
    method: "PUT",
    body: JSON.stringify({ nodes: state.rec.dag.nodes, edges: state.rec.dag.edges, suggestion_status: state.status }),
  });
}

$("#save").onclick = async () => {
  try { await save(); renderRecord(); $("#edit-msg").textContent = "Saved."; }
  catch (e) { $("#edit-msg").textContent = e.message; }
};

$("#approve").onclick = async () => {
  const pending = Object.entries(state.status).filter(([, v]) => v === "suggested").map(([k]) => k);
  if (pending.length) { $("#edit-msg").textContent = `Accept or reject each AI suggestion first: ${pending.join(", ")}.`; return; }
  try {
    await save();
    state.rec = await api(`/api/records/${state.rec.id}/approve`, { method: "POST" });
    renderRecord();
    if (MOTION) gsap.fromTo("#r-status", { scale: 0.6 }, { scale: 1, duration: 0.6, ease: "elastic.out(1, 0.45)", clearProps: "transform" });
    $("#edit-msg").textContent = "";
  } catch (e) { $("#edit-msg").textContent = e.message; }
};

$("#copy-dagitty").onclick = async () => {
  try { await navigator.clipboard.writeText(state.rec.dagitty); $("#copy-dagitty").textContent = "Copied"; }
  catch { $("#copy-dagitty").textContent = "Select & copy manually"; }
  setTimeout(() => ($("#copy-dagitty").textContent = "Copy"), 1500);
};

// ---------- ask the dataset ----------
// Turn the model's plain-text answer into safe HTML: escape first, then [id] -> numbered source links,
// **bold**, and "- " bullet lists.
function renderAnswer(text, cited) {
  const order = cited.map((c) => c.id);
  const num = (id) => order.indexOf(id) + 1;
  const inline = (line) => esc(line)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/\s*\[([0-9a-f]{6,16})\]/g, (m, id) => (num(id) ? `<a class="cite" href="#/record/${id}" title="${esc(cited[num(id) - 1].title)}">${num(id)}</a>` : ""));
  const out = [];
  let list = [];
  const flush = () => { if (list.length) { out.push(`<ul>${list.map((l) => `<li>${l}</li>`).join("")}</ul>`); list = []; } };
  text.split(/\n+/).forEach((line) => {
    const m = line.match(/^\s*[-•*]\s+(.*)/);
    if (m) list.push(inline(m[1]));
    else { flush(); if (line.trim()) out.push(`<p>${inline(line)}</p>`); }
  });
  flush();
  return out.join("");
}

async function askDataset(question) {
  const out = $("#ask-out"), btn = $("#ask-btn");
  question = question.trim();
  if (!question) return;
  $("#ask-q").value = question;
  btn.disabled = true;
  out.hidden = false;
  out.innerHTML = `<p class="muted">Thinking… <span class="small">(Nemotron is planning the query, then answering from the dataset)</span></p>`;
  try {
    const r = await api("/api/ask", { method: "POST", body: JSON.stringify({ question, include_drafts: $("#f-drafts").checked }) });
    const short = (m) => m.split("/").pop();
    out.innerHTML = `<div class="ask-answer">${renderAnswer(r.answer, r.cited)}</div>
      ${r.cited.length ? `<ol class="ask-sources">${r.cited.map((c) => `<li><a href="#/record/${esc(c.id)}">${esc(c.title)}</a> <span class="muted">${esc(c.year || "")}</span></li>`).join("")}</ol>` : ""}
      <p class="ask-method small muted"><b>How this was answered:</b> ${esc(r.method)}.
        Query planned by ${esc(short(r.planner || ""))}, answer written by ${esc(short(r.model))}.
        AI-generated: check the cited papers.</p>`;
    enter("#ask-out > *", { stagger: 0.06, y: 8 });
  } catch (e) {
    out.innerHTML = `<p class="error">${esc(e.message)}</p>`;
  } finally {
    btn.disabled = false;
  }
}

$("#ask-form").addEventListener("submit", (ev) => { ev.preventDefault(); askDataset($("#ask-q").value); });
document.querySelectorAll(".ask-examples .chip").forEach((b) => (b.onclick = () => askDataset(b.textContent)));

// ---------- dataset ----------
let datasetRows = [];

// Shared hover tooltip for chart marks with a data-tip attribute.
function bindTooltips(container) {
  const tip = $("#tooltip");
  container.querySelectorAll("[data-tip]").forEach((el) => {
    el.addEventListener("mousemove", (ev) => {
      tip.innerHTML = el.dataset.tip;
      tip.hidden = false;
      const x = Math.min(ev.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
      tip.style.left = `${x}px`;
      tip.style.top = `${ev.clientY + 14}px`;
    });
    el.addEventListener("mouseleave", () => (tip.hidden = true));
  });
}

const NO_DATA = (what) => `<p class="small muted">${what} Approve papers to add them here, or tick “show drafts”.</p>`;

function renderForestChart(data) {
  const rows = data.forest;
  const el = $("#forest-chart");
  if (!rows.length) { el.innerHTML = NO_DATA("No ratio estimates (HR, OR, RR) yet."); return; }
  const W = 680, L = 250, R = 110, top = 8, rowH = 30, H = top + rows.length * rowH + 30;
  const lo = Math.min(0.5, ...rows.map((r) => r.lo)), hi = Math.max(2, ...rows.map((r) => r.hi));
  const a = Math.log(lo) - 0.08, b = Math.log(hi) + 0.08;
  const X = (v) => L + ((Math.log(v) - a) / (b - a)) * (W - L - R);
  const ticks = [0.1, 0.2, 0.25, 0.5, 1, 2, 4, 5, 10, 20].filter((t) => t >= Math.exp(a) && t <= Math.exp(b));
  const trunc = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + "…" : s || "");
  let svg = `<svg viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="Forest plot of effect estimates">`;
  ticks.forEach((t) => {
    svg += `<line x1="${X(t)}" x2="${X(t)}" y1="${top}" y2="${H - 24}" class="${t === 1 ? "null" : "grid"}"/>
            <text x="${X(t)}" y="${H - 8}" class="axis" text-anchor="middle">${t}</text>`;
  });
  rows.forEach((r, i) => {
    const y = top + i * rowH + rowH / 2;
    const sig = r.lo > 1 || r.hi < 1;
    const tipHtml = `<b>${esc(r.exposure)} → ${esc(r.outcome)}</b><br>${esc(r.measure)} ${r.value} (95% CI ${r.lo}–${r.hi})${r.adjusted ? ", adjusted" : ""}<br><span class="muted">${esc(trunc(r.title, 90))} ${esc(r.year || "")}</span>`;
    svg += `<g class="frow ${sig ? "sig" : "ns"}" data-id="${esc(r.id)}" data-tip="${esc(tipHtml)}">
      <rect x="0" y="${y - rowH / 2}" width="${W}" height="${rowH}" class="hit"/>
      <text x="0" y="${y - 2}" class="lbl">${esc(trunc(`${r.exposure} → ${r.outcome}`, 40))}</text>
      <text x="0" y="${y + 10}" class="sub">${esc(r.design || "")} · ${esc(r.year || "")}</text>
      <line x1="${X(r.lo)}" x2="${X(r.hi)}" y1="${y}" y2="${y}" class="ci"/>
      <rect x="${X(r.value) - 4.5}" y="${y - 4.5}" width="9" height="9" rx="2" class="est"/>
      <text x="${W - R + 12}" y="${y + 4}" class="val">${esc(r.measure)} ${r.value} (${r.lo}–${r.hi})</text>
    </g>`;
  });
  svg += `</svg>`;
  el.innerHTML = svg + `<p class="small muted">Black: 95% CI excludes 1. Grey: includes 1.${data.forest_skipped ? ` ${data.forest_skipped} paper(s) with non-ratio estimates not shown.` : ""}</p>`;
  bindTooltips(el);
  el.querySelectorAll(".frow").forEach((g) => (g.onclick = () => (location.hash = `#/record/${g.dataset.id}`)));
}

function renderMissingChart(data) {
  const rows = data.missing_confounders;
  const el = $("#missing-chart");
  if (!rows.length) { el.innerHTML = NO_DATA("No AI suggestions yet."); return; }
  const n = data.n_papers, W = 520, L = 170, R = 96, rowH = 28, H = rows.length * rowH + 4;
  const X = (v) => (v / n) * (W - L - R);
  let svg = `<svg viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="Most often missed confounders">`;
  rows.forEach((r, i) => {
    const y = i * rowH + 4;
    const list = r.papers.slice(0, 6).map((t) => `• ${esc(t.length > 70 ? t.slice(0, 69) + "…" : t)}`).join("<br>");
    const tipHtml = `<b>${esc(r.label)}</b>: flagged in ${r.count} of ${n} papers (${r.accepted} accepted by reviewers)<br>${list}${r.papers.length > 6 ? "<br>…" : ""}`;
    svg += `<g data-tip="${esc(tipHtml)}">
      <rect x="0" y="${y}" width="${W}" height="${rowH}" class="hit"/>
      <text x="${L - 10}" y="${y + rowH / 2 + 4}" class="lbl" text-anchor="end">${esc(r.label.length > 26 ? r.label.slice(0, 25) + "…" : r.label)}</text>
      <rect x="${L}" y="${y + 6}" width="${Math.max(3, X(r.count))}" height="${rowH - 12}" rx="3" class="bar"/>
      <text x="${L + X(r.count) + 8}" y="${y + rowH / 2 + 4}" class="val">${r.count} of ${n}</text>
    </g>`;
  });
  el.innerHTML = svg + `</svg>`;
  bindTooltips(el);
}

async function loadDataset() {
  const drafts = $("#f-drafts").checked;
  api(`/api/dataset/insights?include_drafts=${drafts}`).then((d) => { renderForestChart(d); renderMissingChart(d); }).catch(() => {});
  datasetRows = await api(`/api/dataset?include_drafts=${drafts}`);
  const designs = [...new Set(datasetRows.map((r) => r.design))].sort();
  const cur = $("#f-design").value;
  $("#f-design").innerHTML = `<option value="">All designs</option>` + designs.map((d) => `<option ${d === cur ? "selected" : ""}>${esc(d)}</option>`).join("");
  renderTable();
  enter("#table tbody tr", { y: 8, stagger: 0.03, duration: 0.3 });
}

function renderTable() {
  const q = $("#f-search").value.toLowerCase();
  const d = $("#f-design").value;
  const rows = datasetRows.filter((r) =>
    (!d || r.design === d) &&
    (!q || [r.title, r.exposure, r.outcome, r.journal].join(" ").toLowerCase().includes(q)));
  $("#table tbody").innerHTML = rows.map((r) => `<tr data-id="${esc(r.id)}">
    <td>${esc(r.exposure)}</td><td>${esc(r.outcome)}</td><td>${esc(r.design)}</td>
    <td>${esc((r.title || "").slice(0, 90))}</td><td>${esc(r.year || "")}</td><td>${r.n_nodes}</td>
    <td><span class="badge ${esc(r.status)}">${esc(r.status)}</span></td></tr>`).join("");
  $("#empty").hidden = rows.length > 0;
  document.querySelectorAll("#table tbody tr").forEach((tr) => tr.onclick = () => (location.hash = `#/record/${tr.dataset.id}`));
}
$("#f-search").oninput = renderTable;
$("#f-design").onchange = renderTable;
$("#f-drafts").onchange = loadDataset;

// ---------- network (3D view of all papers) ----------
const NET_COLORS = { exposure: "#2bb38a", outcome: "#e2654b", confounder: "#5b8fd0", ai: "#e0a320", mediator: "#a57fc9", other: "#8a96a0" };
const net = { graph: null, data: null, selected: null, neighbours: new Set(), labels: new Map(), raf: null, labelMin: 3 };

// Text labels as HTML over the canvas: crisp at any zoom, and no extra 3D text library needed.
function buildNetLabels(nodes) {
  const box = $("#net-labels");
  box.innerHTML = "";
  net.labels.clear();
  nodes.forEach((n) => {
    const el = document.createElement("div");
    el.className = `net-label ${n.group}`;
    el.textContent = n.label;
    el.style.fontSize = `${11 + Math.min(5, n.n_papers * 0.45)}px`;
    box.appendChild(el);
    net.labels.set(n.id, el);
  });
  // label the most shared variables by default (about the top 15)
  const counts = nodes.map((n) => n.n_papers).sort((a, b) => b - a);
  net.labelMin = nodes.length <= 60 ? 0 : Math.max(2, counts[Math.min(24, counts.length - 1)] || 2);
}

function drawNetLabels() {
  net.raf = requestAnimationFrame(drawNetLabels);
  if (!net.graph || document.hidden) return;
  const cam = net.graph.camera();
  const m = cam.matrixWorldInverse.elements;
  const W = $("#net3d").clientWidth, H = $("#net3d").clientHeight;
  const shown = [];
  net.graph.graphData().nodes.forEach((n) => {
    const el = net.labels.get(n.id);
    if (!el || n.x === undefined) return;
    const wanted = net.selected ? n === net.selected || net.neighbours.has(n.id) : n.n_papers >= net.labelMin;
    const depth = -(m[2] * n.x + m[6] * n.y + m[10] * n.z + m[14]);  // distance in front of the camera
    if (!wanted || depth <= 0) { el.style.opacity = 0; return; }
    const p = net.graph.graph2ScreenCoords(n.x, n.y, n.z);
    if (p.x < -50 || p.y < -20 || p.x > W + 50 || p.y > H + 20) { el.style.opacity = 0; return; }
    shown.push({ el, p, depth, sel: n === net.selected });
  });
  if (!shown.length) return;
  // Perspective: compare each label's depth with the depth of the graph's centre (the origin).
  const centre = Math.max(1, -m[14]);
  shown.forEach(({ el, p, depth, sel }) => {
    const r = depth / centre;                                   // <1 = in front of the centre, >1 = behind
    const fade = Math.min(1, Math.max(0, (r - 0.85) / 0.4));    // solid until slightly in front, faint by 1.25x
    const scale = Math.min(1.6, Math.max(0.55, 1 / r));         // nearer = bigger, like real perspective
    el.style.opacity = sel ? 1 : (1 - fade * 0.85).toFixed(2);
    el.style.transform = `translate(${p.x.toFixed(1)}px, ${p.y.toFixed(1)}px) translate(-50%, 9px) scale(${scale.toFixed(3)})`;
    el.style.zIndex = String(100000 - Math.round(depth * 10));  // nearer labels on top
    el.classList.toggle("selected", sel);
  });
}

function stopNetLabels() { if (net.raf) cancelAnimationFrame(net.raf); net.raf = null; }

function loadScript(src) {
  return new Promise((ok, fail) => {
    if (document.querySelector(`script[src="${src}"]`)) return ok();
    const s = document.createElement("script");
    s.src = src; s.onload = ok; s.onerror = () => fail(new Error("Couldn't load the 3D library."));
    document.head.appendChild(s);
  });
}

async function loadNetwork() {
  const data = await api(`/api/dataset/network?include_drafts=${$("#n-drafts").checked}`);
  net.data = data;
  const el = $("#net3d");
  $("#net-empty").hidden = data.nodes.length > 0;
  $("#net-empty").innerHTML = `<p>No approved papers yet. Tick “show drafts” to explore the drafts.</p>`;
  showNetOverview();
  if (!data.nodes.length) { if (net.graph) net.graph.graphData({ nodes: [], links: [] }); $("#net-labels").innerHTML = ""; return; }
  await loadScript("https://unpkg.com/3d-force-graph@1.80.0/dist/3d-force-graph.min.js");
  const graphData = { nodes: data.nodes.map((n) => ({ ...n })), links: data.links.map((l) => ({ ...l })) };
  if (!net.graph) {
    net.graph = new ForceGraph3D(el, { controlType: "orbit" })
      .backgroundColor("#0f1720")
      .showNavInfo(false)
      .nodeId("id")
      .nodeVal((n) => n.n_papers)
      .nodeRelSize(2.6)
      .nodeResolution(28)          // smooth spheres (default is 8 segments)
      .nodeOpacity(1)
      .nodeLabel((n) => `<div class="net-tip"><b>${esc(n.label)}</b><br>${n.n_papers} of ${net.data.n_papers} papers · ${esc(n.group)}</div>`)
      .nodeColor((n) => (net.selected && n !== net.selected && !net.neighbours.has(n.id) ? "#2a3642" : NET_COLORS[n.group] || NET_COLORS.other))
      .linkColor((l) => {
        if (net.selected) {
          const touches = l.source.id === net.selected.id || l.target.id === net.selected.id;
          return touches ? (l.ai ? "#f0c060" : "#e8eef4") : "rgba(120,135,150,0.08)";
        }
        return l.ai ? "rgba(240,185,70,0.75)" : "rgba(190,205,220,0.6)";
      })
      .linkWidth((l) => Math.min(2.2, 0.35 + l.n_papers * 0.35))
      .linkOpacity(0.9)
      .linkDirectionalArrowLength(3.5)
      .linkDirectionalArrowColor((l) => (l.ai ? "#f0b946" : "#c8d4df"))
      .linkDirectionalArrowRelPos(1)
      .onNodeClick(selectNetNode)
      .onBackgroundClick(() => { net.selected = null; net.neighbours.clear(); refreshNetStyles(); showNetOverview(); });
    const controls = net.graph.controls();
    controls.autoRotate = true;              // slow spin until the viewer takes over
    controls.autoRotateSpeed = 0.6;
    controls.mouseButtons.MIDDLE = 0;        // middle button rotates too (0 = ROTATE; scroll wheel still zooms)
    controls.addEventListener("start", () => (controls.autoRotate = false));
    new ResizeObserver(() => net.graph && net.graph.width(el.clientWidth).height(el.clientHeight)).observe(el);
    net.graph.onEngineStop(() => { if (net.needsFit) { net.needsFit = false; net.graph.zoomToFit(700, 60); } });
  }
  net.selected = null; net.neighbours.clear();
  net.needsFit = true;
  net.graph.graphData(graphData).width(el.clientWidth).height(el.clientHeight).resumeAnimation();
  buildNetLabels(graphData.nodes);
  stopNetLabels();
  drawNetLabels();
}

function refreshNetStyles() {
  // re-applying the accessors makes the library recolour everything
  net.graph.nodeColor(net.graph.nodeColor()).linkColor(net.graph.linkColor());
}

function selectNetNode(node) {
  net.selected = node;
  net.neighbours = new Set();
  net.graph.graphData().links.forEach((l) => {
    if (l.source.id === node.id) net.neighbours.add(l.target.id);
    if (l.target.id === node.id) net.neighbours.add(l.source.id);
  });
  refreshNetStyles();
  const dist = 90, r = 1 + dist / Math.hypot(node.x || 1, node.y || 1, node.z || 1);
  net.graph.cameraPosition({ x: node.x * r, y: node.y * r, z: node.z * r }, node, 900);
  const roleLabel = { exposure: "exposure", outcome: "outcome", confounder: "confounder", mediator: "mediator", ai: "AI-suggested", other: "other", collider: "collider", effect_modifier: "effect modifier" };
  $("#net-detail").innerHTML = `<h3>${esc(node.label)}</h3>
    <p class="small">In <b>${node.n_papers}</b> of ${net.data.n_papers} papers · linked to ${net.neighbours.size} other variables</p>
    ${node.mesh ? `<p class="small">MeSH: <a href="https://meshb.nlm.nih.gov/record/ui?ui=${esc(node.mesh_id)}" target="_blank" rel="noopener">${esc(node.mesh)}</a> <span class="muted">(${esc(node.mesh_id)})</span></p>` : ""}
    ${node.aliases.length > 1 ? `<p class="small"><b>Merged names</b> <span class="tag">${node.merged_by === "nemotron" ? "by Nemotron Nano" : "by name rules"}</span><br>
      ${node.aliases.map((a) => `<span class="alias">${esc(a)}</span>`).join(" ")}</p>` : ""}
    <ul class="net-papers">${node.papers.map((p) => `<li><a href="#/record/${esc(p.id)}">${esc(p.title.length > 80 ? p.title.slice(0, 79) + "…" : p.title)}</a>
      <span class="net-role ${esc(p.role)}">${esc(roleLabel[p.role] || p.role)}${p.adjusted ? ", adjusted" : ""}</span></li>`).join("")}</ul>
    <button class="mini secondary" id="net-back">← All variables</button>`;
  $("#net-back").onclick = () => { net.selected = null; net.neighbours.clear(); refreshNetStyles(); showNetOverview(); net.graph.zoomToFit(800); };
}

function showNetOverview() {
  const d = net.data;
  if (!d || !d.nodes.length) { $("#net-detail").innerHTML = ""; return; }
  $("#net-detail").innerHTML = `<h3>Most shared variables</h3>
    <p class="small muted">${d.nodes.length} variables and ${d.links.length} causal links from ${d.n_papers} papers.</p>
    <ol class="net-top">${d.nodes.slice(0, 10).map((n) => `<li><button class="linklike" data-node="${esc(n.id)}">${esc(n.label)}</button>
      <span class="muted small">${n.n_papers} papers</span></li>`).join("")}</ol>`;
  document.querySelectorAll("[data-node]").forEach((b) => (b.onclick = () => {
    const node = net.graph?.graphData().nodes.find((n) => n.id === b.dataset.node);
    if (node) selectNetNode(node);
  }));
}

$("#n-drafts").onchange = () => loadNetwork().catch((e) => ($("#net-detail").textContent = e.message));

// ---------- boot ----------
api("/api/config").then((c) => {
  state.config = c;
  const short = (m) => m.split("/").pop();
  $("#m-fast").textContent = short(c.model_fast);
  $("#m-reason").textContent = short(c.model_reason);
  document.querySelectorAll(".mf").forEach((e) => (e.textContent = short(c.model_fast)));
  document.querySelectorAll(".mr").forEach((e) => (e.textContent = short(c.model_reason)));
}).catch(() => {});
route();
