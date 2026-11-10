#!/usr/bin/env python3
"""Upload an already-rendered run: python engine/upload_only.py <channel-key> <dir with long.mp4/short.mp4/script.json/thumb.jpg>"""
import json, os, sys, datetime
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ch_key, d = sys.argv[1], sys.argv[2]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CH = json.load(open(os.path.join(ROOT, "channels.json")))[ch_key]
ALL = json.loads(os.environ["YT_ALL"]); tok = ALL["tokens"][ch_key]
creds = Credentials(None, refresh_token=tok["refresh_token"], token_uri="https://oauth2.googleapis.com/token",
                    client_id=ALL["client_id"], client_secret=ALL["client_secret"])
yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
mine = yt.channels().list(part="id,snippet", mine=True).execute()["items"][0]
if tok.get("_channelId") and mine["id"] != tok["_channelId"]:
    sys.exit(f"WRONG CHANNEL {mine['id']} != {tok['_channelId']}")
print("Uploading to channel:", mine["snippet"]["title"])
s = json.load(open(os.path.join(d, "script.json")))

def up(path, title, desc, tags):
    body = {"snippet": {"title": title[:100], "description": desc[:4900], "tags": tags[:30], "categoryId": CH["category"]},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True}}
    req = yt.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(path, chunksize=8 << 20, resumable=True))
    resp = None
    while resp is None: _, resp = req.next_chunk()
    print("uploaded", title, "->", "https://youtu.be/" + resp["id"]); return resp["id"]

long_id = up(os.path.join(d, "long.mp4"), s["title"], s["description"], s["tags"])
t = os.path.join(d, "thumb.jpg")
if os.path.exists(t):
    try: yt.thumbnails().set(videoId=long_id, media_body=MediaFileUpload(t)).execute(); print("thumbnail set")
    except Exception as e: print("thumbnail not set:", str(e)[:200])
short_id = up(os.path.join(d, "short.mp4"), s["short_title"] + " #shorts",
              f"{s['short_title']}\n\nWatch the full story: https://youtu.be/{long_id}\n\n#shorts", s["tags"][:10])
hp = os.path.join(ROOT, "data", f"{ch_key}.json"); os.makedirs(os.path.dirname(hp), exist_ok=True)
h = json.load(open(hp)) if os.path.exists(hp) else {"videos": []}
h["videos"].append({"date": datetime.date.today().isoformat(), "title": s["title"], "long_id": long_id, "short_id": short_id})
json.dump(h, open(hp, "w"), indent=1)
