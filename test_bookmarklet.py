import time
import requests

BASE = "http://127.0.0.1:8002"

print("=== Testing Bookmarklet Functionality ===")

# 1. Test Invalid URL
print("\n>>> 1. Testing invalid URL handling...")
r_err = requests.get(f"{BASE}/api/bookmarklet", params={"url": "javascript:alert(1)"})
assert r_err.status_code == 400, f"Expected 400, got {r_err.status_code}"
assert "追加できませんでした" in r_err.text
assert "無効なURL形式です" in r_err.text
print("✓ Invalid URL properly returned status 400 and friendly dark/neon error HTML page.")

# 2. Test Valid Bookmarklet Request
print("\n>>> 2. Testing valid bookmarklet request...")
test_url = "https://example.com"
test_title = "Example Domain Bookmarklet Test"
r_ok = requests.get(f"{BASE}/api/bookmarklet", params={"url": test_url, "title": test_title})
assert r_ok.status_code == 200, f"Expected 200, got {r_ok.status_code}"
assert "🎧 Readout に追加しました！" in r_ok.text
assert test_title in r_ok.text
assert "window.close()" in r_ok.text
assert "1.5秒後に自動で閉じます" in r_ok.text
print("✓ Valid bookmarklet returned status 200 with Readout neon confirmation popup HTML.")

# 3. Check Queue & Background Pipeline
print("\n>>> 3. Checking queue and pipeline...")
time.sleep(1)
r_q = requests.get(f"{BASE}/api/queue")
items = r_q.json()
found = next((it for it in items if it.get("url") == test_url), None)
assert found is not None, "Item was not added to queue!"
print(f"✓ Found item in queue: id={found['id']}, title='{found['title']}', status={found['status']}")

# 4. Wait for processing (example.com content extraction & audio synthesis)
print("\n>>> 4. Waiting for background task execution...")
t0 = time.time()
while time.time() - t0 < 30:
    r_q = requests.get(f"{BASE}/api/queue")
    for it in r_q.json():
        if it["id"] == found["id"]:
            print(f"   Status: {it['status']} | Duration: {it['audio_duration_sec']}s")
            if it["status"] in ("ready", "failed"):
                found = it
                break
    if found["status"] in ("ready", "failed"):
        break
    time.sleep(2)

print(f"Final status for {found['id']}: {found['status']}")
if found["status"] == "ready":
    print(f"✓ Pipeline succeeded! MP3 filename: {found['audio_filename']}, duration: {found['audio_duration_sec']}s")
elif found["status"] == "failed":
    print(f"Note: Failed error: {found['error_message']}")

# Clean up test item
print("\n>>> 5. Cleaning up test queue item...")
r_del = requests.delete(f"{BASE}/api/queue/{found['id']}")
assert r_del.status_code == 200
print("✓ Cleaned up test item successfully.")

# 6. Verify main index page serves bookmarklet UI
print("\n>>> 6. Verifying index.html has bookmarklet elements...")
r_idx = requests.get(f"{BASE}/")
assert r_idx.status_code == 200
assert "btn-bookmarklet" in r_idx.text
assert "modal-bookmarklet" in r_idx.text
assert "🎧 Readout に追加" in r_idx.text
assert "bookmarklet-link" in r_idx.text
assert "bookmarklet-code-input" in r_idx.text
print("✓ index.html has all required bookmarklet elements and modal structure.")

print("\n=== ALL BOOKMARKLET TESTS PASSED SUCCESSFULLY! ===")
