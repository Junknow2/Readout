import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PDF_DIR = DATA_DIR / "pdfs"
QUEUE_FILE = DATA_DIR / "queue.json"
OUTPUT_DIR = BASE_DIR / "output"

_lock = threading.Lock()


def ensure_dirs():
    """Ensure data and output directories exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not QUEUE_FILE.exists():
        QUEUE_FILE.write_text("[]", encoding="utf-8")


def load_queue() -> List[Dict[str, Any]]:
    """Load all items from queue.json."""
    ensure_dirs()
    with _lock:
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []


def save_queue(items: List[Dict[str, Any]]):
    """Save all items to queue.json safely."""
    ensure_dirs()
    with _lock:
        temp_file = QUEUE_FILE.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        shutil.move(str(temp_file), str(QUEUE_FILE))


def get_active_queue() -> List[Dict[str, Any]]:
    """Return items that are not listened, newest first."""
    items = load_queue()
    active = [item for item in items if item.get("status") != "listened"]
    # Sort descending by created_at
    active.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return active


def get_item(item_id: str) -> Optional[Dict[str, Any]]:
    """Find item by id."""
    items = load_queue()
    for item in items:
        if item.get("id") == item_id:
            return item
    return None


def add_item(
    url: str,
    title: Optional[str] = None,
    item_type: str = "web",
    original_filename: Optional[str] = None
) -> Dict[str, Any]:
    """Create a new queue item with status 'pending'."""
    items = load_queue()
    item_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()
    
    new_item: Dict[str, Any] = {
        "id": item_id,
        "url": url,
        "title": title or "URLを解析中...",
        "summary": "",
        "audio_filename": f"{item_id}.mp3",
        "audio_duration_sec": 0.0,
        "status": "pending",  # pending -> processing -> ready -> listened (or failed)
        "error_message": None,
        "page_count": 1,
        "item_type": item_type,  # "web" or "pdf"
        "original_filename": original_filename,
        "created_at": now,
    }
    items.append(new_item)
    save_queue(items)
    return new_item


def update_item(item_id: str, **kwargs) -> Optional[Dict[str, Any]]:
    """Update fields of an existing item."""
    items = load_queue()
    updated = None
    for item in items:
        if item.get("id") == item_id:
            item.update(kwargs)
            updated = item
            break
    if updated:
        save_queue(items)
    return updated


def mark_listened(item_id: str) -> bool:
    """Set status to 'listened'."""
    res = update_item(item_id, status="listened")
    return res is not None


def delete_item(item_id: str) -> bool:
    """Remove item from queue and delete its audio/text output files."""
    items = load_queue()
    target = None
    new_items = []
    for item in items:
        if item.get("id") == item_id:
            target = item
        else:
            new_items.append(item)

    if target:
        save_queue(new_items)
        # Delete output files if present
        audio_filename = target.get("audio_filename")
        if audio_filename:
            mp3_path = OUTPUT_DIR / audio_filename
            if mp3_path.exists():
                try:
                    mp3_path.unlink()
                except Exception:
                    pass
            txt_path = mp3_path.with_suffix(".txt")
            if txt_path.exists():
                try:
                    txt_path.unlink()
                except Exception:
                    pass
        # Delete uploaded PDF if present
        pdf_file = PDF_DIR / f"{item_id}.pdf"
        if pdf_file.exists():
            try:
                pdf_file.unlink()
            except Exception:
                pass
        return True
    return False
