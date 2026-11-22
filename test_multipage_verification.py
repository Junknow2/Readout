import sys
import time
from pathlib import Path
import requests

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import poc_pipeline
import queue_manager

BASE_URL = "http://127.0.0.1:8002"
NIKKEI_URL = "https://bookplus.nikkei.com/atcl/column/112600441/112600002/"
SINGLE_URL = "https://github.blog/news-insights/product-news/github-copilot-enterprise-is-now-generally-available/"

print("=== Readout Multipage Verification ===")

# Test 1: Multipage content extraction
print("\n>>> 1. Testing extract_multipage_content on Nikkei BookPlus...")
res_multi = poc_pipeline.extract_multipage_content(NIKKEI_URL)
print(f"Title: {res_multi['title']}")
print(f"Page Count: {res_multi['page_count']}")
print(f"Pages: {res_multi['pages']}")
print(f"Content Length: {len(res_multi['content'])} chars")

assert res_multi["page_count"] == 2, f"Expected 2 pages, got {res_multi['page_count']}"
assert "成果主義" in res_multi["content"], "Page 1 content missing"
assert "官僚制" in res_multi["content"], "Page 2 content missing"
assert "--- 続き（ページ 2" in res_multi["content"], "Page boundary transition missing"
print("[✓] Multipage extraction verified successfully!")

# Test 2: Single-page URL extraction
print("\n>>> 2. Testing extract_multipage_content on single-page URL...")
res_single = poc_pipeline.extract_multipage_content(SINGLE_URL)
print(f"Title: {res_single['title']}")
print(f"Page Count: {res_single['page_count']}")
assert res_single["page_count"] == 1, f"Expected 1 page, got {res_single['page_count']}"
print("[✓] Single-page extraction verified successfully!")

# Test 3: FastAPI API integration (/api/queue)
print("\n>>> 3. Testing POST /api/queue with multipage URL...")
r = requests.post(f"{BASE_URL}/api/queue", json={"url": NIKKEI_URL})
assert r.status_code == 200, f"Failed to enqueue: {r.text}"
item = r.json()
item_id = item["id"]
print(f"Enqueued item: {item_id}")

print("Waiting for background task to complete...")
t0 = time.time()
ready_item = None
while time.time() - t0 < 120:
    q = requests.get(f"{BASE_URL}/api/queue").json()
    matched = [it for it in q if it["id"] == item_id]
    if matched:
        cur = matched[0]
        status = cur.get("status")
        if status == "ready":
            ready_item = cur
            break
        elif status == "failed":
            raise RuntimeError(f"Processing failed: {cur.get('error_message')}")
    time.sleep(3)

assert ready_item is not None, "Item did not reach ready status within 120s"
print(f"[✓] Item {item_id} reached READY!")
print(f"Title: {ready_item['title']}")
print(f"Duration: {ready_item['audio_duration_sec']}s")
print(f"Page count stored: {ready_item.get('page_count')}")
assert ready_item.get("page_count") == 2, f"Expected page_count=2, got {ready_item.get('page_count')}"

print("\n========================================================")
print("ALL MULTIPAGE VERIFICATION TESTS PASSED SUCCESSFULLY!")
print("========================================================")
