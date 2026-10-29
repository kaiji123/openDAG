"""
OpenDAG demo soundtrack: UI sounds placed on the recording's real events (no music, like the Anomality demo).

Runs in earshot's venv:  ~/tools/earshot/.venv/bin/python scripts/sound.py   (from demo/)
Reads assets/rec/track.js (clicks and keys with their times), the camera / caption times of compositions/screen.html,
and the film layout of index.html. Writes assets/audio/master.wav and sound/cues.json (frames, for
`earshot timeline --cues`).
"""
import json, os, re

import numpy as np
import pyloudnorm as pyln
import soundfile as sf

from earshot.forge import kit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SR, FPS = 48000, 30
KIT_JSON = os.path.join(ROOT, "sound", "kit.json")
KIT_DIR = os.path.join(ROOT, "sound", "takes")
SFX_DB = -2.0
PEAK_CEIL = 10 ** (-2.0 / 20)


def put(buf, t, y, gain_db=0.0):
    i = int(round(t * SR))
    if y.ndim == 1:
        y = np.stack([y, y], 1)
    a, b = max(i, 0), min(i + len(y), len(buf))
    if b > a:
        buf[a:b] += y[a - i:b - i] * 10 ** (gain_db / 20)


def limiter_gain(x, look=0.005, release=0.06):
    from scipy.ndimage import minimum_filter1d, uniform_filter1d
    a = np.max(np.abs(x), axis=1)
    need = np.minimum(1.0, PEAK_CEIL / np.maximum(a, 1e-9))
    r = int((look + release) * SR)
    return uniform_filter1d(minimum_filter1d(need, size=2 * r + 1), size=r + 1).astype(np.float32)


class Kit:
    def __init__(self):
        self.lock = kit.render(KIT_JSON, KIT_DIR)
        self.n, self.cache, self.cues = {}, {}, []

    def cue(self, buf, role, t, what, pitch=None, extra_db=0.0):
        r = self.lock["roles"][role]
        takes = [x for x in r["takes"] if pitch is None or x.get("pitch") == pitch]
        k = self.n.get(role, 0); self.n[role] = k + 1
        tk = takes[k % len(takes)]       # rotate takes: the same sample twice in a row is the tell
        if tk["file"] not in self.cache:
            y, _ = sf.read(os.path.join(KIT_DIR, tk["file"]), dtype="float32", always_2d=True)
            self.cache[tk["file"]] = y if y.shape[1] == 2 else np.repeat(y, 2, 1)
        put(buf, kit.place(tk, r, t), self.cache[tk["file"]], r["level_db"] + SFX_DB + extra_db)
        self.cues.append({"f": round(t * FPS), "kind": role, "id": what})


def main():
    index = open(os.path.join(ROOT, "index.html")).read()
    total = float(re.search(r'id="root"[^>]*data-duration="([\d.]+)"', index).group(1))
    end_start = float(re.search(r'id="endcard"[^>]*data-start="([\d.]+)"', index).group(1))

    n = int(total * SR)
    K = Kit()
    fx = np.zeros((n, 2), np.float32)

    # ---- the hook: one light tap as the question lands
    K.cue(fx, "land.light", 2.1 + 0.05, "question lands", extra_db=-1)

    # ---- the recorded scenes' own events: (scene id, its file, its track file)
    for scene, html_name, track_name in [("screen", "screen.html", "track.js"), ("network", "network.html", "track-network.js")]:
        track = json.loads(open(os.path.join(ROOT, "assets", "rec", track_name)).read().split("=", 1)[1].rstrip().rstrip(";"))
        html = open(os.path.join(ROOT, "compositions", html_name)).read()
        start = float(re.search(rf'id="{scene}"[^>]*?data-start="([\d.]+)"', index, re.S).group(1))
        length = float(re.search(rf'id="{scene}"[^>]*?data-duration="([\d.]+)"', index, re.S).group(1))
        s0 = float(re.search(r"const S0 = ([\d.]+)", html).group(1))
        film = lambda src, start=start, s0=s0: start + (src - s0)       # recording clock -> film clock
        moves = [(float(a), float(d)) for a, d in re.findall(r"move\(([\d.]+), ([\d.]+),", html)]
        caps = [float(a) for a in re.findall(r'cap\("#\w+-c\d", ([\d.]+),', html)]
        busy = []
        for e in track["events"]:
            t = film(e["t"])
            if t < start or t >= start + length:
                continue
            if e.get("click"):
                K.cue(fx, "click", t, scene + " click"); busy.append(t)
            elif e.get("key"):
                K.cue(fx, "key", t, "key " + e["key"]); busy.append(t)
        for a in caps:
            K.cue(fx, "land.light", film(a) + 0.05, "caption lands", extra_db=-3); busy.append(film(a))
        for a, d in moves:
            mid = film(a + d / 2)
            if not busy or min(abs(mid - b) for b in busy) > 0.5:   # a glide only where nothing else is sounding
                K.cue(fx, "move", mid, "camera move")

    # ---- the end card (compositions/endcard.html, frames at 30 fps from its start)
    K.cue(fx, "whoosh.long", end_start + 6 / FPS, "colour flood", extra_db=-6)
    K.cue(fx, "pop", end_start + 76 / FPS, "the diamonds pop in", extra_db=-4)
    K.cue(fx, "bell", end_start + 108 / FPS, "the wordmark completes", pitch="G5", extra_db=-3)

    # UI sounds peak at -3 dBFS into the limiter; no music bed (Kai prefers effects only)
    fx *= 10 ** (4.0 / 20) / max(np.max(np.abs(fx)), 1e-9)
    fx *= limiter_gain(fx)[:, None]
    os.makedirs(os.path.join(ROOT, "assets", "audio"), exist_ok=True)
    sf.write(os.path.join(ROOT, "assets", "audio", "master.wav"), fx, SR, subtype="PCM_24")
    json.dump({"fps": FPS, "events": [{"f": c["f"], "kind": c["kind"], "id": c["id"]} for c in K.cues]},
              open(os.path.join(ROOT, "sound", "cues.json"), "w"), indent=1)
    meter = pyln.Meter(SR)
    print(f"{total}s, {len(K.cues)} cues, master {meter.integrated_loudness(fx):.1f} LUFS")


if __name__ == "__main__":
    main()
