// Records the network scene for the demo film: from the dataset page, click "Network", include drafts, let the 3D
// web of variables turn, then click "PPI use" so its studies light up.
// Writes assets/rec/network.mp4 (30 fps) and assets/rec/track-network.js (window.TRACK_NET: cursor path, clicks, marks).
// No models run: the paper is already in the dataset. Run from demo/: node scripts/record-network.cjs
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");
const { chromium } = require("playwright");

const APP = "http://127.0.0.1:8000";
const NODE = process.env.NODE || "Socioeconomic status";
const W = 1440, H = 810;
const OUT = path.join(__dirname, "..", "assets", "rec");
const FR = path.join(OUT, "frames-network");
const CHROME = (() => {
  const base = path.join(process.env.HOME, ".cache/hyperframes/chrome/chrome-headless-shell");
  const v = fs.readdirSync(base).sort().pop();
  return path.join(base, v, "chrome-headless-shell-mac-arm64", "chrome-headless-shell");
})();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = () => Date.now() / 1000;

// ---- the slow stretches (the layout settling, the cursor's approach while the page is busy) play faster;
// frames -> 30 fps video with repeated frames (interpolation warped the moving labels); events -> the video's clock
function build({ frames, ev }) {
  const first = frames[0].ts;
  const raw = (t) => t - first;
  const m = Object.fromEntries(ev.filter((e) => e.mark).map((e) => [e.mark, raw(e.t)]));
  // each stretch is squeezed to a target length in seconds (cursor moves lag while the busy 3D page draws)
  const fit = (a, b, target) => [a, b, Math.max(1, (b - a) / target)];
  const segs = [fit(m.network + 0.6, m.settle_start, 2.0), fit(m.settle_start, m.drafts, 1.0),
                fit(m.drafts, m.aim_start, 3.0), fit(m.aim_start, m.node, 2.4)];
  const map = (t) => {
    let out = t;
    for (const [a, b, k] of segs) out -= (Math.min(Math.max(t, a), b) - a) * (1 - 1 / k);
    return out;
  };
  const times = frames.map((f) => map(raw(f.ts)));
  const endAt = map(m.end);
  const list = frames.map((f, i) => `file '${f.file}'\nduration ${Math.max(0.001, (i + 1 < frames.length ? times[i + 1] : Math.max(times[i] + 0.1, endAt)) - times[i]).toFixed(4)}`).join("\n")
    + `\nfile '${frames[frames.length - 1].file}'\n`;
  fs.writeFileSync(path.join(OUT, "frames-network.txt"), list);
  execFileSync("ffmpeg", ["-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", path.join(OUT, "frames-network.txt"),
    "-vf", "fps=30,scale=2880:1620:flags=lanczos,format=yuv420p",
    "-c:v", "libx264", "-crf", "14", "-preset", "slow", path.join(OUT, "network.mp4")]);
  const track = { w: W, h: H, duration: +Math.max(times[times.length - 1] + 0.1, endAt).toFixed(3),
    events: ev.map((e) => ({ ...e, t: +map(raw(e.t)).toFixed(3) })) };
  fs.writeFileSync(path.join(OUT, "track-network.js"), "window.TRACK_NET = " + JSON.stringify(track) + ";\n");
  console.log(`${frames.length} frames, ${track.duration}s`);
  console.log("marks:", track.events.filter((e) => e.mark).map((e) => `${e.mark}@${e.t}`).join(" "));
}

if (process.argv.includes("--post")) {
  build(JSON.parse(fs.readFileSync(path.join(OUT, "raw-network.json"), "utf8")));
  process.exit(0);
}

(async () => {
  fs.rmSync(FR, { recursive: true, force: true });
  fs.mkdirSync(FR, { recursive: true });
  // WebGL in headless Chrome: use the software renderer so the 3D scene draws
  const browser = await chromium.launch({ executablePath: CHROME });   // hardware GPU (HyperFrames renders show it works here)
  const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: +(process.env.DPR || 1) });
  const p = await ctx.newPage();
  await p.goto(APP + "/#/");
  await p.waitForSelector("#forest-chart svg", { timeout: 30000 });
  await sleep(1200);

  const cdp = await ctx.newCDPSession(p);
  const frames = [];
  let n = 0;
  cdp.on("Page.screencastFrame", async (f) => {
    const file = path.join(FR, String(n++).padStart(5, "0") + ".jpg");
    fs.writeFileSync(file, Buffer.from(f.data, "base64"));
    frames.push({ file, ts: f.metadata.timestamp });
    try { await cdp.send("Page.screencastFrameAck", { sessionId: f.sessionId }); } catch (e) {}
  });
  await cdp.send("Page.startScreencast", { format: "jpeg", quality: 92, maxWidth: W * 2, maxHeight: H * 2, everyNthFrame: 1 });

  const ev = [];
  let cx = W - 300, cy = H - 200;
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
  async function click() {
    await sleep(160);
    ev.push({ t: now(), click: true });
    await p.mouse.down(); await sleep(70); await p.mouse.up();
  }
  async function center(sel) {
    const b = await (await p.waitForSelector(sel, { timeout: 30000 })).boundingBox();
    return [b.x + b.width / 2, b.y + b.height / 2];
  }
  const nodeAt = () => p.evaluate((id) => {   // where the node is on screen right now
    const node = net.graph.graphData().nodes.find((x) => x.id === id);
    const c = net.graph.graph2ScreenCoords(node.x, node.y, node.z);
    const r = document.querySelector("#net3d").getBoundingClientRect();
    return [r.left + c.x, r.top + c.y];
  }, NODE);

  await sleep(400);
  mark("start");
  await sleep(900);

  // 1) the menu: Network
  const [nx, ny] = await center('nav a[data-nav="network"]');
  await moveTo(nx, ny, 900);
  await click();
  await p.waitForFunction(() => typeof net !== "undefined" && net.graph && net.graph.graphData().nodes.length > 0, null, { timeout: 30000 });
  mark("network");
  await sleep(900);

  // 2) include drafts, so the new paper is part of the web
  const [dx, dy] = await center("#n-drafts");
  await moveTo(dx, dy, 900);
  await click();
  mark("settle_start");
  await p.waitForFunction(() => net.data && net.data.n_papers >= 16 && !net.needsFit, null, { timeout: 30000 });
  mark("drafts");
  await moveTo(1180, 640, 1000);   // out of the way while the web turns
  await sleep(2600);

  mark("aim_start");
  // 3) click "PPI use": aim, re-aim for the last few pixels (the web is turning), click, check it took
  for (let attempt = 0; attempt < 3; attempt++) {
    let [tx, ty] = await nodeAt();
    await moveTo(tx, ty, attempt ? 300 : 1000);
    [tx, ty] = await nodeAt();
    await moveTo(tx, ty, 120);
    await click();
    await sleep(250);
    if (await p.evaluate((id) => net.selected && net.selected.id === id, NODE)) break;
    if (attempt === 2) throw new Error("couldn't select " + NODE);
  }
  mark("node");
  await sleep(4200);   // the camera flies in; its studies list in the side panel
  mark("end");
  await cdp.send("Page.stopScreencast");
  await sleep(300);
  await browser.close();

  fs.writeFileSync(path.join(OUT, "raw-network.json"), JSON.stringify({ frames, ev }));
  build({ frames, ev });
})().catch((e) => { console.error(e); process.exit(1); });
