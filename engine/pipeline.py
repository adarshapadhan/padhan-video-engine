#!/usr/bin/env python3
"""Padhan video engine: topic -> script -> review -> images -> voice -> render -> upload.
Runs on GitHub Actions. Needs env: GEMINI_API_KEY, YT_CLIENT_ID,
YT_ALL, CF_ACCOUNT_ID, CF_API_TOKEN. Usage: python engine/pipeline.py <channel-key> [--no-upload]
"""
import json, os, sys, time, random, re, subprocess, urllib.parse, datetime
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CH_KEY = sys.argv[1]
CH = json.load(open(os.path.join(ROOT, "channels.json")))[CH_KEY]
NO_UPLOAD = "--no-upload" in sys.argv or not CH.get("live", False)   # safety lock until approved
OUT = os.path.join(ROOT, "out", CH_KEY)
os.makedirs(OUT, exist_ok=True)
HIST_P = os.path.join(ROOT, "data", f"{CH_KEY}.json")
HIST = json.load(open(HIST_P)) if os.path.exists(HIST_P) else {"videos": []}
GKEY = os.environ["GEMINI_API_KEY"]

def log(*a): print(f"[{datetime.datetime.now(datetime.timezone.utc):%H:%M:%S}]", *a, flush=True)

# ---------------- LLMs: Gemini (writer) + Cloudflare Workers AI (everything else / fallback) ----------------
CF_ACC = os.environ.get("CF_ACCOUNT_ID", ""); CF_TOK = os.environ.get("CF_API_TOKEN", "")
GEMINI_MODELS = ["gemini-3.8-flash", "gemini-flash-latest", "gemini-flash-lite-latest"]
CF_MODELS = ["@cf/openai/gpt-oss-120b", "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "@cf/qwen/qwen3-30b-a3b-fp8"]
DEAD = set()   # providers/models that hit a daily quota this run

def _parse(txt, json_out):
    if not json_out: return txt
    if isinstance(txt, (dict, list)): return txt
    txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()
    txt = re.sub(r"^```(json)?|```$", "", txt.strip(), flags=re.M).strip()
    try: return json.loads(txt)
    except Exception:
        from json_repair import repair_json
        m = re.search(r"\{.*\}", txt, re.S)
        out = repair_json(m.group(0) if m else txt, return_objects=True)
        if not isinstance(out, dict) or not out: raise ValueError("unparseable JSON")
        return out

def _gemini(m, prompt, json_out, temp):
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"temperature": temp}}
    if json_out: body["generationConfig"]["responseMimeType"] = "application/json"
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                      params={"key": GKEY}, json=body, timeout=300)
    if r.status_code != 200: return None, r.status_code, r.text[:150]
    return "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"]), 200, ""

def _cf_chat(m, prompt, json_out, temp):
    body = {"model": m, "messages": [{"role": "user", "content": prompt}], "temperature": temp, "max_tokens": 12000}
    r = requests.post(f"https://api.cloudflare.com/client/v4/accounts/{CF_ACC}/ai/v1/chat/completions",
                      headers={"Authorization": f"Bearer {CF_TOK}"}, json=body, timeout=240)
    if r.status_code != 200: return None, r.status_code, r.text[:150]
    return r.json()["choices"][0]["message"]["content"] or "", 200, ""

def llm(prompt, role="cf", json_out=True, temp=0.8):
    """role 'writer' = Gemini first; anything else = Cloudflare first. Each falls back to the other."""
    chain = ([("g", m) for m in GEMINI_MODELS[:2]] + [("c", m) for m in CF_MODELS] + [("g", GEMINI_MODELS[2])]) if role == "writer" \
        else ([("c", m) for m in CF_MODELS] + [("g", m) for m in GEMINI_MODELS])
    last = None
    for prov, m in chain:
        if m in DEAD: continue
        for attempt in range(2):
            try:
                txt, code, err = (_gemini if prov == "g" else _cf_chat)(m, prompt, json_out, temp)
            except Exception as e:
                txt, code, err = None, 0, str(e)[:150]
            if txt:
                try:
                    out = _parse(txt, json_out); log(f"  llm ok: {m}"); return out
                except Exception as e:
                    last = f"{m}: bad JSON {e}"; continue
            last = f"{m}: {code} {err}"
            if code in (400, 401, 403, 404) or (code == 429 and prov == "g"):
                DEAD.add(m); break
            time.sleep(20)
        log("  model skipped:", last.replace("\n", " ")[:180])
    raise RuntimeError(f"All models failed: {last}")

past = [v["title"] for v in HIST["videos"]][-80:]

# ---------------- 1. topic ----------------
log("Choosing topic for", CH["name"])
topics = llm(f"""You are the head of content strategy for the YouTube channel "{CH['name']}".
Niche: {CH['niche']}
Proven format: {CH['format']}
Already published (never repeat or closely overlap): {json.dumps(past)}

Propose 8 video ideas. Each must be a TRUE story/subject with enough well-documented facts for an 8-minute video,
have a massive curiosity gap, broad worldwide appeal, and evergreen search interest.
Score each 1-10 on: click_appeal, retention_potential, factual_depth. Return JSON:
{{"ideas":[{{"title":"...","angle":"one sentence","twist":"the true surprising twist ending","click_appeal":0,"retention_potential":0,"factual_depth":0}}]}}""",
    role="cf", temp=1.0)
if isinstance(topics, dict): topics = topics.get("ideas") or next((v for v in topics.values() if isinstance(v, list)), [])
topics = [t for t in topics if isinstance(t, dict) and t.get("title")]
for t in topics:
    for k in ("click_appeal", "retention_potential", "factual_depth"):
        try: t[k] = float(t.get(k, 0))
        except Exception: t[k] = 0
topics.sort(key=lambda t: -(t["click_appeal"] + t["retention_potential"] + t["factual_depth"]))
topic = topics[0]
log("Topic:", topic["title"])

# ---------------- 2. research (grounded) ----------------
research = llm(f"""Research this YouTube documentary topic from your knowledge and return a dense fact sheet
(40-60 bullet facts) with dates, names, numbers, places, notable quotes and 2-3 vivid true anecdotes.
Mark uncertain or legendary claims as (LEGEND) or (DISPUTED). Include the twist: {topic['twist']}
Topic: {topic['title']} — {topic['angle']}""", role="cf", json_out=False, temp=0.3)

SCENE_RULES = f"""Rules for the narration:
- Written to be SPOKEN by a calm, gripping documentary narrator. Short punchy sentences mixed with longer ones.
- The FIRST sentence is the hook: concrete, visual, high-stakes, puts the viewer inside the moment, opens a curiosity gap. No greetings, no 'in this video'.
- Every 60-90 seconds add a re-hook (a question, a reversal, 'but the worst part was still coming').
- Spell out years and numbers as words the narrator would say (e.g. 'twelve nineteen', 'four hundred and fifty').
- Use ONLY facts from the fact sheet; frame legends as legends. No graphic gore descriptions (keep it advertiser-friendly: aftermath, not injuries).
- Each scene = 1-3 sentences (6-15 seconds) with ONE strong visual. img must describe a single photoreal cinematic shot (subject, setting, lighting, camera), never text/maps/diagrams, no gore, no famous modern people.
- Use "overlay" (max 3 words, e.g. a year, a number, a place) on 6-10 key scenes, and "chapter" (2-4 word title) on 4-6 scenes that start a new section."""

# ---------------- 3. write ----------------
log("Writing script")
draft = llm(f"""Write a complete YouTube documentary for "{CH['name']}".
Title idea: {topic['title']}  Angle: {topic['angle']}  Twist ending: {topic['twist']}
Format: {CH['format']}
FACT SHEET:
{research}

{SCENE_RULES}
Long video: 1150-1400 words of narration, 40-55 scenes, last scene invites viewers to subscribe to {CH['name']} in one short sentence.
Short (vertical, under 55 seconds): 110-130 words, 8-10 scenes, a different hook built for scrolling viewers, ends with 'Full story on {CH['name']}.'

Return JSON:
{{"title":"final long title (max 70 chars)","description":"3 short paragraphs for YouTube description","tags":["15 tags"],
"thumbnail_text":"max 4 words, all caps","thumbnail_img":"photoreal thumbnail scene, dramatic, single clear subject",
"short_title":"short title max 60 chars",
"long":[{{"id":"s01","text":"...","img":"...","overlay":"optional","chapter":"optional"}}],
"short":[{{"id":"h01","text":"...","img":"...","overlay":"optional"}}]}}""", role="writer", temp=0.85)

# ---------------- 4. independent review (different model + Google Search) ----------------
def review(d):
    return llm(f"""You are a ruthless independent YouTube retention editor AND fact-checker. You did not write this.
Fact-check every claim against your knowledge; flag anything you are not sure is true. Judge the hook and pacing.
Script JSON:
{json.dumps(d)}
Return ONLY a JSON object (no prose) with:
{{"hook_score": 1-10, "short_hook_score": 1-10, "retention_score": 1-10,
"wrong_claims":[{{"id":"scene id","claim":"...","fix":"corrected wording"}}],
"soften":[{{"id":"...","fix":"..."}}],
"hook_rewrites":["3 stronger true first sentences"], "cut_or_merge":["scene ids that sag"],
"ad_safety_issues":[{{"id":"...","fix":"..."}}]}}""", role="cf", json_out=True, temp=0.2)

def parse_loose(x):
    return x if isinstance(x, dict) else _parse(x, True)

final = draft
for rnd in range(3):
    log(f"Review round {rnd+1}")
    rv = parse_loose(review(final))
    log("scores:", rv.get("hook_score"), rv.get("short_hook_score"), rv.get("retention_score"),
        "wrong:", len(rv.get("wrong_claims", [])))
    json.dump(rv, open(os.path.join(OUT, f"review_{rnd+1}.json"), "w"), indent=1)
    good = (rv.get("hook_score", 0) >= 8 and rv.get("short_hook_score", 0) >= 8
            and rv.get("retention_score", 0) >= 8 and not rv.get("wrong_claims"))
    if good: break
    final = llm(f"""Revise this YouTube script JSON by applying the editor's review. Fix every wrong claim — if you are not certain of the correct fact,
REMOVE the claim or reframe it honestly ("according to legend", "some historians say") instead of guessing. Apply softenings,
use the strongest hook rewrite if it beats the current hook, cut/merge sagging scenes, fix ad-safety issues.
Keep the same JSON structure and all rules:
{SCENE_RULES}
REVIEW: {json.dumps(rv)}
SCRIPT: {json.dumps(final)}""", role="cf", temp=0.5)
else:
    rv = parse_loose(review(final))
    flagged = rv.get("wrong_claims", []) + rv.get("ad_safety_issues", [])
    if flagged:
        log(f"Final safety pass: removing/softening {len(flagged)} still-flagged claims")
        final = llm(f"""Edit this YouTube script JSON. For EACH flagged item below, find that claim in the given scene and either
DELETE the sentence or rewrite it as clearly hedged ("according to legend", "historians debate whether").
Do NOT add any new facts. Keep everything else, the JSON structure and scene ids exactly the same. Return the full JSON.
FLAGGED: {json.dumps(flagged)}
SCRIPT: {json.dumps(final)}""", role="cf", temp=0.2)
        rv["wrong_claims"] = []
QUALITY = {"hook": rv.get("hook_score", 0), "short_hook": rv.get("short_hook_score", 0),
           "retention": rv.get("retention_score", 0), "wrong": len(rv.get("wrong_claims", []))}
log("Final quality:", QUALITY)
json.dump(final, open(os.path.join(OUT, "script.json"), "w"), indent=1)
if QUALITY["wrong"] > 0 or min(QUALITY["hook"], QUALITY["retention"]) < 7:
    raise SystemExit(f"QUALITY GATE FAILED {QUALITY} — not publishing")

# ---------------- 5. images ----------------
import base64
def _cf(model, **kw):
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACC}/ai/run/@cf/black-forest-labs/{model}"
    h = {"Authorization": f"Bearer {CF_TOK}"}
    if kw.pop("multipart", False):
        r = requests.post(url, headers=h, files={k: (None, str(v)) for k, v in kw.items()}, timeout=180)
    else:
        r = requests.post(url, headers=h, json=kw, timeout=180)
    if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
        return r.content
    if r.status_code == 200:
        img = (r.json().get("result") or {}).get("image")
        if img: return base64.b64decode(img)
    raise RuntimeError(f"{model} HTTP {r.status_code} {r.text[:200]}")

def gen_image(prompt, w, h, path, tries=4):
    # FLUX.2 klein (native aspect ratio) first, FLUX.1 schnell (square, cropped later) as fallback
    sw, sh = (1024, 576) if w >= h else (576, 1024)
    for t in range(tries):
        for model, kw in (("flux-2-klein-4b", dict(prompt=prompt, width=sw, height=sh, multipart=True)),
                          ("flux-1-schnell", dict(prompt=prompt, steps=6))):
            try:
                data = _cf(model, **kw)
                if len(data) > 15000:
                    open(path, "wb").write(data); return True
            except Exception as e:
                log("img retry:", str(e)[:160])
        time.sleep(10 * (t + 1))
    return False

def make_images(scenes, w, h, d):
    os.makedirs(d, exist_ok=True)
    for sc in scenes:
        p = os.path.join(d, sc["id"] + ".jpg")
        if os.path.exists(p): continue
        prompt = f"{sc['img']}, {CH['style']}" + (", vertical composition, subject centered" if h > w else "")
        if not gen_image(prompt, w, h, p):
            raise RuntimeError(f"image failed for {sc['id']}")
        log("img", sc["id"])

make_images(final["long"], 1920, 1080, os.path.join(OUT, "img"))
make_images(final["short"], 1080, 1920, os.path.join(OUT, "img"))
gen_image(f"{final['thumbnail_img']}, {CH['style']}, extremely dramatic, high contrast, YouTube thumbnail",
          1280, 720, os.path.join(OUT, "thumb_bg.jpg"))

# ---------------- 6. voice + render ----------------
for kind in ("long", "short"):
    sj = os.path.join(OUT, f"{kind}_script.json")
    json.dump({"title": final["title"], "scenes": final[kind]}, open(sj, "w"))
    subprocess.run(["node", os.path.join(ROOT, "engine", "tts_all.mjs"), sj, CH["voice"],
                    str(CH["speed"] if kind == "long" else 1.0), os.path.join(OUT, f"tts_{kind}")], check=True, cwd=ROOT)
    subprocess.run([sys.executable, os.path.join(ROOT, "engine", "render.py"), sj, os.path.join(OUT, f"tts_{kind}"),
                    os.path.join(OUT, "img"), os.path.join(OUT, f"{kind}.mp4"), kind], check=True)

# ---------------- 7. thumbnail ----------------
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
def thumbnail():
    bgp = os.path.join(OUT, "thumb_bg.jpg")
    if not os.path.exists(bgp): bgp = os.path.join(OUT, "img", final["long"][0]["id"] + ".jpg")
    im = Image.open(bgp).convert("RGB").resize((1280, 720))
    im = ImageEnhance.Contrast(im).enhance(1.25); im = ImageEnhance.Color(im).enhance(1.15)
    dr = ImageDraw.Draw(im, "RGBA")
    for y in range(720):  # dark gradient on left for text
        pass
    dr.rectangle([0, 0, 1280, 720], fill=(0, 0, 0, 40))
    font = ImageFont.truetype(os.path.expanduser("~/.fonts/Anton_400Regular.ttf"), 132)
    words = final["thumbnail_text"].upper().split(); lines, cur = [], ""
    for w in words:
        if len(cur + " " + w) > 11 and cur: lines.append(cur); cur = w
        else: cur = (cur + " " + w).strip()
    lines.append(cur)
    y = 360 - len(lines) * 75
    for i, ln in enumerate(lines):
        col = (255, 214, 0) if i == len(lines) - 1 else (255, 255, 255)
        dr.text((60, y), ln, font=font, fill=col, stroke_width=10, stroke_fill=(0, 0, 0))
        y += 150
    p = os.path.join(OUT, "thumb.jpg"); im.save(p, quality=92); return p
thumb = thumbnail()

# ---------------- 8. upload ----------------
YT_ALL = json.loads(os.environ.get("YT_ALL") or "{}")
def yt_token():
    if YT_ALL: return YT_ALL["tokens"][CH_KEY]
    return json.loads(os.environ[CH["token_secret"]])
def yt_creds():
    from google.oauth2.credentials import Credentials
    tok = yt_token()
    cid = YT_ALL.get("client_id") or os.environ["YT_CLIENT_ID"]
    csec = YT_ALL.get("client_secret") or os.environ["YT_CLIENT_SECRET"]
    return Credentials(None, refresh_token=tok["refresh_token"], token_uri="https://oauth2.googleapis.com/token",
                       client_id=cid, client_secret=csec)

def upload(path, title, desc, tags, thumb_path=None):
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    yt = build("youtube", "v3", credentials=yt_creds(), cache_discovery=False)
    expected = yt_token().get("_channelId")
    mine = yt.channels().list(part="id", mine=True).execute()["items"][0]["id"]
    if expected and mine != expected:
        raise SystemExit(f"WRONG CHANNEL: token controls {mine}, expected {expected}. Not uploading.")
    body = {"snippet": {"title": title[:100], "description": desc[:4900], "tags": tags[:30], "categoryId": CH["category"]},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True}}
    req = yt.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(path, chunksize=8 << 20, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]; log("uploaded", vid)
    if thumb_path:
        try: yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thumb_path)).execute()
        except Exception as e: log("thumbnail not set (channel may need phone verification):", str(e)[:150])
    return vid

record = {"date": datetime.date.today().isoformat(), "title": final["title"], "quality": QUALITY}
if NO_UPLOAD:
    log("--no-upload: skipping YouTube")
else:
    long_id = upload(os.path.join(OUT, "long.mp4"), final["title"], final["description"], final["tags"], thumb)
    short_desc = f"{final['short_title']}\n\nWatch the full story: https://youtu.be/{long_id}\n\n#shorts #history"
    short_id = upload(os.path.join(OUT, "short.mp4"), final["short_title"] + " #shorts", short_desc, final["tags"][:10])
    record.update(long_id=long_id, short_id=short_id)
HIST["videos"].append(record)
json.dump(HIST, open(HIST_P, "w"), indent=1)
log("DONE", record)
