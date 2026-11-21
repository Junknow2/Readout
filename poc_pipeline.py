#!/usr/bin/env python3
"""
Readout - Minimal PoC Pipeline
Step 1: URL Content Extraction (trafilatura)
Step 2: Ear-friendly Rewriting & Translation (Gemini 3.8 Flash)
Step 3: High-Quality Audio Synthesis (Edge-TTS)
"""

import os
import sys
import time
import argparse
import asyncio
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=Warning)
from dotenv import load_dotenv
import trafilatura
from trafilatura.settings import use_config
import requests
from google import genai
from google.genai import types
import edge_tts

# Load environment variables
# Look in current directory first, fallback to LifeLog/.env if needed
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
LIFELOG_ENV_PATH = BASE_DIR.parent / "LifeLog" / ".env"

if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
elif LIFELOG_ENV_PATH.exists():
    load_dotenv(LIFELOG_ENV_PATH)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

DEFAULT_MODEL = "gemini-3.8-flash"
FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
DEFAULT_VOICE = "ja-JP-NanamiNeural"  # or ja-JP-KeitaNeural
DEFAULT_TEST_URL = "https://github.blog/news-insights/product-news/github-copilot-enterprise-is-now-generally-available/"

PODCAST_REWRITE_PROMPT = """\
あなたはマラソンやランニングをしながら聴くための「音声ニュース・ポッドキャスト」の専属プロライター兼ナレーター原稿作成者です。

以下の記事コンテンツを、耳で聴くだけでスッと情景やロジックが頭に入ってくる「ラジオニュース・ポッドキャスト原稿風の自然な日本語」にリライトしてください。

【重要なリライト要件】
1. **耳心地の良さとリズム感**:
   - 1文を短くし、テンポよく心地よい話し言葉（「です・ます」調）で執筆してください。
   - 走っている人が息切れや足音の中でも理解できるよう、複雑な複文や難解な漢語は避け、平易な表現に言い換えてください。
2. **翻訳と口語化**:
   - 英語記事の場合は直訳を排し、自然な日本語のナレーションとして表現してください。
   - 専門用語が出るときは、簡単な補足や例えを交えて耳で理解できるようにしてください。
3. **ノイズの完全除去**:
   - URL、目次、参考文献、コードブロック、複雑な表、注釈番号などは完全に除外してください。
   - Markdownの見出し記号（# や ##）、箇条書き記号（* や - や ・）、アスタリスク、波線、絵文字は一切出力しないでください。音声合成エンジンが記号をそのまま読んでしまうのを防ぐため、句読点（「、」「。」）や疑問符・感嘆符のみを用いたプレーンテキストにしてください。
4. **構成**:
   - 冒頭: 「こんにちは。ランナーズ・ボイスの時間です。今日は、『{title}』についてのトピックをお届けします。」のような、リスナーを引き込む親しみやすい導入。
   - 本文: 核心となるポイントを分かりやすく段階的に解説。
   - 結び: 「ランニングの合間のインプットに、ぜひ役立ててみてくださいね。それでは、今日も良いランを！」といった爽やかな締めくくり。

【元記事タイトル】
{title}

【元記事本文】
{content}
"""


def extract_content_from_url(url: str) -> dict:
    """Extract article title and clean main content using trafilatura."""
    print(f"[*] Step A: Fetching & Extracting content from: {url}")
    t0 = time.time()
    
    # Try fetching with trafilatura
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        print("  [-] trafilatura.fetch_url returned empty, trying requests fallback...")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        downloaded = resp.text

    # Extract metadata
    metadata = trafilatura.extract_metadata(downloaded)
    title = metadata.title if metadata and metadata.title else "無題の記事"

    # Extract main text
    # Configure trafilatura to strip comments and tables
    config = use_config()
    config.set("DEFAULT", "EXTRACTION_COVERAGE", "medium")
    text = trafilatura.extract(
        downloaded,
        config=config,
        include_comments=False,
        include_tables=False,
        include_images=False,
        include_links=False,
        favor_recall=True
    )
    
    if not text:
        raise ValueError(f"Failed to extract readable content from {url}")

    elapsed = time.time() - t0
    print(f"  [+] Extracted title: '{title}' ({len(text)} chars) in {elapsed:.2f}s")
    return {"title": title, "content": text, "url": url}


def rewrite_for_audio(title: str, content: str, model_name: str = DEFAULT_MODEL) -> str:
    """Rewrite extracted text into natural audio podcast script using Gemini."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set. Check .env file.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    
    # Truncate content if excessively long (over 15000 chars) for PoC speed
    clipped_content = content[:15000] if len(content) > 15000 else content

    prompt = PODCAST_REWRITE_PROMPT.format(title=title, content=clipped_content)
    
    models_to_try = [model_name] + [m for m in FALLBACK_MODELS if m != model_name]
    
    last_error = None
    for model in models_to_try:
        try:
            print(f"[*] Step B: Rewriting content for ear listening with {model}...")
            t0 = time.time()
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.7,
                )
            )
            script_text = response.text.strip()
            elapsed = time.time() - t0
            print(f"  [+] Successfully generated script ({len(script_text)} chars) using {model} in {elapsed:.2f}s")
            return script_text
        except Exception as e:
            print(f"  [-] Failed with model {model}: {e}")
            last_error = e

    raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")


async def generate_speech_async(text: str, output_path: Path, voice: str = DEFAULT_VOICE):
    """Generate MP3 audio from text using Edge-TTS."""
    print(f"[*] Step C: Synthesizing speech with edge-tts (voice: {voice})...")
    t0 = time.time()
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(output_path))
    
    elapsed = time.time() - t0
    file_size_kb = output_path.stat().st_size / 1024
    print(f"  [+] Generated audio file: {output_path} ({file_size_kb:.1f} KB) in {elapsed:.2f}s")


def run_pipeline(url: str, output_mp3_path: Path, voice: str = DEFAULT_VOICE, model: str = DEFAULT_MODEL) -> dict:
    """Run full pipeline: Extract -> Rewrite -> TTS."""
    total_t0 = time.time()
    print("=" * 60)
    print("Readout Minimal PoC Pipeline Starting")
    print(f"Target URL: {url}")
    print(f"Voice Model: {voice}")
    print(f"LLM Model: {model}")
    print("=" * 60)

    # Step A: Extract
    extracted = extract_content_from_url(url)

    # Step B: Rewrite
    script_text = rewrite_for_audio(extracted["title"], extracted["content"], model_name=model)

    # Save script text for inspection alongside MP3
    txt_path = output_mp3_path.with_suffix(".txt")
    txt_path.write_text(script_text, encoding="utf-8")
    print(f"  [+] Saved script text to: {txt_path}")

    # Step C: TTS
    asyncio.run(generate_speech_async(script_text, output_mp3_path, voice=voice))

    total_time = time.time() - total_t0
    file_size_kb = output_mp3_path.stat().st_size / 1024

    result = {
        "url": url,
        "title": extracted["title"],
        "original_chars": len(extracted["content"]),
        "script_chars": len(script_text),
        "script_preview": script_text[:250] + "...",
        "output_mp3": str(output_mp3_path),
        "output_txt": str(txt_path),
        "file_size_kb": file_size_kb,
        "total_time_seconds": total_time,
        "voice": voice,
        "model": model,
    }

    print("=" * 60)
    print(f"Pipeline Completed Successfully in {total_time:.2f} seconds!")
    print(f"Audio Output: {output_mp3_path} ({file_size_kb:.1f} KB)")
    print("=" * 60)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Readout PoC: Web URL to Podcast Audio")
    parser.add_argument("url", nargs="?", default=DEFAULT_TEST_URL, help=f"Target URL (default: {DEFAULT_TEST_URL})")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help=f"Edge-TTS voice (default: {DEFAULT_VOICE})")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini model (default: {DEFAULT_MODEL})")
    parser.add_argument("--output", default=str(BASE_DIR / "output" / "test_output.mp3"), help="Output MP3 path")
    args = parser.parse_args()

    run_pipeline(
        url=args.url,
        output_mp3_path=Path(args.output).resolve(),
        voice=args.voice,
        model=args.model
    )
