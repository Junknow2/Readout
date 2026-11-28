import asyncio
import base64
import hashlib
import html
import os
import secrets
import sys
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
import curator_engine

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

# Auth configuration (mirrors Matrix / LifeLog)
READOUT_USERNAME = os.getenv("READOUT_USERNAME") or os.getenv("DASHBOARD_USERNAME")
READOUT_PASSWORD = os.getenv("READOUT_PASSWORD") or os.getenv("DASHBOARD_PASSWORD")

# Matrix Note integration configuration (LifeLog local API on port 8000)
MATRIX_NOTE_API_URL = os.getenv("MATRIX_NOTE_API_URL", "http://127.0.0.1:8000/api/notes").rstrip("/")


def _load_env_file_vars(env_path: Path) -> dict[str, str]:
    """Minimal .env key=value parser for fallback credential lookup."""
    result: dict[str, str] = {}
    if not env_path.exists():
        return result
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return result


def _get_matrix_note_credentials() -> tuple[str, Optional[tuple[str, str]]]:
    """Return (api_url, (username, password) or None) for LifeLog Matrix Note API."""
    api_url = (os.getenv("MATRIX_NOTE_API_URL") or MATRIX_NOTE_API_URL).rstrip("/")
    user = os.getenv("MATRIX_USERNAME") or READOUT_USERNAME
    pw = os.getenv("MATRIX_PASSWORD") or READOUT_PASSWORD
    if not (user and pw):
        lifelog_env = _load_env_file_vars(BASE_DIR.parent / "LifeLog" / ".env")
        user = user or lifelog_env.get("DASHBOARD_USERNAME")
        pw = pw or lifelog_env.get("DASHBOARD_PASSWORD")
    auth = (user, pw) if (user and pw) else None
    return api_url, auth


def _post_to_matrix_note(
    title: str,
    content: str,
    tags: list[str],
    source: dict,
) -> dict:
    """POST a note to LifeLog's Matrix Note API (NoteCreateRequest schema)."""
    api_url, auth = _get_matrix_note_credentials()
    payload = {
        "title": title,
        "content": content,
        "tags": tags,
        "source": source,
    }
    resp = requests.post(
        api_url,
        json=payload,
        auth=auth,
        timeout=15,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Matrix Note API returned HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()

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


class CuratorRunRequest(BaseModel):
    date: Optional[str] = None
    synthesize_audio: bool = True
    append_set: bool = True


class CuratorEnqueueRequest(BaseModel):
    date: str
    part_id: str


class CuratorFeedbackRequest(BaseModel):
    date: str
    part_id: str
    rating: int | str
    reasons: list[str] = []
    memo: str = ""


class CuratorPickCandidateRequest(BaseModel):
    url: str
    title: Optional[str] = None
    date: Optional[str] = None


class SendToMatrixNoteRequest(BaseModel):
    force: bool = False


class CuratorSendToMatrixNoteRequest(BaseModel):
    date: str
    part_id: str
    force: bool = False


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
        used_engine = await poc_pipeline.generate_speech_async(script_text, mp3_path, voice=chosen_voice)

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
            tts_engine=used_engine,
            status="ready",
            error_message=None,
            page_count=page_count,
        )
        print(f"[+] Task completed for {item_id}: {title} ({duration_sec}s, {page_count} pages, engine={used_engine})")

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
    curator_engine.ensure_dirs()
    try:
        queue_manager.cleanup_listened_audio(retention_days=7)
    except Exception as e:
        print(f"[Startup] Listened audio cleanup warning: {e}")



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
async def get_queue(filter: str = "unread"):
    """
    List queue items.
    - filter='unread' (default): active unread items (backward compatible)
    - filter='listened': listened items (newest listened_at first)
    - filter='all': all items
    """
    mode = (filter or "unread").strip().lower()
    if mode == "listened":
        return queue_manager.get_listened_queue()
    if mode == "all":
        return queue_manager.get_all_queue()
    if mode != "unread":
        raise HTTPException(status_code=400, detail="filter must be one of: unread, listened, all")
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
    """Mark an item as listened (moves from unread queue to listened list)."""
    success = queue_manager.mark_listened(item_id)
    if not success:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"success": True, "id": item_id}


@app.post("/api/queue/{item_id}/unread")
async def mark_item_unread(item_id: str):
    """Revert a listened item back to unread ('ready') if its MP3 audio file still exists."""
    try:
        updated = queue_manager.mark_unread(item_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not updated:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"success": True, "id": item_id, "item": updated}


@app.post("/api/queue/cleanup-listened")
async def cleanup_listened_audio_api(retention_days: int = 7):
    """Manually trigger cleanup of listened MP3 audio files older than retention_days."""
    if retention_days < 0:
        raise HTTPException(status_code=400, detail="retention_days must be >= 0")
    result = queue_manager.cleanup_listened_audio(retention_days=retention_days)
    return {"success": True, **result}


@app.post("/api/queue/{item_id}/send-to-matrix-note")
async def send_queue_item_to_matrix_note(
    item_id: str,
    payload: Optional[SendToMatrixNoteRequest] = None,
):
    """Send a Readout queue item (unread or listened) to LifeLog's Matrix Note."""
    item = queue_manager.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    force = bool(payload.force) if payload else False
    if item.get("sent_to_matrix_note") and item.get("matrix_note_id") and not force:
        return {
            "success": True,
            "already_sent": True,
            "note_id": item.get("matrix_note_id"),
            "item": queue_manager._enrich_item(item),
        }

    title = (item.get("title") or "Readout メモ").strip()
    summary = (item.get("summary") or "").strip()
    url = (item.get("url") or "").strip()
    source_name = (item.get("source_name") or "").strip()
    is_http_url = url.startswith(("http://", "https://"))

    content_parts = []
    if summary:
        content_parts.append(summary)
    source_lines = []
    if source_name:
        source_lines.append(f"- 媒体: {source_name}")
    if is_http_url:
        source_lines.append(f"- 出典: {url}")
    elif item.get("original_filename"):
        source_lines.append(f"- ファイル: {item.get('original_filename')}")
    if source_lines:
        content_parts.append("### 出典情報\n" + "\n".join(source_lines))
    content = "\n\n".join(content_parts) or title

    tags = ["Readout", "Curator"] if item.get("item_type") == "curator" else ["Readout"]
    source_title = f"{source_name} - {title}".strip(" -") if source_name else title
    source_obj = {
        "type": "web",
        "title": source_title,
        "url": url if is_http_url else "",
    }

    try:
        res = await asyncio.to_thread(_post_to_matrix_note, title, content, tags, source_obj)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Matrix Note 送信に失敗しました: {e}")

    note_data = res.get("note") if isinstance(res, dict) else None
    note_id = (note_data or {}).get("id") or ""
    updated = queue_manager.update_item(
        item_id,
        sent_to_matrix_note=True,
        matrix_note_id=note_id,
        matrix_sent_at=datetime.now(timezone.utc).isoformat(),
    )
    return {
        "success": True,
        "already_sent": False,
        "note_id": note_id,
        "note": note_data,
        "item": queue_manager._enrich_item(updated or item),
    }


@app.delete("/api/queue/{item_id}")
async def delete_queue_item(item_id: str):
    """Delete an item from the queue and remove associated files."""
    success = queue_manager.delete_item(item_id)
    if not success:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"success": True, "id": item_id}


# --- Personal Knowledge Curator Endpoints ---

async def _synthesize_curator_queue_item(item_id: str, script_text: str):
    """Background TTS fallback when a curator part is enqueued before pre-synthesis finishes."""
    try:
        mp3_path = OUTPUT_DIR / f"{item_id}.mp3"
        used_engine = await poc_pipeline.generate_speech_async(script_text, mp3_path, voice=poc_pipeline.DEFAULT_VOICE)
        dur = 0.0
        try:
            dur = round(MP3(str(mp3_path)).info.length, 1)
        except Exception:
            dur = round(len(script_text) / 5.5, 1)
        queue_manager.update_item(
            item_id,
            audio_duration_sec=dur,
            tts_engine=used_engine,
            status="ready",
            error_message=None,
        )
    except Exception as e:
        queue_manager.update_item(item_id, status="failed", error_message=str(e))


async def _run_curator_bg(date_str: Optional[str], synthesize_audio: bool, append_set: bool = False):
    try:
        await curator_engine.run_daily_curation(
            date_str=date_str,
            synthesize_audio=synthesize_audio,
            append_set=append_set,
        )
    except Exception as e:
        print(f"[-] Background curator run failed: {e}")


@app.get("/api/curator/dates")
async def get_curator_dates():
    """Return list of dates that have curated daily selections."""
    return {"dates": curator_engine.list_curated_dates()}


@app.get("/api/curator/status")
async def get_curator_status():
    """Return current background curation progress status."""
    return curator_engine.get_status()


@app.get("/api/curator/daily")
async def get_curator_daily(date: Optional[str] = None):
    """Return Daily Selection + Research Log + runtime status for specified or latest date."""
    target_date, daily, research_log = curator_engine.load_daily(date)
    return {
        "date": target_date,
        "dates": curator_engine.list_curated_dates(),
        "daily": daily,
        "research_log": research_log,
        "status": curator_engine.get_status(),
    }


@app.post("/api/curator/run")
async def trigger_curator_run(
    background_tasks: BackgroundTasks,
    payload: Optional[CuratorRunRequest] = None,
):
    """Trigger manual curation run in background (defaults to append_set=True so existing daily sets are never overwritten)."""
    status = curator_engine.get_status()
    if status.get("running"):
        return {"started": False, "message": "すでにキュレーションを実行中です。", "status": status}
    req_date = payload.date if payload else None
    synth = payload.synthesize_audio if payload else True
    append_set = payload.append_set if payload else True
    resolved_date = curator_engine.resolve_next_set_key(req_date, append_set=append_set)
    # Pass already-resolved set_key with append_set=False so background task uses the exact key returned here
    background_tasks.add_task(_run_curator_bg, resolved_date, synth, False)
    parsed = curator_engine.parse_set_key(resolved_date)
    is_extra = bool(parsed and parsed[1] >= 2)
    msg = (
        f"追加セット（{parsed[0]} 追加 #{parsed[1]}）のキュレーションを開始しました。"
        if is_extra
        else f"{resolved_date} のキュレーションを開始しました。"
    )
    return {
        "started": True,
        "date": resolved_date,
        "append_set": append_set,
        "message": msg,
    }


@app.post("/api/curator/enqueue")
async def enqueue_curator_part_api(
    payload: CuratorEnqueueRequest,
    background_tasks: BackgroundTasks,
):
    """
    Add a curated recommendation part to Readout's playback queue.
    If pre-synthesized MP3 is ready, copies it immediately (0s wait, status='ready').
    """
    try:
        item, needs_bg_tts, script_text = curator_engine.enqueue_curator_part(
            payload.date, payload.part_id
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Enqueue failed: {e}")

    if needs_bg_tts and script_text:
        background_tasks.add_task(_synthesize_curator_queue_item, item["id"], script_text)

    return {
        "item": item,
        "instant_ready": not needs_bg_tts,
    }


@app.post("/api/curator/feedback")
async def submit_curator_feedback(payload: CuratorFeedbackRequest):
    """Save 5-level feedback + reason chips + memo and update Core preference profile."""
    try:
        res = curator_engine.record_feedback(
            date_str=payload.date,
            part_id=payload.part_id,
            rating=payload.rating,
            reasons=payload.reasons,
            memo=payload.memo,
        )
        return {"success": True, **res}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Feedback save failed: {e}")


@app.post("/api/curator/pick-candidate")
async def pick_curator_candidate(
    payload: CuratorPickCandidateRequest,
    background_tasks: BackgroundTasks,
):
    """Enqueue any candidate from Research Log (including rejected/not-selected) into standard Readout queue."""
    url = str(payload.url).strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Invalid URL scheme. Must start with http:// or https://")

    is_pdf = is_pdf_url(url)
    item = queue_manager.add_item(
        url=url,
        title=(payload.title or "").strip() or None,
        item_type="pdf" if is_pdf else "web",
    )
    background_tasks.add_task(process_item_task, item_id=item["id"])
    return item


@app.post("/api/curator/send-to-matrix-note")
async def send_curator_part_to_matrix_note(payload: CuratorSendToMatrixNoteRequest):
    """Send a Curator daily part directly to LifeLog's Matrix Note."""
    target_date, daily, _ = curator_engine.load_daily(payload.date)
    if not daily or not isinstance(daily.get("parts"), list):
        raise HTTPException(status_code=404, detail=f"Curator daily data not found for {payload.date}")

    target_part = None
    for p in daily["parts"]:
        if p.get("part_id") == payload.part_id:
            target_part = p
            break
    if not target_part:
        raise HTTPException(status_code=404, detail=f"Part '{payload.part_id}' not found")

    if target_part.get("sent_to_matrix_note") and target_part.get("matrix_note_id") and not payload.force:
        return {
            "success": True,
            "already_sent": True,
            "note_id": target_part.get("matrix_note_id"),
            "part_id": payload.part_id,
            "date": target_date,
        }

    src = target_part.get("source") or {}
    title = (target_part.get("title") or "Curator メモ").strip()
    why_selected = (target_part.get("why_selected") or "").strip()
    src_name = (src.get("source_name") or "").strip()
    src_author = (src.get("author") or "").strip()
    src_pub = (src.get("published_at") or "").strip()
    src_url = (src.get("url") or "").strip()
    orig_title = (src.get("original_title") or "").strip()

    content_parts = []
    if why_selected:
        content_parts.append(why_selected)
    source_lines = []
    if orig_title and orig_title != title:
        source_lines.append(f"- 原題: {orig_title}")
    if src_name:
        source_lines.append(f"- 媒体: {src_name}")
    if src_author and src_author != "記載なし":
        source_lines.append(f"- 著者: {src_author}")
    if src_pub and src_pub != "記載なし":
        source_lines.append(f"- 公開日: {src_pub}")
    if src_url:
        source_lines.append(f"- 出典: {src_url}")
    if source_lines:
        content_parts.append("### 出典情報\n" + "\n".join(source_lines))
    content = "\n\n".join(content_parts) or title

    source_title = f"{src_name} - {orig_title or title}".strip(" -") if src_name else (orig_title or title)
    source_obj = {
        "type": "web",
        "title": source_title,
        "url": src_url if src_url.startswith(("http://", "https://")) else "",
    }
    tags = ["Readout", "Curator"]

    try:
        res = await asyncio.to_thread(_post_to_matrix_note, title, content, tags, source_obj)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Matrix Note 送信に失敗しました: {e}")

    note_data = res.get("note") if isinstance(res, dict) else None
    note_id = (note_data or {}).get("id") or ""
    curator_engine.mark_part_sent_to_matrix(target_date or payload.date, payload.part_id, note_id)

    return {
        "success": True,
        "already_sent": False,
        "note_id": note_id,
        "note": note_data,
        "part_id": payload.part_id,
        "date": target_date or payload.date,
    }



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
