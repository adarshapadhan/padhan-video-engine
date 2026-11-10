#!/usr/bin/env python3
"""Render a documentary video from script.json + TTS manifest + images.
Usage: python3 render.py <script.json> <tts_dir> <img_dir> <out.mp4> <long|short>
"""
import json, math, os, subprocess, sys, random
import numpy as np
import soundfile as sf
from PIL import Image, ImageFilter
from multiprocessing import Pool

script_p, tts_dir, img_dir, out_p, mode = sys.argv[1:6]
LONG = mode == "long"
W, H = (1920, 1080) if LONG else (1080, 1920)
FPS = 30
XF = 0.45                  # crossfade seconds
SENT_GAP = 0.20            # pause between sentences
SCENE_GAP = 0.38 if LONG else 0.18
SR = 24000
work = os.path.join(os.path.dirname(out_p) or ".", f"work_{mode}")
os.makedirs(work, exist_ok=True)

script = json.load(open(script_p))
manifest = {m["id"]: m for m in json.load(open(os.path.join(tts_dir, "manifest.json")))}

# ---------- 1. narration timeline ----------
audio = []
t = 0.0
scenes = []
words_all = []  # (start, end, word)
for si, sc in enumerate(script["scenes"]):
    m = manifest[sc["id"]]
    st = t
    lead = 0.0
    if sc.get("chapter") and LONG:
        lead = 2.0            # silent beat for chapter card
    if lead:
        audio.append(np.zeros(int(lead * SR), dtype=np.float32)); t += lead
    for k, s in enumerate(m["sentences"]):
        y, sr = sf.read(s["file"], dtype="float32")
        assert sr == SR
        if y.ndim > 1: y = y.mean(axis=1)
        # trim leading/trailing silence a bit
        nz = np.where(np.abs(y) > 0.01)[0]
        if len(nz): y = y[max(0, nz[0] - 600): min(len(y), nz[-1] + 1200)]
        dur = len(y) / SR
        # word timings proportional to length
        ws = s["text"].split()
        weights = [len(w.strip(",.;:!?\"'")) + 2 for w in ws]
        tot = sum(weights); cur = t
        for w, wt in zip(ws, weights):
            d = dur * wt / tot
            words_all.append((cur, cur + d, w)); cur += d
        audio.append(y); t += dur
        gap = SENT_GAP if k < len(m["sentences"]) - 1 else SCENE_GAP
        if sc.get("overlay") and k == 0 and not LONG: gap += 0.1
        audio.append(np.zeros(int(gap * SR), dtype=np.float32)); t += gap
    scenes.append({**sc, "start": st, "dur": t - st, "lead": lead})
tail = 2.5 if LONG else 0.6
audio.append(np.zeros(int(tail * SR), dtype=np.float32)); t += tail
scenes[-1]["dur"] += tail
total = t
narr = np.concatenate(audio)
sf.write(os.path.join(work, "narration.wav"), narr, SR)
print(f"total duration {total:.1f}s, {len(scenes)} scenes")

# ---------- 2. music bed + sfx (synthesized, royalty-free) ----------
MSR = 48000
n = int(total * MSR) + MSR
tt = np.arange(n) / MSR
rng = np.random.default_rng(7)
def tone(f, amp, detune=0.0):
    return amp * (np.sin(2*np.pi*f*tt) + 0.5*np.sin(2*np.pi*f*(1+detune)*tt + 1.3))
# slow chord changes: Dm - Bb - F - C (low, dark), 8s each
chords = [[73.42, 110.0, 174.61], [58.27, 116.54, 174.61], [87.31, 130.81, 174.61], [65.41, 98.0, 164.81]]
mus = np.zeros(n)
seg = 8.0
for ci in range(int(total // seg) + 2):
    a, b = int(ci*seg*MSR), int(min(n, (ci+1)*seg*MSR + 2*MSR))
    if a >= n: break
    env = np.ones(b - a)
    fl = min(len(env), 2*MSR); env[:fl] *= np.linspace(0, 1, fl); env[-fl:] *= np.linspace(1, 0, fl)
    ch = chords[ci % 4]
    s = sum(0.22*np.sin(2*np.pi*f*tt[a:b]) + 0.08*np.sin(2*np.pi*2*f*tt[a:b] + 0.7) for f in ch)
    mus[a:b] += s * env
lfo = 0.75 + 0.25*np.sin(2*np.pi*tt/11.0)
mus *= lfo
# soft noise wind
wind = np.convolve(rng.standard_normal(n), np.ones(400)/400, mode="same") * 0.6
mus += wind * (0.6 + 0.4*np.sin(2*np.pi*tt/17))
# low booms + whooshes at scene changes
def boom(at, amp):
    i = int(at*MSR); L = int(2.2*MSR)
    if i >= n: return
    L = min(L, n - i); x = np.arange(L)/MSR
    mus[i:i+L] += amp * np.sin(2*np.pi*(48*np.exp(-x*1.5)+28)*x) * np.exp(-x*2.2)
def whoosh(at, amp):
    L = int(0.9*MSR); i = int(max(0, at - 0.45)*MSR)
    if i + L >= n: return
    noise = rng.standard_normal(L)
    sm = np.convolve(noise, np.ones(30)/30, mode="same")
    env = np.sin(np.linspace(0, np.pi, L))**2
    mus[i:i+L] += amp * sm * env
for k, sc in enumerate(scenes):
    if k > 0: whoosh(sc["start"], 0.5 if sc.get("chapter") else 0.25)
    if sc.get("overlay") or sc.get("chapter"): boom(sc["start"] + (sc["lead"] if sc.get("chapter") else 0.05), 1.6)
mus = mus / (np.max(np.abs(mus)) + 1e-9) * 0.9
# fade out end
fo = int(3*MSR); mus[-fo:] *= np.linspace(1, 0, fo)
sf.write(os.path.join(work, "music.wav"), mus.astype(np.float32), MSR)

# ---------- 3. per-scene Ken Burns clips ----------
moves = ["in", "out", "left", "right", "in", "up"]
def render_scene(k):
    sc = scenes[k]
    out = os.path.join(work, f"clip_{k:03d}.mp4")
    if os.path.exists(out): return out
    d = sc["dur"] + (XF if k < len(scenes) - 1 else 0)
    frames = int(round(d * FPS))
    cands=[os.path.join(img_dir, sc["id"]+e) for e in (".webp",".jpg",".png")]
    img = Image.open(next(c for c in cands if os.path.exists(c))).convert("RGB")
    # cover-fit to W*1.2 x H*1.2 canvas for movement room
    cw, ch = int(W*1.18), int(H*1.18)
    r = max(cw/img.width, ch/img.height)
    img = img.resize((int(img.width*r+0.5), int(img.height*r+0.5)), Image.LANCZOS)
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))
    iw, ih = img.size
    mv = moves[k % len(moves)] if k else "in"
    p = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                          "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
                          "-crf", "17", "-pix_fmt", "yuv420p", out], stdin=subprocess.PIPE)
    for f in range(frames):
        u = f / max(1, frames - 1)
        e = u*u*(3 - 2*u) * 0.85 + u*0.15   # ease
        if mv == "in":   z = 1.0 + 0.16*e; cx, cy = 0.5, 0.48
        elif mv == "out": z = 1.16 - 0.16*e; cx, cy = 0.5, 0.5
        elif mv == "left": z = 1.08; cx, cy = 0.56 - 0.12*e, 0.5
        elif mv == "right": z = 1.08; cx, cy = 0.44 + 0.12*e, 0.5
        else: z = 1.08 + 0.04*e; cx, cy = 0.5, 0.56 - 0.12*e
        vw, vh = (W/ (W*1.18/iw)) / z / (iw/ (W*1.18)) if False else iw/1.18/z, ih/1.18/z
        x0 = cx*iw - vw/2; y0 = cy*ih - vh/2
        x0 = min(max(0, x0), iw - vw); y0 = min(max(0, y0), ih - vh)
        sx, sy = vw/W, vh/H
        fr = img.transform((W, H), Image.AFFINE, (sx, 0, x0, 0, sy, y0), resample=Image.BILINEAR)
        p.stdin.write(fr.tobytes())
    p.stdin.close(); p.wait()
    return out

with Pool(os.cpu_count() or 2) as pool:
    clips = pool.map(render_scene, range(len(scenes)))
print("clips done")

# ---------- 4. ASS: captions + overlays + chapter cards ----------
def ts(x):
    x = max(0, x); h = int(x//3600); m = int(x%3600//60); s = x%60
    return f"{h}:{m:02d}:{s:05.2f}"
if LONG:
    cap_font, cap_size, cap_mv, max_words = "Montserrat ExtraBold", 50, 90, 6
    ov_size, ch_size = 150, 120
else:
    cap_font, cap_size, cap_mv, max_words = "Montserrat Black", 92, 620, 3
    ov_size, ch_size = 240, 120
GOLD = "&H0037C8FF&"   # BGR -> warm gold
ass = [f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{cap_font},{cap_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,0,0,0,0,100,100,0,0,1,{5 if LONG else 7},{2 if LONG else 3},2,80,80,{cap_mv},1
Style: Ov,Cinzel Black,{ov_size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,0,0,0,0,100,100,6,0,1,4,6,8,60,60,{170 if LONG else 330},1
Style: ChSmall,Montserrat ExtraBold,{40 if LONG else 46},{GOLD},&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,14,0,1,0,0,5,60,60,0,1
Style: ChBig,Cinzel Black,{ch_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,4,0,1,0,3,5,80,80,0,1
Style: Box,Arial,10,&H00000000,&H00000000,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"""]
# captions: chunk words
chunks, curc = [], []
for w in words_all:
    curc.append(w)
    end_punct = w[2][-1] in ".!?,;:" if w[2] else False
    if len(curc) >= max_words or (end_punct and len(curc) >= 2) or w[2][-1:] in ".!?":
        chunks.append(curc); curc = []
if curc: chunks.append(curc)
for c in chunks:
    for i, (a, b, wd) in enumerate(c):
        end = c[i+1][0] if i + 1 < len(c) else b + 0.15
        parts = []
        for j, (_, _, w2) in enumerate(c):
            w2c = w2.upper() if not LONG else w2
            if j == i:
                pop = "" if LONG else r"\fscx112\fscy112"
                parts.append("{\\c" + GOLD + pop + "}" + w2c + "{\\c&H00FFFFFF&\\fscx100\\fscy100}")
            else:
                parts.append(w2c)
        ass.append(f"Dialogue: 1,{ts(a)},{ts(end)},Cap,,0,0,0,,{' '.join(parts)}")
# overlays & chapter cards
for sc in scenes:
    if sc.get("chapter") and LONG:
        a, b = sc["start"], sc["start"] + sc["lead"] + 0.2
        ass.append(f"Dialogue: 2,{ts(a)},{ts(b)},Box,,0,0,0,,{{\\alpha&H60&\\fad(250,300)\\p1}}m 0 0 l {W} 0 l {W} {H} l 0 {H}{{\\p0}}")
        ass.append(f"Dialogue: 3,{ts(a)},{ts(b)},ChSmall,,0,0,0,,{{\\pos({W//2},{H//2-95})\\fad(300,300)}}CHAPTER")
        ass.append(f"Dialogue: 3,{ts(a)},{ts(b)},ChBig,,0,0,0,,{{\\pos({W//2},{H//2+20})\\fad(300,300)\\fscx88\\fscy88\\t(0,1800,\\fscx100\\fscy100)}}{sc['chapter']}")
    if sc.get("overlay"):
        a = sc["start"] + (sc["lead"] if sc.get("chapter") else 0) + 0.1
        b = a + (2.6 if LONG else 2.0)
        ass.append(f"Dialogue: 2,{ts(a)},{ts(b)},Ov,,0,0,0,,{{\\fad(180,350)\\fscx135\\fscy135\\t(0,260,\\fscx100\\fscy100)\\blur1}}{sc['overlay']}")
ass_p = os.path.join(work, "subs.ass")
open(ass_p, "w").write("\n".join(ass) + "\n")

# ---------- 5. assemble: xfade chain, ASS, grade, audio ----------
inputs = []
for c in clips: inputs += ["-i", c]
fc = []
prev = "[0:v]"
for k in range(1, len(clips)):
    off = scenes[k]["start"]
    lab = f"[x{k}]"
    fc.append(f"{prev}[{k}:v]xfade=transition=fade:duration={XF}:offset={off:.3f}{lab}")
    prev = lab
grade = "eq=contrast=1.06:saturation=0.92,vignette=PI/4.6"
fc.append(f"{prev}{grade},ass={ass_p},fade=t=out:st={total-1.6:.2f}:d=1.6[v]")
na = len(clips)
fc.append(f"[{na}:a]aresample=48000,volume=1.0,highpass=f=70,acompressor=threshold=-18dB:ratio=3:attack=5:release=120,asplit=2[n][n2]")
fc.append(f"[{na+1}:a]volume={0.16 if LONG else 0.2}[m]")
fc.append(f"[m][n]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[md]")
fc.append(f"[n2][md]amix=inputs=2:normalize=0,alimiter=limit=0.95,afade=t=out:st={total-1.6:.2f}:d=1.6[a]")
cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-i", os.path.join(work, "narration.wav"),
       "-i", os.path.join(work, "music.wav"), "-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]",
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
       "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.2f}", "-movflags", "+faststart", out_p]
subprocess.run(cmd, check=True)
json.dump({"total": total, "scenes": [{k: s[k] for k in ("id", "start", "dur")} for s in scenes]},
          open(os.path.join(work, "timeline.json"), "w"), indent=1)
print("DONE", out_p, f"{total:.1f}s")
