import time
import requests

BASE = "http://127.0.0.1:8002"
TEST_URL = "https://github.blog/news-insights/product-news/github-copilot-enterprise-is-now-generally-available/"

print("=== Readout Comprehensive Pipeline & RSS Reader Verification ===")

print("\n>>> 1. Testing RSS feeds API (/api/feeds)...")
r_feeds = requests.get(f"{BASE}/api/feeds")
assert r_feeds.status_code == 200, "GET /api/feeds failed"
feeds_data = r_feeds.json()
print(f"Subscribed feeds: {len(feeds_data)} items")
assert any(f["id"] == "feed_ghblog" for f in feeds_data), "GitHub Blog default feed not found"

print("\n>>> 2. Testing feed subscription & unsubscribe...")
# Add test feed
r_add_feed = requests.post(f"{BASE}/api/feeds", json={"url": "https://gihyo.jp/feed/rss2"})
assert r_add_feed.status_code == 200, "POST /api/feeds failed"
added_feed = r_add_feed.json()
print(f"Added feed: {added_feed['title']} (ID: {added_feed['id']})")

# Delete test feed
r_del_feed = requests.delete(f"{BASE}/api/feeds/{added_feed['id']}")
assert r_del_feed.status_code == 200, "DELETE /api/feeds failed"
print(f"Successfully unsubscribed feed: {added_feed['id']}")

print("\n>>> 3. Testing aggregated timeline (/api/articles)...")
r_articles = requests.get(f"{BASE}/api/articles")
assert r_articles.status_code == 200, "GET /api/articles failed"
articles_data = r_articles.json()
print(f"Articles fetched in timeline: {len(articles_data)} items")
assert len(articles_data) > 0, "No articles found in timeline"
first_art = articles_data[0]
print(f"Latest article: '{first_art['title']}' from '{first_art['feed_title']}' (in_queue: {first_art['in_queue']})")
assert "in_queue" in first_art, "in_queue field missing"
assert "summary" in first_art, "summary field missing"

print("\n>>> 4. Testing 1-tap add to queue from timeline...")
target_article = next((a for a in articles_data if not a["in_queue"]), articles_data[0])
print(f"Target article to enqueue: '{target_article['title']}' ({target_article['link']})")

res = requests.post(f"{BASE}/api/queue", json={"url": target_article["link"]})
assert res.status_code == 200, "POST /api/queue failed"
item = res.json()
item_id = item["id"]
print(f"Created queue item: {item_id} | Initial status: {item['status']}")

print("\n>>> 5. Waiting for AI rewrite + TTS audio generation pipeline...")
t0 = time.time()
ready_item = None
while time.time() - t0 < 60:
    q_res = requests.get(f"{BASE}/api/queue")
    q_data = q_res.json()
    matched = [it for it in q_data if it["id"] == item_id]
    if matched:
        cur = matched[0]
        status = cur.get("status")
        title = cur.get("title")
        print(f"   Elapsed: {time.time()-t0:.1f}s | Status: {status} | Title: {title}")
        if status == "ready":
            ready_item = cur
            break
        elif status == "failed":
            print("Failed error:", cur.get("error_message"))
            break
    time.sleep(3)

assert ready_item is not None, "Item did not reach ready status within 60s!"
print(f">>> Audio pipeline completed successfully in {time.time()-t0:.1f}s!")
print(f"Title: {ready_item['title']}")
print(f"Audio Duration: {ready_item['audio_duration_sec']}s")
print(f"Summary: {ready_item['summary'][:90]}...")

print("\n>>> 6. Verifying in_queue state sync in timeline...")
r_articles_after = requests.get(f"{BASE}/api/articles").json()
synced_art = next((a for a in r_articles_after if a["link"].rstrip("/") == target_article["link"].rstrip("/")), None)
if synced_art:
    assert synced_art["in_queue"] is True, "Article in_queue was not updated to True!"
    print(f"Confirmed article '{synced_art['title']}' in_queue state is TRUE")

print("\n>>> 7. Testing audio streaming with HTTP Range header...")
audio_file = ready_item["audio_filename"]
audio_url = f"{BASE}/audio/{audio_file}"

r_full = requests.get(audio_url)
assert r_full.status_code == 200
r_range = requests.get(audio_url, headers={"Range": "bytes=0-1023"})
assert r_range.status_code == 206
assert len(r_range.content) == 1024
print(f"Range streaming verified: Status 206, Content-Range {r_range.headers.get('content-range')}")

print("\n>>> 8. Testing Podcast RSS Feed (/feed.xml)...")
r_feed = requests.get(f"{BASE}/feed.xml")
assert r_feed.status_code == 200
assert audio_file in r_feed.text
assert ready_item["title"] in r_feed.text
print("Podcast RSS contains the newly generated audio episode!")

print("\n========================================================")
print("  ALL PIPELINE, RSS READER & QUEUE TESTS PASSED! (100%)  ")
print("========================================================")
