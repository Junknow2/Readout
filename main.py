import asyncio
import os
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mutagen.mp3 import MP3
from pydantic import BaseModel, HttpUrl

import poc_pipeline
import queue_manager

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

app = FastAPI(title="Readout", description="Marathon / Running Audio Queue & PWA")

# CORS for local dev / PWA
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Templates
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


class AddQueueRequest(BaseModel):
    url: str
    voice: Optional[str] = None
    model: Optional[str] = None


async def process_item_task(item_id: str, voice: Optional[str] = None, model: Optional[str] = None):
    """Background task to extract content, rewrite with Gemini, and generate MP3."""
    item = queue_manager.get_item(item_id)
    if not item:
        return

    queue_manager.update_item(item_id, status="processing")
    url = item["url"]
    chosen_voice = voice or poc_pipeline.DEFAULT_VOICE
    chosen_model = model or poc_pipeline.DEFAULT_MODEL

    try:
        # Step A: Extract
        extracted = poc_pipeline.extract_content_from_url(url)
        title = extracted["title"]
        queue_manager.update_item(item_id, title=title)

        # Step B: Rewrite
        script_text = poc_pipeline.rewrite_for_audio(title, extracted["content"], model_name=chosen_model)

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

        # Summary preview: First 120 chars
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
        )
        print(f"[+] Task completed for {item_id}: {title} ({duration_sec}s)")

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
    """Add a new URL to the queue and trigger async pipeline."""
    url = str(payload.url).strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid URL scheme. Must start with http:// or https://")

    item = queue_manager.add_item(url=url)
    background_tasks.add_task(
        process_item_task,
        item_id=item["id"],
        voice=payload.voice,
        model=payload.model,
    )
    return item


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
