// Records a scripted session of the real OpenDAG app (http://127.0.0.1:8000) for the demo film.
// Chrome's screencast at 1440x810 CSS px, 2x device pixels -> assets/rec/frames/*.jpg (variable rate),
// then a 30 fps video assets/rec/session.mp4, plus assets/rec/track.js: the cursor path, clicks, key presses and
// named marks on the video's clock. The wait while the models run is compressed (COMPRESS x) in both.
// Each take removes the demo paper from the database first, so the pipeline really runs (about 1 cent of tokens).
// Run from demo/: node scripts/record.cjs        (the app must be running on port 8000)
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");
const { chromium } = require("playwright");

const APP = "http://127.0.0.1:8000";
const PAPER_PAGE = "https://pmc.ncbi.nlm.nih.gov/articles/PMC9434890/";
const DOI = "10.1186/s12916-022-02478-y";
const PMCID = "PMC9434890";
const DB = path.join(__dirname, "..", "..", "opendag.db");
const W = 1440, H = 810;
const COMPRESS = +(process.env.COMPRESS || 12);   // how much faster the model wait plays
const OUT = path.join(__dirname, "..", "assets", "rec");
const FR = path.join(OUT, "frames");
const CHROME = (() => {   // the headless Chrome HyperFrames already downloaded
  const base = path.join(process.env.HOME, ".cache/hyperframes/chrome/chrome-headless-shell");
  const v = fs.readdirSync(base).sort().pop();
  return path.join(base, v, "chrome-headless-shell-mac-arm64", "chrome-headless-shell");
})();
// the app's admin token (from ../.env) lets takes bypass the per-visitor limit of 5 new papers an hour
const ADMIN = (fs.readFileSync(path.join(__dirname, "..", "..", ".env"), "utf8").match(/^ADMIN_TOKEN=(\S+)/m) || [])[1];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = () => Date.now() / 1000;

// ---- compress the model wait, then frames -> 30 fps video; events -> the video's clock
function build({ frames, ev, facts }) {
  const first = frames[0].ts;
  const raw = (t) => t - first;
  const m = Object.fromEntries(ev.filter((e) => e.mark).map((e) => [e.mark, raw(e.t)]));
  const a = m.proc_start, b = m.proc_end;
  const map = (t) => (t < a ? t : t < b ? a + (t - a) / COMPRESS : a + (b - a) / COMPRESS + (t - b));
  const times = frames.map((f) => map(raw(f.ts)));
  const endAt = map(m.end);   // the screen stops sending frames when nothing changes: hold the last one to "end"
  const list = frames.map((f, i) => `file '${f.file}'\nduration ${Math.max(0.001, (i + 1 < frames.length ? times[i + 1] : Math.max(times[i] + 0.1, endAt)) - times[i]).toFixed(4)}`).join("\n")
    + `\nfile '${frames[frames.length - 1].file}'\n`;
  fs.writeFileSync(path.join(OUT, "frames.txt"), list);
  execFileSync("ffmpeg", ["-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", path.join(OUT, "frames.txt"),
    "-vf", "fps=30,scale=2880:1620:flags=lanczos,format=yuv420p", "-c:v", "libx264", "-crf", "14", "-preset", "slow",
    path.join(OUT, "session.mp4")]);
  const track = {
    w: W, h: H, compress: { from: +a.toFixed(3), to: +b.toFixed(3), factor: COMPRESS },
    duration: +Math.max(times[times.length - 1] + 0.1, endAt).toFixed(3),
    facts,
    events: ev.map((e) => ({ ...e, t: +map(raw(e.t)).toFixed(3) })),
  };
  fs.writeFileSync(path.join(OUT, "track.js"), "window.TRACK = " + JSON.stringify(track) + ";\n");
  console.log(`${frames.length} frames, ${track.duration}s (model wait ${(b - a).toFixed(1)}s -> ${((b - a) / COMPRESS).toFixed(1)}s)`);
  console.log("marks:", track.events.filter((e) => e.mark).map((e) => `${e.mark}@${e.t}`).join(" "));
  console.log("top missed confounder:", JSON.stringify(facts));
}

if (process.argv.includes("--post")) {   // re-encode from the last take without re-recording
  build(JSON.parse(fs.readFileSync(path.join(OUT, "raw.json"), "utf8")));
  process.exit(0);
}

(async () => {
  // a fresh take: forget the paper so OpenDAG processes it for real
  execFileSync("sqlite3", [DB, `DELETE FROM records WHERE pmcid='${PMCID}';`]);
  fs.rmSync(FR, { recursive: true, force: true });
  fs.mkdirSync(FR, { recursive: true });

  const browser = await chromium.launch({ executablePath: CHROME });
  const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 2 });
  if (ADMIN) await ctx.route(APP + "/api/**", (route) => route.continue({ headers: { ...route.request().headers(), "x-admin-token": ADMIN } }));
  const p = await ctx.newPage();
  await p.addInitScript(() => {
    try { localStorage.clear(); } catch (e) {}
    // the film draws its own cursor; keep the page's tooltips but hide nothing else
  });
  await p.goto(PAPER_PAGE, { waitUntil: "domcontentloaded", timeout: 60000 });
  await p.evaluate(() => {   // hide the site's cookie / notice banners if any
    document.querySelectorAll("[class*=banner],[id*=banner],.usa-banner").forEach((e) => (e.style.display = "none"));
  });
  await sleep(1200);

  // ---- screencast
  const cdp = await ctx.newCDPSession(p);
  const frames = [];
  let n = 0;
  cdp.on("Page.screencastFrame", async (f) => {
    const file = path.join(FR, String(n++).padStart(5, "0") + ".jpg");
    fs.writeFileSync(file, Buffer.from(f.data, "base64"));
    frames.push({ file, ts: f.metadata.timestamp });
    try { await cdp.send("Page.screencastFrameAck", { sessionId: f.sessionId }); } catch (e) {}
  });
  const startCast = () => cdp.send("Page.startScreencast", { format: "jpeg", quality: 92, maxWidth: W * 2, maxHeight: H * 2, everyNthFrame: 1 });
  await startCast();

  const ev = [];   // {t, x, y} moves, {t, click:true}, {t, key:"a"}, {t, mark:"name"}
  let cx = W + 60, cy = H - 120;
  const mark = (name) => ev.push({ t: now(), mark: name });
  const ease = (u) => (u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2);
  async function moveTo(x, y, ms = 800) {
    const x0 = cx, y0 = cy, steps = Math.max(2, Math.round(ms / 16));
    for (let i = 1; i <= steps; i++) {
      const u = ease(i / steps);
      cx = x0 + (x - x0) * u; cy = y0 + (y - y0) * u;
      await p.mouse.move(cx, cy);
      ev.push({ t: now(), x: cx, y: cy });
      await sleep(16);
    }
  }
  async function center(sel) {
    const b = await (await p.waitForSelector(sel, { timeout: 60000 })).boundingBox();
    return [b.x + b.width / 2, b.y + b.height / 2];
  }
  async function clickAt(x, y, ms = 800) {
    await moveTo(x, y, ms);
    await sleep(180);
    ev.push({ t: now(), click: true });
    await p.mouse.down(); await sleep(70); await p.mouse.up();
  }
  const clickOn = async (sel, ms) => { const [x, y] = await center(sel); await clickAt(x, y, ms); };
  async function scrollTo(sel, offset = 70, ms = 1300) {
    const target = await p.$eval(sel, (e, o) => e.getBoundingClientRect().top + scrollY - o, offset);
    const y0 = await p.evaluate(() => scrollY), steps = Math.round(ms / 16);
    for (let i = 1; i <= steps; i++) {
      await p.evaluate((v) => window.scrollTo(0, v), y0 + (target - y0) * ease(i / steps));
      await sleep(16);
    }
  }

  await sleep(300);
  mark("start");

  // 1) the paper on PubMed Central: its adjustment list
  mark("pmc");
  await sleep(1300);
  const notes = await p.evaluateHandle(() => [...document.querySelectorAll("p")].find((x) => /Model 2: Cox/.test(x.textContent)));
  await p.evaluate((e) => e.setAttribute("id", "demo-covariates"), notes);
  await scrollTo("#demo-covariates", 300, 1600);
  mark("methods");
  await sleep(3000);

  // 2) OpenDAG: paste the DOI
  await p.goto(APP + "/#/add");
  await p.waitForSelector("#identifier");
  await sleep(700);
  mark("add");
  await clickOn("#identifier", 900);
  await sleep(250);
  for (const k of DOI) { ev.push({ t: now(), key: k }); await p.keyboard.type(k); await sleep(55); }
  await sleep(350);
  mark("typed");
  await clickOn("#submit-form button", 700);
  mark("submit");

  // 3) the pipeline runs (this stretch is compressed in the video)
  await sleep(600);
  mark("proc_start");
  await p.waitForFunction(() => {   // the record page, or fail fast if the app shows an error
    const err = document.querySelector("#submit-error");
    if (err && !err.hidden) throw new Error("app error: " + err.textContent);
    return location.hash.startsWith("#/record/");
  }, null, { timeout: 300000 });
  await p.waitForFunction(() => typeof state !== "undefined" && state.cy && state.cy.nodes().length > 0, null, { timeout: 30000 });
  mark("proc_end");
  await sleep(2800);   // the record page settles: key figures, then the DAG

  // 4) the DAG: click an AI-suggested confounder, then accept it
  mark("dag");
  await scrollTo("#cy", 24, 1300);
  await sleep(900);
  const target = await p.evaluate(() => {
    // show an unambiguous gap: skip suggestions that share a word with something the paper adjusted for
    // ("GERD severity" when GERD itself was adjusted for reads as a mistake on screen, even if defensible)
    const GENERIC = new Set(["index", "disease", "diseases", "status", "level", "history", "score", "total", "baseline",
                             "chronic", "other", "factor", "factors", "intake", "infection", "burden", "severity"]);
    const words = (t) => (t.toLowerCase().match(/[a-z0-9]{4,}/g) || []).filter((w) => !GENERIC.has(w));
    const adjusted = new Set(state.rec.dag.nodes.filter((n) => n.adjusted).flatMap((n) => words(n.label)));
    // and prefer a textbook confounder: something caused BY the exposure (e.g. PPIs -> low vitamin B12) is a
    // mediator, and accepting it on camera would look like a methods error
    const PREFER = [/frail/i, /diet/i, /kidney|renal/i];
    const rank = (n) => { const i = PREFER.findIndex((r) => r.test(n.label)); return i < 0 ? PREFER.length : i; };
    const ai = state.rec.dag.nodes.filter((n) => n.ai_suggested && !words(n.label).some((w) => adjusted.has(w)))
      .sort((a, b) => rank(a) - rank(b));
    const list = state.rec.critique.missing_confounders || [];
    for (const n of ai) {
      const i = list.findIndex((m) => slug(m.label) === n.id);
      const el = state.cy.getElementById(n.id);
      if (i >= 0 && el.nonempty()) {
        const pos = el.renderedPosition(), box = document.querySelector("#cy").getBoundingClientRect();
        return { id: n.id, i, x: box.left + pos.x, y: box.top + pos.y, label: n.label };
      }
    }
    return null;
  });
  if (!target) throw new Error("no AI-suggested node to click");
  console.log("clicking AI node:", target.label);
  await clickAt(target.x, target.y, 1000);
  mark("node");
  await sleep(3400);
  const acc = `[data-acc="${target.i}"]`;
  await p.$eval(acc, (e) => e.scrollIntoView({ block: "center", behavior: "smooth" }));
  await sleep(700);
  await clickOn(acc, 800);
  mark("accept");
  await sleep(1500);

  // 5) back to the dataset: the new paper among the others
  await scrollTo("body", 0, 900);
  await clickOn("a.back", 800);
  await p.waitForSelector("#forest-chart svg", { timeout: 30000 });
  await sleep(500);
  await scrollTo(".filters", 430, 1200);   // the checkbox sits under the charts: bring it on screen first
  await sleep(300);
  await clickOn("#f-drafts", 900);
  await p.waitForFunction(() => document.querySelectorAll("#forest-chart .frow").length > 14, null, { timeout: 30000 });
  mark("dataset");
  await sleep(500);
  await scrollTo("#forest-chart", 140, 1400);
  const rowSel = `#forest-chart .frow[data-id="${await p.evaluate(() => state.rec.id)}"]`;
  await p.$eval(rowSel, (g) => { const box = g.closest(".forest-rows"); const r = g.getBoundingClientRect(), b = box.getBoundingClientRect();
    box.scrollTo({ top: box.scrollTop + (r.top - b.top) - b.height / 2 + r.height / 2, behavior: "smooth" }); });
  await sleep(800);
  const [rx, ry] = await center(rowSel);
  await moveTo(rx + 40, ry, 1000);
  mark("forest");
  await sleep(3200);
  const [bx, by] = await center("#missing-chart g[data-tip] .bar");
  await moveTo(bx, by, 1100);
  mark("missed");
  await sleep(3000);
  mark("end");
  await cdp.send("Page.stopScreencast");
  const facts = await p.evaluate(async () => {
    const d = await (await fetch("/api/dataset/insights?include_drafts=true")).json();
    return { top: d.missing_confounders[0], n_papers: d.n_papers };
  });
  await sleep(300);
  await browser.close();

  fs.writeFileSync(path.join(OUT, "raw.json"), JSON.stringify({ frames, ev, facts }));
  build({ frames, ev, facts });
})().catch((e) => { console.error(e); process.exit(1); });
