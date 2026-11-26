import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
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


def _enrich_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Attach computed audio existence & Matrix Note defaults without mutating stored JSON."""
    enriched = dict(item)
    audio_filename = enriched.get("audio_filename") or (
        f"{enriched['id']}.mp3" if enriched.get("id") else ""
    )
    mp3_exists = bool(audio_filename and (OUTPUT_DIR / audio_filename).exists())
    audio_deleted = bool(enriched.get("audio_deleted", False)) or (
        enriched.get("status") == "listened" and not mp3_exists
    )
    enriched["has_audio"] = mp3_exists
    enriched["audio_deleted"] = audio_deleted
    enriched.setdefault("sent_to_matrix_note", False)
    enriched.setdefault("matrix_note_id", None)
    return enriched


def get_active_queue() -> List[Dict[str, Any]]:
    """Return items that are not listened, newest first."""
    items = load_queue()
    active = [_enrich_item(item) for item in items if item.get("status") != "listened"]
    # Sort descending by created_at
    active.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return active


def get_listened_queue() -> List[Dict[str, Any]]:
    """Return items marked as listened, newest listened_at/created_at first."""
    items = load_queue()
    listened = [_enrich_item(item) for item in items if item.get("status") == "listened"]
    listened.sort(
        key=lambda x: x.get("listened_at") or x.get("created_at") or "",
        reverse=True,
    )
    return listened


def get_all_queue() -> List[Dict[str, Any]]:
    """Return all queue items (unread + listened), newest created_at first."""
    items = load_queue()
    enriched = [_enrich_item(item) for item in items]
    enriched.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return enriched


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
        "tts_engine": None,
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
    """Set status to 'listened' and record UTC ISO8601 listened_at timestamp."""
    now = datetime.now(timezone.utc).isoformat()
    res = update_item(item_id, status="listened", listened_at=now)
    return res is not None


def mark_unread(item_id: str) -> Optional[Dict[str, Any]]:
    """
    Revert a listened item back to 'ready' if its MP3 audio file still exists.
    Raises FileNotFoundError if the audio file was already deleted.
    Returns None if item does not exist.
    """
    item = get_item(item_id)
    if not item:
        return None
    audio_filename = item.get("audio_filename") or f"{item_id}.mp3"
    mp3_path = OUTPUT_DIR / audio_filename
    if not mp3_path.exists():
        raise FileNotFoundError("音声ファイルが削除済みのため未読へ戻せません。")
    updated = update_item(
        item_id,
        status="ready",
        listened_at=None,
        audio_deleted=False,
    )
    return _enrich_item(updated) if updated else None


def _parse_iso_utc(ts: Optional[str]) -> Optional[datetime]:
    """Parse ISO8601 timestamp string to timezone-aware UTC datetime."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def cleanup_listened_audio(retention_days: int = 7) -> Dict[str, Any]:
    """
    Delete MP3 audio files for items with status=='listened' older than `retention_days`.
    Preserves queue.json metadata (title, summary, url, matrix_note status) and .txt scripts.
    Never touches unread/pending/processing/ready/failed items.
    """
    ensure_dirs()
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=retention_days)
    items = load_queue()

    deleted_files: List[str] = []
    freed_bytes = 0
    changed = False

    for item in items:
        if item.get("status") != "listened":
            continue
        ref_dt = _parse_iso_utc(item.get("listened_at") or item.get("created_at"))
        if ref_dt is None or ref_dt > cutoff:
            continue

        audio_filename = item.get("audio_filename") or f"{item.get('id', '')}.mp3"
        mp3_path = OUTPUT_DIR / audio_filename
        if mp3_path.exists() and mp3_path.is_file():
            try:
                size = mp3_path.stat().st_size
                mp3_path.unlink()
                deleted_files.append(audio_filename)
                freed_bytes += size
                item["audio_deleted"] = True
                item["audio_deleted_at"] = now_utc.isoformat()
                changed = True
            except Exception as e:
                print(f"[Queue/Cleanup] Failed to delete {mp3_path}: {e}")
        elif not item.get("audio_deleted"):
            # Already missing on disk; mark flag for consistency
            item["audio_deleted"] = True
            item.setdefault("audio_deleted_at", now_utc.isoformat())
            changed = True

    if changed:
        save_queue(items)

    if deleted_files:
        print(
            f"[Queue/Cleanup] Deleted {len(deleted_files)} listened MP3(s) "
            f"older than {retention_days}d ({freed_bytes} bytes freed): {deleted_files}"
        )

    return {
        "retention_days": retention_days,
        "deleted_count": len(deleted_files),
        "deleted_files": deleted_files,
        "freed_bytes": freed_bytes,
    }


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
