import time
import requests

BASE = "http://127.0.0.1:8002"
TEST_URL = "https://github.blog/news-insights/product-news/github-copilot-enterprise-is-now-generally-available/"

print(">>> 1. Adding item to queue...")
res = requests.post(f"{BASE}/api/queue", json={"url": TEST_URL})
print("POST /api/queue status:", res.status_code)
item = res.json()
item_id = item["id"]
print("Created item:", item_id, "Status:", item["status"])
assert res.status_code == 200
assert item["status"] == "pending"

print(">>> 2. Waiting for pipeline to process (poll /api/queue)...")
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

assert ready_item is not None, "Item did not reach ready status!"
print(">>> Pipeline completed successfully!")
print("Title:", ready_item["title"])
print("Duration sec:", ready_item["audio_duration_sec"])
print("Summary preview:", ready_item["summary"][:80])

print(">>> 3. Testing audio stream & Range header...")
audio_file = ready_item["audio_filename"]
audio_url = f"{BASE}/audio/{audio_file}"

# Full GET
r_full = requests.get(audio_url)
print("GET full audio status:", r_full.status_code, "Content-Length:", len(r_full.content))
assert r_full.status_code == 200

# Range GET
r_range = requests.get(audio_url, headers={"Range": "bytes=0-1023"})
print("GET range status:", r_range.status_code, "Content-Range:", r_range.headers.get("content-range"), "Len:", len(r_range.content))
assert r_range.status_code == 206
assert len(r_range.content) == 1024

print(">>> 4. Testing Podcast RSS feed (/feed.xml) with ready item...")
r_feed = requests.get(f"{BASE}/feed.xml")
assert r_feed.status_code == 200
assert audio_file in r_feed.text
assert ready_item["title"] in r_feed.text
print("Feed contains new episode!")

print(">>> 5. Testing mark listened (/api/queue/{id}/listened)...")
r_listened = requests.post(f"{BASE}/api/queue/{item_id}/listened")
assert r_listened.status_code == 200
q_after = requests.get(f"{BASE}/api/queue").json()
assert not any(it["id"] == item_id for it in q_after)
print("Successfully marked listened and excluded from queue!")

print("ALL PIPELINE & API VERIFICATION TESTS PASSED!")
