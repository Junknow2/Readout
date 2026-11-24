import asyncio
import base64
import hashlib
import html
import os
import secrets
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Optional

import requests
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mutagen.mp3 import MP3
from pydantic import BaseModel, HttpUrl

import poc_pipeline
import queue_manager
import feed_manager


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

# Auth configuration (mirrors Matrix / LifeLog)
READOUT_USERNAME = os.getenv("READOUT_USERNAME") or os.getenv("DASHBOARD_USERNAME")
READOUT_PASSWORD = os.getenv("READOUT_PASSWORD") or os.getenv("DASHBOARD_PASSWORD")

app = FastAPI(title="Readout", description="Marathon / Running Audio Queue & PWA")

# CORS for local dev / PWA
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Authentication & Security Middleware
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.scope.get("path", "")
    set_cookie_needed = False
    expected_token = None

    if READOUT_USERNAME and READOUT_PASSWORD:
        exempt_paths = [
            "/manifest.json",
            "/apple-touch-icon.png",
            "/apple-touch-icon-precomposed.png",
            "/favicon.ico",
            "/favicon.png",
            "/sw.js",
        ]
        if path not in exempt_paths and not path.startswith("/static"):
            authenticated = False
            expected_token = hashlib.sha256(
                f"{READOUT_USERNAME}:{READOUT_PASSWORD}:readout_cookie_salt_2026".encode("utf-8")
            ).hexdigest()

            # A. Check cookie (1-year persistent session)
            cookie_val = request.cookies.get("readout_auth")
            if cookie_val and secrets.compare_digest(cookie_val, expected_token):
                authenticated = True

            # B. Check HTTP Basic Auth header
            if not authenticated:
                auth_header = request.headers.get("authorization", "")
                if auth_header.startswith("Basic "):
                    try:
                        encoded_creds = auth_header[6:].strip()
                        decoded_creds = base64.b64decode(encoded_creds).decode("utf-8")
                        if ":" in decoded_creds:
                            u, p = decoded_creds.split(":", 1)
                            user_ok = secrets.compare_digest(u, READOUT_USERNAME)
                            pass_ok = secrets.compare_digest(p, READOUT_PASSWORD)
                            if user_ok and pass_ok:
                                authenticated = True
                                set_cookie_needed = True
                    except Exception:
                        pass

            if not authenticated:
                return Response(
                    content="Unauthorized: Access to Readout requires authentication.\n",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Readout", charset="UTF-8"'},
                    media_type="text/plain"
                )

    response = await call_next(request)

    # Issue 1-year persistent session cookie on successful Basic Auth
    if set_cookie_needed and expected_token:
        response.set_cookie(
            key="readout_auth",
            value=expected_token,
            max_age=31536000,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/"
        )

    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Templates
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


class AddQueueRequest(BaseModel):
    url: str
    voice: Optional[str] = None
    model: Optional[str] = None


class AddFeedRequest(BaseModel):
    url: str


def is_pdf_url(url: str) -> bool:
    """Check if a URL points directly to a PDF resource."""
    clean = url.split("?")[0].split("#")[0].lower()
    if clean.endswith(".pdf"):
        return True
    try:
        resp = requests.head(
            url,
            allow_redirects=True,
            timeout=5,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        content_type = resp.headers.get("content-type", "").lower()
        if "application/pdf" in content_type:
            return True
    except Exception:
        pass
    return False


async def process_item_task(item_id: str, voice: Optional[str] = None, model: Optional[str] = None):
    """Background task to extract content (web or PDF), rewrite with Gemini, and generate MP3."""
    item = queue_manager.get_item(item_id)
    if not item:
        return

    queue_manager.update_item(item_id, status="processing")
    url = item.get("url", "")
    chosen_voice = voice or poc_pipeline.DEFAULT_VOICE
    chosen_model = model or poc_pipeline.DEFAULT_MODEL

    try:
        is_pdf = (
            item.get("item_type") == "pdf"
            or url.startswith("upload:")
            or (queue_manager.PDF_DIR / f"{item_id}.pdf").exists()
            or is_pdf_url(url)
        )

        if is_pdf:
            print(f"[*] Processing PDF item {item_id}: {url}")
            pdf_file = queue_manager.PDF_DIR / f"{item_id}.pdf"
            if pdf_file.exists():
                pdf_bytes = pdf_file.read_bytes()
                orig_name = item.get("original_filename") or item.get("title") or "document.pdf"
            else:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                resp = requests.get(url, headers=headers, timeout=45)
                resp.raise_for_status()
                pdf_bytes = resp.content
                orig_name = url.split("/")[-1].split("?")[0] or "document.pdf"

            rewritten = poc_pipeline.rewrite_pdf_for_audio(
                pdf_bytes,
                filename=orig_name,
                model_name=chosen_model
            )
            title = rewritten["title"]
            script_text = rewritten["script_text"]
            page_count = 1
            queue_manager.update_item(item_id, title=title, page_count=page_count, item_type="pdf")
        else:
            # Step A: Web Extract
            extracted = poc_pipeline.extract_content_from_url(url)
            title = extracted["title"]
            if (not title or title == "無題の記事") and item.get("title") and item.get("title") != "URLを解析中...":
                title = item["title"]
            page_count = extracted.get("page_count", 1)
            queue_manager.update_item(item_id, title=title, page_count=page_count, item_type="web")

            # Step B: Rewrite
            script_text = poc_pipeline.rewrite_for_audio(
                title,
                extracted["content"],
                site_name=extracted.get("site_name", ""),
                model_name=chosen_model
            )

        # Save script text
        audio_filename = item["audio_filename"] or f"{item_id}.mp3"
        mp3_path = OUTPUT_DIR / audio_filename
        txt_path = mp3_path.with_suffix(".txt")
        txt_path.write_text(script_text, encoding="utf-8")

        # Step C: TTS
        await poc_pipeline.generate_speech_async(script_text, mp3_path, voice=chosen_voice)

        # Measure audio duration
        duration_sec = 0.0
        try:
            audio_info = MP3(str(mp3_path))
            duration_sec = round(audio_info.info.length, 1)
        except Exception as e:
            print(f"Warning: Failed to measure audio duration for {mp3_path}: {e}")
            # Fallback estimation: ~5 chars per second for Japanese speech
            duration_sec = round(len(script_text) / 5.5, 1)

        # Summary preview: First 150 chars
        summary = script_text[:150].replace("\n", " ").strip()
        if len(script_text) > 150:
            summary += "..."

        queue_manager.update_item(
            item_id,
            title=title,
            summary=summary,
            audio_duration_sec=duration_sec,
            status="ready",
            error_message=None,
            page_count=page_count,
        )
        print(f"[+] Task completed for {item_id}: {title} ({duration_sec}s, {page_count} pages)")

    except Exception as e:
        print(f"[-] Processing failed for item {item_id}: {e}")
        queue_manager.update_item(
            item_id,
            status="failed",
            error_message=str(e),
        )


@app.on_event("startup")
def startup_event():
    queue_manager.ensure_dirs()
    feed_manager.ensure_dirs()



@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    """PWA main UI."""
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/manifest.json")
async def manifest():
    """PWA Web App Manifest."""
    manifest_file = STATIC_DIR / "manifest.json"
    if manifest_file.exists():
        return FileResponse(manifest_file, media_type="application/manifest+json")
    raise HTTPException(status_code=404, detail="Manifest not found")


@app.get("/sw.js")
async def service_worker():
    """Service Worker file served from root to allow '/' scope."""
    sw_file = STATIC_DIR / "sw.js"
    if sw_file.exists():
        return FileResponse(
            sw_file,
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/"},
        )
    raise HTTPException(status_code=404, detail="Service worker not found")


@app.get("/api/queue")
async def get_queue():
    """List all active queue items (not listened)."""
    return queue_manager.get_active_queue()


@app.post("/api/queue")
async def add_to_queue(payload: AddQueueRequest, background_tasks: BackgroundTasks):
    """Add a new URL to the queue (HTML article or PDF URL) and trigger async pipeline."""
    url = str(payload.url).strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid URL scheme. Must start with http:// or https://")

    is_pdf = is_pdf_url(url)
    item_type = "pdf" if is_pdf else "web"
    initial_title = Path(url.split("?")[0]).stem if is_pdf else None

    item = queue_manager.add_item(
        url=url,
        title=initial_title,
        item_type=item_type
    )
    background_tasks.add_task(
        process_item_task,
        item_id=item["id"],
        voice=payload.voice,
        model=payload.model,
    )
    return item


@app.post("/api/upload-pdf")
async def upload_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    voice: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
):
    """Upload a PDF file and enqueue it for audio synthesis."""
    filename = file.filename or "uploaded.pdf"
    if not filename.lower().endswith(".pdf") and (file.content_type and "pdf" not in file.content_type.lower()):
        raise HTTPException(status_code=400, detail="PDFファイル（.pdf）を指定してください。")

    content = await file.read()
    if not content or len(content) == 0:
        raise HTTPException(status_code=400, detail="アップロードされたファイルが空です。")

    # Limit to 50MB
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="ファイルサイズが上限（50MB）を超えています。")

    clean_stem = Path(filename).stem.replace("_", " ").strip()
    initial_title = f"{clean_stem}"

    item = queue_manager.add_item(
        url=f"upload:{filename}",
        title=initial_title,
        item_type="pdf",
        original_filename=filename,
    )

    # Save uploaded PDF to data/pdfs/{item_id}.pdf
    pdf_path = queue_manager.PDF_DIR / f"{item['id']}.pdf"
    pdf_path.write_bytes(content)

    background_tasks.add_task(
        process_item_task,
        item_id=item["id"],
        voice=voice,
        model=model,
    )

    return item


@app.get("/pdf/{item_id}.pdf")
async def get_pdf_file(item_id: str):
    """Serve uploaded PDF file."""
    pdf_file = queue_manager.PDF_DIR / f"{item_id}.pdf"
    if not pdf_file.exists():
        raise HTTPException(status_code=404, detail="PDF not found")
    return FileResponse(
        pdf_file,
        media_type="application/pdf",
        filename=f"{item_id}.pdf"
    )


HTML_POPUP_SUCCESS = """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Readout - 登録完了</title>
  <style>
    :root {
      --bg: #0A0A0E;
      --surface: #14141B;
      --border: #2A2A38;
      --neon-yellow: #CCFF00;
      --text: #FFFFFF;
      --text-muted: #9E9EB2;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      height: 100vh;
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: center;
      padding: 16px;
      overflow: hidden;
      user-select: none;
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 18px 20px;
      width: 100%;
      max-width: 360px;
      text-align: center;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 10px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.6), 0 0 15px rgba(204, 255, 0, 0.12);
    }
    .icon-badge {
      width: 44px;
      height: 44px;
      border-radius: 50%;
      background: rgba(204, 255, 0, 0.12);
      border: 2px solid var(--neon-yellow);
      display: flex;
      justify-content: center;
      align-items: center;
      color: var(--neon-yellow);
      animation: popIn 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275);
    }
    @keyframes popIn {
      0% { transform: scale(0.5); opacity: 0; }
      100% { transform: scale(1); opacity: 1; }
    }
    .title {
      font-size: 1.05rem;
      font-weight: 800;
      color: var(--neon-yellow);
      letter-spacing: -0.3px;
    }
    .article-title {
      font-size: 0.82rem;
      color: var(--text);
      max-width: 100%;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      opacity: 0.9;
    }
    .subtext {
      font-size: 0.75rem;
      color: var(--text-muted);
    }
    .progress-bar-wrap {
      width: 100%;
      height: 3px;
      background: rgba(255, 255, 255, 0.1);
      border-radius: 2px;
      overflow: hidden;
      margin-top: 2px;
    }
    .progress-bar-fill {
      height: 100%;
      background: var(--neon-yellow);
      width: 100%;
      animation: countdown 1.5s linear forwards;
    }
    @keyframes countdown {
      from { width: 100%; }
      to { width: 0%; }
    }
    .close-btn {
      margin-top: 2px;
      background: none;
      border: none;
      color: var(--text-muted);
      font-size: 0.75rem;
      cursor: pointer;
      text-decoration: underline;
    }
    .close-btn:hover {
      color: var(--text);
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon-badge">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
        <path d="M3 18v-6a9 9 0 0 1 18 0v6"></path>
        <path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"></path>
      </svg>
    </div>
    <div class="title">🎧 Readout に追加しました！</div>
    <div class="article-title">{display_title}</div>
    <div class="subtext">(1.5秒後に自動で閉じます)</div>
    <div class="progress-bar-wrap">
      <div class="progress-bar-fill"></div>
    </div>
    <button class="close-btn" onclick="window.close()">今すぐ閉じる</button>
  </div>
  <script>
    setTimeout(function() {
      window.close();
    }, 1500);
  </script>
</body>
</html>
"""

HTML_POPUP_ERROR = """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Readout - エラー</title>
  <style>
    :root {
      --bg: #0A0A0E;
      --surface: #14141B;
      --border: #2A2A38;
      --danger: #FF3366;
      --text: #FFFFFF;
      --text-muted: #9E9EB2;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      height: 100vh;
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: center;
      padding: 16px;
      overflow: hidden;
      user-select: none;
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 18px 20px;
      width: 100%;
      max-width: 360px;
      text-align: center;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 10px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.6);
    }
    .icon-badge {
      width: 44px;
      height: 44px;
      border-radius: 50%;
      background: rgba(255, 51, 102, 0.15);
      border: 2px solid var(--danger);
      display: flex;
      justify-content: center;
      align-items: center;
      color: var(--danger);
    }
    .title {
      font-size: 1.05rem;
      font-weight: 800;
      color: var(--danger);
    }
    .message {
      font-size: 0.82rem;
      color: var(--text-muted);
    }
    .btn {
      margin-top: 6px;
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--text);
      font-size: 0.8rem;
      padding: 6px 16px;
      border-radius: 8px;
      cursor: pointer;
    }
    .btn:hover {
      background: rgba(255, 255, 255, 0.1);
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon-badge">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
        <circle cx="12" cy="12" r="10"></circle>
        <line x1="12" y1="8" x2="12" y2="12"></line>
        <line x1="12" y1="16" x2="12.01" y2="16"></line>
      </svg>
    </div>
    <div class="title">追加できませんでした</div>
    <div class="message">{error_message}</div>
    <button class="btn" onclick="window.close()">閉じる</button>
  </div>
</body>
</html>
"""


@app.get("/api/bookmarklet", response_class=HTMLResponse)
async def add_via_bookmarklet(
    url: str,
    background_tasks: BackgroundTasks,
    title: Optional[str] = None,
    voice: Optional[str] = None,
    model: Optional[str] = None,
):
    """Add a new URL via 1-click bookmarklet popup and return auto-closing confirmation page."""
    clean_url = str(url).strip()
    if not clean_url.startswith("http://") and not clean_url.startswith("https://"):
        safe_msg = html.escape("無効なURL形式です。http:// または https:// で始まるURLを指定してください。")
        return HTMLResponse(
            status_code=400,
            content=HTML_POPUP_ERROR.replace("{error_message}", safe_msg),
        )

    clean_title = title.strip() if title else ""
    is_pdf = is_pdf_url(clean_url)
    item_type = "pdf" if is_pdf else "web"
    item = queue_manager.add_item(
        url=clean_url,
        title=clean_title or (Path(clean_url.split("?")[0]).stem if is_pdf else None),
        item_type=item_type,
    )
    background_tasks.add_task(
        process_item_task,
        item_id=item["id"],
        voice=voice,
        model=model,
    )

    display_title = html.escape(clean_title or clean_url)
    html_content = HTML_POPUP_SUCCESS.replace("{display_title}", display_title)
    return HTMLResponse(content=html_content)


@app.post("/api/queue/{item_id}/listened")
async def mark_item_listened(item_id: str):
    """Mark an item as listened (removes from active queue)."""
    success = queue_manager.mark_listened(item_id)
    if not success:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"success": True, "id": item_id}


@app.delete("/api/queue/{item_id}")
async def delete_queue_item(item_id: str):
    """Delete an item from the queue and remove associated files."""
    success = queue_manager.delete_item(item_id)
    if not success:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"success": True, "id": item_id}


# --- RSS Feed & Article Endpoints ---

@app.get("/api/feeds")
async def get_feeds():
    """List all subscribed RSS/Atom feeds."""
    return feed_manager.get_feeds()


@app.post("/api/feeds")
async def add_feed(payload: AddFeedRequest):
    """Subscribe to a new RSS/Atom feed."""
    url = str(payload.url).strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid URL scheme. Must start with http:// or https://")
    try:
        feed = feed_manager.add_feed(url)
        return feed
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse feed: {e}")


@app.delete("/api/feeds/{feed_id}")
async def delete_feed(feed_id: str):
    """Unsubscribe from a feed."""
    success = feed_manager.delete_feed(feed_id)
    if not success:
        raise HTTPException(status_code=404, detail="Feed not found")
    return {"success": True, "id": feed_id}


@app.get("/api/articles")
async def get_articles(limit: int = 40):
    """Get aggregated timeline of latest articles from all subscribed feeds."""
    try:
        articles = feed_manager.fetch_all_articles(max_articles=limit)
        return articles
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch articles: {e}")



@app.get("/audio/{filename}")
async def get_audio_file(filename: str):
    """Stream audio MP3 file with HTTP Range support for seeking."""
    file_path = OUTPUT_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")
    return FileResponse(
        file_path,
        media_type="audio/mpeg",
        filename=filename,
        headers={"Accept-Ranges": "bytes"},
    )


@app.get("/feed.xml")
async def podcast_feed(request: Request):
    """Generate RSS 2.0 Podcast feed for Pocket Casts, Apple Podcasts, etc."""
    base_url = str(request.base_url).rstrip("/")
    items = queue_manager.get_active_queue()
    ready_items = [it for it in items if it.get("status") == "ready"]

    # Current RFC 822 / 2822 date
    now_rfc = format_datetime(datetime.now(timezone.utc))

    xml_items = []
    for it in ready_items:
        item_id = it.get("id")
        title = it.get("title", "無題のエピソード")
        summary = it.get("summary", "")
        filename = it.get("audio_filename", f"{item_id}.mp3")
        audio_url = f"{base_url}/audio/{filename}"
        mp3_file = OUTPUT_DIR / filename
        file_size = mp3_file.stat().st_size if mp3_file.exists() else 0
        duration_sec = int(it.get("audio_duration_sec", 0))

        # PubDate
        try:
            created_dt = datetime.fromisoformat(it.get("created_at"))
            pub_date = format_datetime(created_dt)
        except Exception:
            pub_date = now_rfc

        # Format duration HH:MM:SS
        mins, secs = divmod(duration_sec, 60)
        hours, mins = divmod(mins, 60)
        duration_str = f"{hours:02d}:{mins:02d}:{secs:02d}" if hours else f"{mins:02d}:{secs:02d}"

        xml_items.append(f"""    <item>
      <title><![CDATA[{title}]]></title>
      <description><![CDATA[{summary}]]></description>
      <link>{it.get('url', base_url)}</link>
      <guid isPermaLink="false">{item_id}</guid>
      <pubDate>{pub_date}</pubDate>
      <enclosure url="{audio_url}" length="{file_size}" type="audio/mpeg" />
      <itunes:duration>{duration_str}</itunes:duration>
      <itunes:summary><![CDATA[{summary}]]></itunes:summary>
    </item>""")

    items_block = "\n".join(xml_items)

    feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" 
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" 
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Readout Private Podcast</title>
    <link>{base_url}/</link>
    <description>Your personal running &amp; marathon audio queue generated by Readout</description>
    <language>ja</language>
    <itunes:author>Readout</itunes:author>
    <itunes:summary>マラソン・ランニング中に聴くパーソナル音声ニュースキュー</itunes:summary>
    <itunes:owner>
      <itunes:name>Readout</itunes:name>
      <itunes:email>readout@local</itunes:email>
    </itunes:owner>
    <itunes:image href="{base_url}/static/icons/icon-512.png" />
    <itunes:category text="Technology" />
    <itunes:explicit>false</itunes:explicit>
    <lastBuildDate>{now_rfc}</lastBuildDate>
{items_block}
  </channel>
</rss>
"""
    return Response(content=feed_xml, media_type="application/rss+xml; charset=utf-8")
