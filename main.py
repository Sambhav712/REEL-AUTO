#!/usr/bin/env python3
"""Topic -> AI script -> AI voice -> Pexels stock clips -> edited 9:16 video -> Instagram Reel.

All config comes from environment variables (GitHub Secrets in Actions).
Set DRY_RUN=1 to build the video only (no upload).
"""
import asyncio
import datetime
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).parent


def load_dotenv(path):
    """Load simple KEY=VALUE pairs from .env without overriding existing env vars."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            os.environ.setdefault(k, v)


load_dotenv(ROOT / ".env")

# ---------------------------------------------------------------- config
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
IG_TOKEN = os.getenv("IG_TOKEN", "")
IG_USER_ID = os.getenv("IG_USER_ID", "")
CLOUDINARY_URL = os.getenv("CLOUDINARY_URL", "")  # cloudinary://KEY:SECRET@CLOUD
CLOUDINARY_CLIPS_FOLDER = os.getenv("CLOUDINARY_CLIPS_FOLDER", "reel-clips").strip("/")
LANGUAGE = os.getenv("LANGUAGE", "hi")  # hi or en
NICHE = os.getenv("NICHE", "Ujjain, Omkareshwar & Maa Baglamukhi pilgrimage travel (yatraone.co.in)")
DRY_RUN = os.getenv("DRY_RUN", "") == "1"
TOPIC_OVERRIDE = os.getenv("TOPIC", "").strip()

VOICES = {"hi": "hi-IN-SwaraNeural", "en": "en-US-GuyNeural"}
VOICE = os.getenv("VOICE", VOICES.get(LANGUAGE, VOICES["en"]))
LANG_NAME = {"hi": "Hindi (Devanagari script)", "en": "English"}.get(LANGUAGE, "English")
SUB_FONT = "Noto Sans Devanagari" if LANGUAGE == "hi" else "Noto Sans"

IG_API = "https://graph.instagram.com/v21.0"
W, H, FPS = 1080, 1920, 30

WORK = ROOT / "work"
OUT = ROOT / "output"


def log(*a):
    print("[reel-bot]", *a, flush=True)


def need(name, val):
    if not val:
        sys.exit(f"Missing required setting: {name}")


def http(method, url, retries=3, **kw):
    kw.setdefault("timeout", 60)
    last = None
    for i in range(retries):
        try:
            r = requests.request(method, url, **kw)
            if r.status_code < 500 and r.status_code != 429:
                return r
            last = r
        except requests.RequestException as e:
            last = e
        time.sleep(2 * (i + 1))
    if isinstance(last, requests.Response):
        return last
    raise RuntimeError(f"Request failed: {url}: {last}")


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(map(str, cmd))}\n{r.stderr[-1500:]}")
    return r.stdout


# ---------------------------------------------------------------- 1. topic
def pick_topic():
    if TOPIC_OVERRIDE:
        return TOPIC_OVERRIDE
    lines = [l.strip() for l in (ROOT / "topics.txt").read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.startswith("#")]
    if not lines:
        sys.exit("topics.txt is empty")
    day = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)).date().toordinal()
    return lines[day % len(lines)]


# ---------------------------------------------------------------- 2. research (Tavily) + script (Groq / Gemini)
DEFAULT_PROMPT = """You write scripts for 30-45 second Instagram Reels.
Niche: {niche}
Topic: {topic}
Narration language: {lang}
Research: {research}

Return ONLY JSON:
{{"title": "short catchy title (max 60 chars)",
  "caption": "1-2 sentence caption",
  "hashtags": ["#tag1", "#tag2", "... 6 to 8 tags"],
  "scenes": [{{"narration": "1-2 short spoken sentences", "keyword": "2-3 word ENGLISH stock-footage search term"}}]}}
Rules: 6-8 scenes, total {words} words. Scene 1 is a strong hook, last scene is a CTA."""


def load_prompt():
    p = ROOT / "system_prompt.txt"
    return p.read_text(encoding="utf-8") if p.is_file() else DEFAULT_PROMPT


def tavily_research(topic):
    if not TAVILY_API_KEY:
        return "None (use verified general knowledge only)."
    try:
        r = http("POST", "https://api.tavily.com/search", json={
            "api_key": TAVILY_API_KEY,
            "query": f"{topic} yatraone.co.in Ujjain Omkareshwar Baglamukhi",
            "search_depth": "basic",
            "include_answer": True,
            "max_results": 4,
        }, timeout=30)
        if r.status_code != 200:
            log(f"Tavily skipped ({r.status_code})")
            return "None (use verified general knowledge only)."
        j = r.json()
        parts = [j["answer"]] if j.get("answer") else []
        for res in j.get("results", [])[:3]:
            if res.get("content"):
                parts.append(f"- {res['content'][:260]}")
        log("Tavily research fetched")
        return "\n".join(parts) or "None"
    except Exception as e:
        log("Tavily skipped:", e)
        return "None (use verified general knowledge only)."


def _gen_groq(prompt_text):
    r = http("POST", "https://api.groq.com/openai/v1/chat/completions",
             headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
             json={
                 "model": GROQ_MODEL,
                 "messages": [{"role": "user", "content": prompt_text}],
                 "response_format": {"type": "json_object"},
                 "temperature": 0.8,
             }, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"Groq error {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"]


def _gen_gemini(prompt_text):
    body = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8},
    }
    models = list(dict.fromkeys([GEMINI_MODEL, "gemini-3.1-flash-lite"]))
    r = None
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        r = http("POST", url, params={"key": GEMINI_API_KEY}, json=body, timeout=90)
        if r.status_code == 200:
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        log(f"Gemini {model} failed ({r.status_code}), trying fallback...")
    raise RuntimeError(f"Gemini error {r.status_code}: {r.text[:300]}")


def gen_script(topic):
    if not GROQ_API_KEY and not GEMINI_API_KEY:
        sys.exit("Missing required setting: GROQ_API_KEY or GEMINI_API_KEY")
    research = tavily_research(topic)
    prompt_text = load_prompt().format(
        niche=NICHE, topic=topic, lang=LANG_NAME,
        words=70 if LANGUAGE == "hi" else 85, research=research,
    )
    providers = []
    if GROQ_API_KEY:
        providers.append(("Groq", _gen_groq))
    if GEMINI_API_KEY:
        providers.append(("Gemini", _gen_gemini))

    text, last_err = None, None
    for name, fn in providers:
        try:
            text = fn(prompt_text)
            log(f"Script generated via {name}")
            break
        except Exception as e:
            last_err = e
            log(f"{name} failed: {e}")
    if not text:
        raise RuntimeError(f"All LLM providers failed: {last_err}")

    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    data = json.loads(text)
    scenes = [s for s in data["scenes"] if s.get("narration") and s.get("keyword")]
    if len(scenes) < 3:
        raise RuntimeError("LLM returned too few scenes")
    data["scenes"] = scenes[:9]
    return data


# ---------------------------------------------------------------- 3. voice (edge-tts)
async def _tts(text, path):
    import edge_tts
    await edge_tts.Communicate(text, VOICE).save(str(path))


def duration(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                      "-of", "csv=p=0", str(path)]).strip())


def make_voice(scenes):
    for i, s in enumerate(scenes):
        p = WORK / f"voice_{i}.mp3"
        try:
            asyncio.run(_tts(s["narration"], p))
        except Exception as e:
            if i == 0:
                log(f"edge-tts failed ({e}), using gTTS fallback...")
            from gtts import gTTS
            gTTS(text=s["narration"], lang=LANGUAGE if LANGUAGE in ("hi", "en") else "en").save(str(p))
        s["audio"] = p
        s["dur"] = duration(p) + 0.25  # tiny pause between scenes
    return scenes


# ---------------------------------------------------------------- 4. footage (Pixabay / Pexels)
def _download_clip(url, idx):
    path = WORK / f"raw_{idx}.mp4"
    with http("GET", url, stream=True, timeout=120) as resp:
        if resp.status_code != 200:
            return None
        with open(path, "wb") as fh:
            for chunk in resp.iter_content(1 << 20):
                fh.write(chunk)
    return path


def _pick_pixabay(hit):
    vids = hit.get("videos") or {}
    candidates = [v for v in vids.values() if isinstance(v, dict) and v.get("url")]
    ok = [v for v in candidates if (v.get("width") or 0) >= 720] or candidates
    ok.sort(key=lambda v: v.get("width") or 0)
    return ok[0]["url"] if ok else None


def _fetch_pixabay(tries, used, idx):
    for q in tries:
        r = http("GET", "https://pixabay.com/api/videos/",
                 params={"key": PIXABAY_API_KEY, "q": q, "per_page": 15, "safesearch": "true"})
        if r.status_code != 200:
            continue
        hits = [h for h in r.json().get("hits", []) if f"px:{h['id']}" not in used]
        random.shuffle(hits)
        for h in hits:
            url = _pick_pixabay(h)
            if not url:
                continue
            path = _download_clip(url, idx)
            if path:
                used.add(f"px:{h['id']}")
                return path
    return None


def pick_file(video):
    files = [f for f in video.get("video_files", []) if f.get("file_type") == "video/mp4"]
    files = [f for f in files if (f.get("height") or 0) >= (f.get("width") or 0)] or files
    ok = [f for f in files if (f.get("width") or 0) >= 720] or files
    ok.sort(key=lambda f: f.get("width") or 0)
    return ok[0] if ok else None


def _fetch_pexels(tries, used, idx):
    for q in tries:
        r = http("GET", "https://api.pexels.com/videos/search",
                 headers={"Authorization": PEXELS_API_KEY},
                 params={"query": q, "orientation": "portrait", "per_page": 15})
        if r.status_code != 200:
            continue
        vids = [v for v in r.json().get("videos", []) if v["id"] not in used]
        random.shuffle(vids)
        for v in vids:
            f = pick_file(v)
            if not f:
                continue
            path = _download_clip(f["link"], idx)
            if path:
                used.add(v["id"])
                return path
    return None


_CLOUD_CLIPS_CACHE = None


def _list_cloudinary_clips():
    global _CLOUD_CLIPS_CACHE
    if _CLOUD_CLIPS_CACHE is not None:
        return _CLOUD_CLIPS_CACHE
    if not CLOUDINARY_URL:
        _CLOUD_CLIPS_CACHE = []
        return _CLOUD_CLIPS_CACHE
    try:
        key, secret, cloud = cloudinary_creds()
        params = {"type": "upload", "max_results": 100}
        if CLOUDINARY_CLIPS_FOLDER:
            params["prefix"] = f"{CLOUDINARY_CLIPS_FOLDER}/"
        r = http("GET", f"https://api.cloudinary.com/v1_1/{cloud}/resources/video",
                 auth=(key, secret), params=params, timeout=30)
        if r.status_code != 200:
            log(f"Cloudinary clips list skipped ({r.status_code})")
            _CLOUD_CLIPS_CACHE = []
            return _CLOUD_CLIPS_CACHE
        res = [v for v in r.json().get("resources", [])
               if not v["public_id"].startswith("reel_") and v.get("secure_url")]
        _CLOUD_CLIPS_CACHE = res
        if res:
            log(f"Found {len(res)} custom clips in Cloudinary")
        return res
    except Exception as e:
        log("Cloudinary clips list skipped:", e)
        _CLOUD_CLIPS_CACHE = []
        return _CLOUD_CLIPS_CACHE


def _fetch_cloudinary(keyword, used, idx):
    clips = [c for c in _list_cloudinary_clips() if f"cl:{c['public_id']}" not in used]
    if not clips:
        return None
    words = [w.lower() for w in re.findall(r"[a-zA-Z0-9]+", keyword) if len(w) > 2]
    matched = [c for c in clips if any(w in c["public_id"].lower() for w in words)]
    pool = matched or clips
    random.shuffle(pool)
    for c in pool:
        path = _download_clip(c["secure_url"], idx)
        if path:
            used.add(f"cl:{c['public_id']}")
            return path
    return None


def fetch_clip(keyword, used, idx):
    if CLOUDINARY_URL:
        path = _fetch_cloudinary(keyword, used, idx)
        if path:
            return path
    if not PIXABAY_API_KEY and not PEXELS_API_KEY:
        sys.exit("Missing stock clip source: upload clips to Cloudinary or set PIXABAY_API_KEY / PEXELS_API_KEY")
    tries = [keyword, keyword.split()[0], "hindu temple", "india", "nature"]
    if PIXABAY_API_KEY:
        path = _fetch_pixabay(tries, used, idx)
        if path:
            return path
    if PEXELS_API_KEY:
        path = _fetch_pexels(tries, used, idx)
        if path:
            return path
    raise RuntimeError(f"No stock clip found for '{keyword}'")


# ---------------------------------------------------------------- 5. edit (FFmpeg)
def fmt_ts(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def chunk_words(text, n=4):
    words = text.split()
    return [" ".join(words[i:i + n]) for i in range(0, len(words), n)] or [text]


def write_srt(scenes, path):
    lines, t0, k = [], 0.0, 1
    for s in scenes:
        chunks = chunk_words(s["narration"])
        total = sum(len(c) for c in chunks)
        speak = s["dur"] - 0.25
        t = t0
        for c in chunks:
            d = speak * len(c) / total
            lines.append(f"{k}\n{fmt_ts(t)} --> {fmt_ts(t + d)}\n{c}\n")
            t += d
            k += 1
        t0 += s["dur"]
    path.write_text("\n".join(lines), encoding="utf-8")


def assemble(scenes, out_path):
    """scenes: list of dicts with audio (mp3), clip (mp4), dur (sec), narration."""
    parts = []
    for i, s in enumerate(scenes):
        seg = WORK / f"seg_{i}.mp4"
        run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(s["clip"]), "-t", f"{s['dur']:.3f}",
             "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1",
             "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
             str(seg)])
        parts.append(seg)

    vlist = WORK / "v.txt"
    vlist.write_text("".join(f"file '{p.name}'\n" for p in parts))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "v.txt", "-c", "copy", "video_all.mp4"], cwd=WORK)

    # voice: pad each scene's audio to its scene length, then concat
    alist = []
    for i, s in enumerate(scenes):
        padded = WORK / f"pad_{i}.m4a"
        run(["ffmpeg", "-y", "-i", str(s["audio"]), "-af", "apad", "-t", f"{s['dur']:.3f}",
             "-ar", "44100", "-ac", "2", "-c:a", "aac", "-b:a", "160k", str(padded)])
        alist.append(padded)
    (WORK / "a.txt").write_text("".join(f"file '{p.name}'\n" for p in alist))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "a.txt", "-c", "copy", "audio_all.m4a"], cwd=WORK)

    write_srt(scenes, WORK / "captions.srt")
    style = (f"FontName={SUB_FONT},FontSize=14,Bold=1,PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=1,Alignment=2,MarginV=95")
    run(["ffmpeg", "-y", "-i", "video_all.mp4", "-i", "audio_all.m4a",
         "-vf", f"subtitles=captions.srt:force_style='{style}'",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart",
         str(out_path.resolve())], cwd=WORK)
    return out_path


# ---------------------------------------------------------------- 6. host video (Cloudinary)
def cloudinary_creds():
    need("CLOUDINARY_URL", CLOUDINARY_URL)
    u = urlparse(CLOUDINARY_URL)
    return u.username, u.password, u.hostname


def _sign(params, secret):
    s = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hashlib.sha1((s + secret).encode()).hexdigest()


def cloud_upload(path):
    key, secret, cloud = cloudinary_creds()
    params = {"timestamp": int(time.time()), "public_id": f"reel_{int(time.time())}"}
    data = dict(params, api_key=key, signature=_sign(params, secret))
    with open(path, "rb") as fh:
        r = requests.post(f"https://api.cloudinary.com/v1_1/{cloud}/video/upload",
                          data=data, files={"file": fh}, timeout=300)
    if r.status_code != 200:
        raise RuntimeError(f"Cloudinary error {r.status_code}: {r.text[:400]}")
    j = r.json()
    return j["secure_url"], j["public_id"]


def cloud_delete(public_id):
    try:
        key, secret, cloud = cloudinary_creds()
        params = {"public_id": public_id, "timestamp": int(time.time())}
        requests.post(f"https://api.cloudinary.com/v1_1/{cloud}/video/destroy",
                      data=dict(params, api_key=key, signature=_sign(params, secret)), timeout=60)
    except Exception as e:  # best effort cleanup
        log("Cloudinary cleanup skipped:", e)


# ---------------------------------------------------------------- 7. Instagram
def ig_publish(video_url, caption):
    need("IG_TOKEN", IG_TOKEN)
    need("IG_USER_ID", IG_USER_ID)
    r = http("POST", f"{IG_API}/{IG_USER_ID}/media", data={
        "media_type": "REELS", "video_url": video_url,
        "caption": caption, "access_token": IG_TOKEN})
    if r.status_code != 200:
        raise RuntimeError(f"IG container error {r.status_code}: {r.text[:500]}")
    cid = r.json()["id"]
    log("IG container", cid)

    for _ in range(40):  # up to ~7 min
        time.sleep(10)
        s = http("GET", f"{IG_API}/{cid}", params={"fields": "status_code,status", "access_token": IG_TOKEN}).json()
        code = s.get("status_code")
        log("IG status:", code)
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"IG processing failed: {s}")
    else:
        raise RuntimeError("IG processing timed out")

    p = http("POST", f"{IG_API}/{IG_USER_ID}/media_publish",
             data={"creation_id": cid, "access_token": IG_TOKEN})
    if p.status_code != 200:
        raise RuntimeError(f"IG publish error {p.status_code}: {p.text[:500]}")
    return p.json()["id"]


def refresh_ig_token():
    """Long-lived tokens last 60 days; refresh each run and hand the new one to the workflow."""
    if not IG_TOKEN:
        return
    try:
        r = requests.get("https://graph.instagram.com/refresh_access_token",
                         params={"grant_type": "ig_refresh_token", "access_token": IG_TOKEN}, timeout=30)
        if r.status_code == 200 and r.json().get("access_token"):
            (ROOT / "new_token.txt").write_text(r.json()["access_token"])
            log("IG token refreshed")
        else:
            log("IG token not refreshed (normal if token is <24h old):", r.status_code)
    except Exception as e:
        log("IG token refresh skipped:", e)


# ---------------------------------------------------------------- main
def main():
    WORK.mkdir(exist_ok=True)
    OUT.mkdir(exist_ok=True)

    topic = pick_topic()
    log("Topic:", topic)

    script = gen_script(topic)
    scenes = script["scenes"]
    log("Title:", script["title"], "| scenes:", len(scenes))

    make_voice(scenes)
    used = set()
    for i, s in enumerate(scenes):
        s["clip"] = fetch_clip(s["keyword"], used, i)

    final = OUT / "final.mp4"
    assemble(scenes, final)
    total = sum(s["dur"] for s in scenes)
    log(f"Video ready: {final} ({total:.0f}s)")

    caption = f"{script['title']}\n\n{script.get('caption', '')}\n\n{' '.join(script.get('hashtags', []))}"
    (OUT / "caption.txt").write_text(caption, encoding="utf-8")

    if DRY_RUN:
        log("DRY_RUN=1, skipping upload.")
        return

    url, public_id = cloud_upload(final)
    log("Hosted at", url)
    try:
        media_id = ig_publish(url, caption[:2200])
        log("Published to Instagram, media id", media_id)
    finally:
        cloud_delete(public_id)
        refresh_ig_token()


if __name__ == "__main__":
    main()
