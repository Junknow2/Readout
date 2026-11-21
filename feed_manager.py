import concurrent.futures
import html
import json
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import feedparser
import queue_manager

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
FEEDS_FILE = DATA_DIR / "feeds.json"

_lock = threading.Lock()

DEFAULT_FEEDS = [
    {
        "id": "feed_ghblog",
        "title": "The GitHub Blog",
        "url": "https://github.blog/feed/",
        "site_url": "https://github.blog",
        "description": "Updates, ideas, and inspiration from GitHub.",
        "created_at": "2026-01-01T00:00:00+00:00",
    },
    {
        "id": "feed_zenn",
        "title": "Zennのトレンド",
        "url": "https://zenn.dev/feed",
        "site_url": "https://zenn.dev",
        "description": "エンジニアのための情報共有コミュニティ Zenn の最新トレンド記事",
        "created_at": "2026-01-01T00:00:00+00:00",
    },
]


def ensure_dirs():
    """Ensure data directory and feeds.json exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not FEEDS_FILE.exists():
        with open(FEEDS_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_FEEDS, f, ensure_ascii=False, indent=2)


def load_feeds() -> List[Dict[str, Any]]:
    """Load feeds list from feeds.json."""
    ensure_dirs()
    with _lock:
        try:
            with open(FEEDS_FILE, "r", encoding="utf-8") as f:
                feeds = json.load(f)
                if isinstance(feeds, list):
                    return feeds
                return []
        except Exception as e:
            print(f"[-] Error loading feeds.json: {e}")
            return []


def save_feeds(feeds: List[Dict[str, Any]]):
    """Save feeds list to feeds.json atomically."""
    ensure_dirs()
    with _lock:
        temp_file = FEEDS_FILE.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(feeds, f, ensure_ascii=False, indent=2)
        shutil.move(str(temp_file), str(FEEDS_FILE))


def get_feeds() -> List[Dict[str, Any]]:
    """Get all subscribed feeds."""
    return load_feeds()


def add_feed(url: str) -> Dict[str, Any]:
    """Parse RSS/Atom feed and add it to subscriptions."""
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("URL must start with http:// or https://")

    feeds = load_feeds()

    # Check for duplicates by URL
    for feed in feeds:
        if feed.get("url").rstrip("/") == url.rstrip("/"):
            return feed

    # Parse feed to validate and extract title
    parsed = feedparser.parse(url)
    if not parsed.feed and len(parsed.entries) == 0:
        raise ValueError("No valid RSS/Atom feed found at this URL")

    feed_meta = parsed.feed
    title = feed_meta.get("title") or urlparse(url).netloc
    site_url = feed_meta.get("link") or url
    description = feed_meta.get("description") or feed_meta.get("subtitle") or ""
    # Clean description
    description = re.sub(r"<[^>]+>", "", description).strip()[:200]

    feed_id = f"feed_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    new_feed: Dict[str, Any] = {
        "id": feed_id,
        "title": title,
        "url": url,
        "site_url": site_url,
        "description": description,
        "created_at": now,
    }

    feeds.append(new_feed)
    save_feeds(feeds)
    return new_feed


def delete_feed(feed_id: str) -> bool:
    """Delete a subscribed feed by id."""
    feeds = load_feeds()
    target = None
    new_feeds = []
    for f in feeds:
        if f.get("id") == feed_id:
            target = f
        else:
            new_feeds.append(f)

    if target:
        save_feeds(new_feeds)
        return True
    return False


def _clean_summary(raw_html: str, max_chars: int = 140) -> str:
    """Strip HTML tags and unescape text for compact preview."""
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        return text[:max_chars] + "..."
    return text


def _fetch_single_feed(feed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch and parse a single feed."""
    feed_url = feed.get("url")
    feed_id = feed.get("id")
    feed_title = feed.get("title") or "Unknown Feed"

    articles = []
    try:
        parsed = feedparser.parse(feed_url)
        for entry in parsed.entries[:25]:  # Take up to 25 latest per feed
            title = entry.get("title", "無題の記事").strip()
            link = entry.get("link", "").strip()
            if not link:
                continue

            # Summary extraction
            summary_raw = (
                entry.get("summary")
                or entry.get("description")
                or (entry.get("content", [{}])[0].get("value") if entry.get("content") else "")
                or ""
            )
            summary = _clean_summary(summary_raw)

            # Published timestamp
            published_ts = 0.0
            published_str = ""
            for date_field in ("published_parsed", "updated_parsed", "created_parsed"):
                date_tuple = entry.get(date_field)
                if date_tuple:
                    try:
                        published_ts = time.mktime(date_tuple)
                        dt = datetime.fromtimestamp(published_ts, tz=timezone.utc)
                        published_str = dt.strftime("%Y-%m-%d %H:%M")
                        break
                    except Exception:
                        pass

            if not published_ts:
                published_ts = time.time()
                published_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

            article_id = f"{feed_id}_{hash(link) & 0xFFFFFFFF:08x}"
            articles.append({
                "id": article_id,
                "feed_id": feed_id,
                "feed_title": feed_title,
                "title": title,
                "link": link,
                "summary": summary,
                "published": published_str,
                "published_ts": published_ts,
            })
    except Exception as e:
        print(f"[-] Failed to fetch feed {feed_title} ({feed_url}): {e}")

    return articles


def fetch_all_articles(max_articles: int = 40) -> List[Dict[str, Any]]:
    """Fetch articles from all subscribed feeds in parallel, sorted by date."""
    feeds = load_feeds()
    if not feeds:
        return []

    # Get active queue URLs to determine in_queue status
    active_queue = queue_manager.get_active_queue()
    queued_urls = set()
    for item in active_queue:
        url = item.get("url", "")
        if url:
            queued_urls.add(url.strip().rstrip("/"))

    all_articles: List[Dict[str, Any]] = []

    # Parallel fetch using ThreadPoolExecutor
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(feeds), 8)) as executor:
        future_to_feed = {executor.submit(_fetch_single_feed, f): f for f in feeds}
        for future in concurrent.futures.as_completed(future_to_feed):
            try:
                articles = future.result()
                all_articles.extend(articles)
            except Exception as e:
                feed = future_to_feed[future]
                print(f"[-] Error fetching feed {feed.get('title')}: {e}")

    # Mark in_queue
    for art in all_articles:
        clean_link = art["link"].strip().rstrip("/")
        art["in_queue"] = clean_link in queued_urls

    # Sort descending by published_ts
    all_articles.sort(key=lambda x: x["published_ts"], reverse=True)

    return all_articles[:max_articles]
