import sys
import time
from pathlib import Path
from mutagen.mp3 import MP3

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import poc_pipeline

NIKKEI_URL = "https://bookplus.nikkei.com/atcl/column/112600441/112600002/"
OUTPUT_MP3 = BASE_DIR / "output" / "test_nikkei_voicepeak.mp3"

print("==================================================")
print("Readout - VOICEPEAK Integration & Audio Verification")
print(f"Target URL: {NIKKEI_URL}")
print(f"Narrator: {poc_pipeline.DEFAULT_VOICE}")
print(f"TTS Engine: {poc_pipeline.DEFAULT_TTS_ENGINE}")
print("==================================================")

t_start = time.time()
result = poc_pipeline.run_pipeline(
    url=NIKKEI_URL,
    output_mp3_path=OUTPUT_MP3,
    voice=poc_pipeline.DEFAULT_VOICE,
    engine=poc_pipeline.DEFAULT_TTS_ENGINE,
    max_pages=2
)
t_elapsed = time.time() - t_start

print("\n--- Pipeline Execution Summary ---")
print(f"Title: {result['title']}")
print(f"Pages Merged: {result['page_count']}")
print(f"Script Chars: {result['script_chars']}")
print(f"Total Time: {result['total_time_seconds']:.2f}s")
print(f"Output MP3: {result['output_mp3']}")

# Verification checks
assert OUTPUT_MP3.exists(), f"MP3 output does not exist: {OUTPUT_MP3}"
mp3_size = OUTPUT_MP3.stat().st_size
assert mp3_size > 10000, f"MP3 output is too small: {mp3_size} bytes"

# Check MP3 duration and audio properties with mutagen
audio_info = MP3(str(OUTPUT_MP3))
duration_sec = round(audio_info.info.length, 2)
bitrate_kbps = audio_info.info.bitrate // 1000
sample_rate = audio_info.info.sample_rate
channels = audio_info.info.channels

print("\n--- Audio Properties Verification ---")
print(f"File Size: {mp3_size / 1024:.1f} KB")
print(f"Duration: {duration_sec} seconds ({duration_sec / 60:.2f} minutes)")
print(f"Bitrate: {bitrate_kbps} kbps (Target: 192 kbps)")
print(f"Sample Rate: {sample_rate} Hz")
print(f"Channels: {channels} ({'Stereo' if channels == 2 else 'Mono'})")

assert duration_sec > 10.0, f"Audio duration too short: {duration_sec}s"
assert bitrate_kbps >= 180, f"Bitrate too low: {bitrate_kbps} kbps"

print("\n==================================================")
print("SUCCESS: VOICEPEAK 'Japanese Female 2' Audio Generated & Verified!")
print("==================================================")
