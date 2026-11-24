import os
import sys
import time
from pathlib import Path
import requests
from dotenv import load_dotenv
from mutagen.mp3 import MP3

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

load_dotenv(BASE_DIR / ".env")
USERNAME = os.getenv("READOUT_USERNAME")
PASSWORD = os.getenv("READOUT_PASSWORD")
AUTH = (USERNAME, PASSWORD) if USERNAME and PASSWORD else None
SERVER_URL = "http://127.0.0.1:8002"

print("=" * 60)
print("Readout - PDF Speech Synthesis & Pipeline Verification")
print(f"Target Server: {SERVER_URL}")
print(f"Auth Configured: {bool(AUTH)}")
print("=" * 60)

# 1. Create a realistic test PDF (Transformer & Attention Paper)
# Using standard PDF syntax with Title, Abstract, Body, Footnotes, and References
SAMPLE_PDF_BYTES = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj
4 0 obj << /Length 520 >> stream
BT
/F1 18 Tf
50 720 Td
(Attention Mechanism and Next-Generation Deep Learning) Tj
/F1 11 Tf
0 -30 Td
(Author: Research Laboratory of Artificial Intelligence) Tj
0 -25 Td
(Abstract: In this paper, we explore how attention mechanisms revolutionized sequence transduction.) Tj
0 -18 Td
(By eliminating recurrent layers, models achieve greater parallelization during training.) Tj
0 -25 Td
(Section 1: Architecture) Tj
0 -18 Td
(The core idea is self-attention, which computes compatibility scores between query and key vectors.) Tj
0 -18 Td
(This enables the model to capture global dependencies regardless of distance in the sequence.) Tj
0 -30 Td
(Footnote 1: Experimental code is available under MIT license.) Tj
0 -20 Td
(References: [1] Vaswani et al. 2017. Attention is All You Need. NeurIPS.) Tj
ET
endstream endobj
5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj
xref
0 6
0000000000 65535 f 
0000000010 00000 n 
0000000060 00000 n 
0000000117 00000 n 
0000000227 00000 n 
0000000798 00000 n 
trailer << /Size 6 /Root 1 0 R >>
startxref
877
%%EOF"""

pdf_test_file = BASE_DIR / "data" / "test_sample_paper.pdf"
pdf_test_file.parent.mkdir(parents=True, exist_ok=True)
pdf_test_file.write_bytes(SAMPLE_PDF_BYTES)
print(f"[1] Created test PDF: {pdf_test_file} ({len(SAMPLE_PDF_BYTES)} bytes)")

# 2. Upload PDF via POST /api/upload-pdf
print("\n[2] Uploading PDF via POST /api/upload-pdf...")
upload_files = {
    "file": ("Attention_Mechanism_Paper.pdf", SAMPLE_PDF_BYTES, "application/pdf")
}
t0 = time.time()
res_upload = requests.post(
    f"{SERVER_URL}/api/upload-pdf",
    files=upload_files,
    auth=AUTH,
    timeout=15
)
assert res_upload.status_code == 200, f"Upload failed: {res_upload.status_code} - {res_upload.text}"
item = res_upload.json()
item_id = item["id"]
print(f"  [+] Upload successful! Enqueued item ID: {item_id}")
print(f"  [+] Initial title: {item['title']}")
print(f"  [+] Item type: {item.get('item_type')}")
assert item.get("item_type") == "pdf", f"Expected item_type == 'pdf', got {item.get('item_type')}"

# 3. Poll queue until item reaches 'ready' status
print("\n[3] Waiting for Gemini 3.8 Flash PDF rewrite + VOICEPEAK synthesis pipeline...")
ready_item = None
max_wait_sec = 180
poll_start = time.time()

while time.time() - poll_start < max_wait_sec:
    q_res = requests.get(f"{SERVER_URL}/api/queue", auth=AUTH, timeout=10)
    assert q_res.status_code == 200, f"Failed to get queue: {q_res.status_code}"
    queue_items = q_res.json()
    matched = [it for it in queue_items if it["id"] == item_id]

    if matched:
        cur = matched[0]
        status = cur.get("status")
        title = cur.get("title")
        elapsed = time.time() - poll_start
        print(f"  ... [{elapsed:5.1f}s] status: {status:10s} | title: {title}")

        if status == "ready":
            ready_item = cur
            break
        elif status == "failed":
            raise RuntimeError(f"Pipeline processing failed: {cur.get('error_message')}")

    time.sleep(3)

assert ready_item is not None, f"Item did not reach 'ready' status within {max_wait_sec}s!"
total_pipeline_time = time.time() - poll_start
print(f"\n[+] Pipeline completed successfully in {total_pipeline_time:.2f} seconds!")

# 4. Verify Script Text Content
audio_filename = ready_item["audio_filename"]
mp3_path = BASE_DIR / "output" / audio_filename
txt_path = mp3_path.with_suffix(".txt")

assert txt_path.exists(), f"Script text file does not exist: {txt_path}"
script_content = txt_path.read_text(encoding="utf-8")

print("\n--- Generated Script Text Inspection ---")
print(f"Title: {ready_item['title']}")
print(f"Character Count: {len(script_content)} chars")
print(f"Script Preview:\n{script_content[:300]}...")

# Quality Verifications:
# - Narration must start with "{Title}についての解説。"
assert "についての解説" in script_content[:150], "Script does not begin with mandatory title explanation declaration!"

# - Radio greetings must be completely excluded
assert "こんにちは" not in script_content, "Found forbidden radio greeting 'こんにちは' in script!"
assert "今日も良い一日を" not in script_content, "Found forbidden radio sign-off '今日も良い一日を' in script!"

# - Footnote noise and raw references must be excluded
assert "MIT license" not in script_content, "Footnote content was not excluded!"
assert "NeurIPS" not in script_content, "Raw bibliography was not excluded!"

# 5. Verify MP3 Audio Properties
assert mp3_path.exists(), f"MP3 output does not exist: {mp3_path}"
mp3_size = mp3_path.stat().st_size
assert mp3_size > 10000, f"MP3 output is too small: {mp3_size} bytes"

audio_info = MP3(str(mp3_path))
duration_sec = round(audio_info.info.length, 2)
bitrate_kbps = audio_info.info.bitrate // 1000
sample_rate = audio_info.info.sample_rate

print("\n--- Audio Properties Verification ---")
print(f"File Size: {mp3_size / 1024:.1f} KB")
print(f"Duration: {duration_sec}s ({duration_sec / 60:.2f} mins)")
print(f"Bitrate: {bitrate_kbps} kbps (Target: ~192 kbps)")
print(f"Sample Rate: {sample_rate} Hz")

assert duration_sec >= 5.0, f"Audio duration too short: {duration_sec}s"
assert bitrate_kbps >= 180, f"Bitrate too low: {bitrate_kbps} kbps"

# 6. Verify HTTP Range Audio Streaming
print("\n[4] Testing HTTP Range audio streaming (/audio/{filename})...")
audio_url = f"{SERVER_URL}/audio/{audio_filename}"
r_stream = requests.get(audio_url, headers={"Range": "bytes=0-1023"}, auth=AUTH, timeout=10)
assert r_stream.status_code == 206, f"Expected 206 Partial Content, got {r_stream.status_code}"
assert len(r_stream.content) == 1024, f"Expected 1024 bytes, got {len(r_stream.content)}"
print(f"  [+] HTTP Range streaming verified (206 Partial Content, {len(r_stream.content)} bytes)")

# 7. Verify Podcast RSS Feed (/feed.xml)
print("\n[5] Testing Podcast RSS feed (/feed.xml)...")
r_feed = requests.get(f"{SERVER_URL}/feed.xml", auth=AUTH, timeout=10)
assert r_feed.status_code == 200, f"Expected 200, got {r_feed.status_code}"
assert audio_filename in r_feed.text, "Audio file not found in RSS feed"
assert ready_item["title"] in r_feed.text, "Title not found in RSS feed"
print("  [+] Podcast RSS feed includes the new episode!")

# 8. Verify PDF Download Endpoint (/pdf/{item_id}.pdf)
print(f"\n[6] Testing uploaded PDF download (/pdf/{item_id}.pdf)...")
r_pdf = requests.get(f"{SERVER_URL}/pdf/{item_id}.pdf", auth=AUTH, timeout=10)
assert r_pdf.status_code == 200, f"Expected 200, got {r_pdf.status_code}"
assert "application/pdf" in r_pdf.headers.get("content-type", ""), "Wrong content-type"
assert len(r_pdf.content) == len(SAMPLE_PDF_BYTES), "PDF size mismatch"
print(f"  [+] PDF endpoint verified (HTTP 200, {len(r_pdf.content)} bytes)")

print("\n" + "=" * 60)
print("SUCCESS: Readout PDF Audio Pipeline Verification Passed 100%!")
print("=" * 60)
