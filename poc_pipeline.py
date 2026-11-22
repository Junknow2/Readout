#!/usr/bin/env python3
"""
Readout - Minimal PoC Pipeline
Step 1: URL Content Extraction (trafilatura + multipage crawler)
Step 2: Ear-friendly Rewriting & Translation (Gemini 3.8 Flash)
Step 3: High-Quality Audio Synthesis (Edge-TTS)
"""

import os
import sys
import time
import re
import argparse
import asyncio
import warnings
from pathlib import Path
from urllib.parse import urljoin, urlparse

warnings.filterwarnings("ignore", category=Warning)
from dotenv import load_dotenv
import trafilatura
from trafilatura.settings import use_config
import lxml.html
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
あなたは歩行中や移動中に耳で聴いてインプットするための「音声ナレーション原稿」の専属プロライターです。

以下の記事コンテンツを、耳で聴くだけでスッと情景やロジックが頭に入ってくる「自然で無駄のない日本語ナレーション」にリライトしてください。

【重要なリライト要件】
1. **冒頭・結びの無駄な挨拶の完全排除**:
   - 「こんにちは」「〇〇の時間です」「今日も良い一日を」「良いランを」といったラジオ風・ポッドキャスト風の前置き挨拶や結びの掛け声は【一切出力しないでください】。
   - 冒頭は必ず「{site_name}、『{title}』。」というシンプルな宣言から始めて、即座に本文の解説へ入ってください。
   - 結びも応援や挨拶は入れず、記事のまとめ・結論で潔く終了してください。
2. **耳心地の良さとリズム感**:
   - 1文を短くし、テンポよく心地よい話し言葉（「です・ます」調）で執筆してください。
   - 移動中や環境音の中でも理解できるよう、複雑な複文や難解な漢語は避け、平易な表現に言い換えてください。
3. **翻訳と口語化**:
   - 英語記事の場合は直訳を排し、自然な日本語のナレーションとして表現してください。
   - 専門用語が出るときは、簡単な補足や例えを交えて耳で理解できるようにしてください。
4. **ノイズの完全除去**:
   - URL、目次、参考文献、コードブロック、複雑な表、注釈番号などは完全に除外してください。
   - Markdownの見出し記号（# や ##）、箇条書き記号（* や - や ・）、アスタリスク、波線、絵文字は一切出力しないでください。音声合成エンジンが記号をそのまま読んでしまうのを防ぐため、句読点（「、」「。」）や疑問符・感嘆符のみを用いたプレーンテキストにしてください。
5. **複数ページ・連載記事の自然な統合**:
   - 元記事が「ページ2」「続き」や複数回に分割されている場合でも、1本の一貫したストーリー・解説としてスムーズに繋げてください。「次のページに移ります」「ページ2では」といった不自然なメタ言及は避け、「続いて、後半では」「さらに別の側面として」のように自然なナレーションの展開としてまとめてください。
6. **構成**:
   - 冒頭: 「{site_name}、『{title}』。」から即座に本文の核心・背景へ導入。
   - 本文: 核心となるポイントを分かりやすくロジカルに解説。
   - 結び: 記事の要点・結論の要約でシンプルに終了。

【媒体名】
{site_name}

【元記事タイトル】
{title}

【元記事本文】
{content}
"""


def clean_path_stem(path: str) -> str:
    """Strip extension like .html, .htm and trailing slashes."""
    p = path.rstrip('/')
    p = re.sub(r'\.(html?|php|aspx|jsp)$', '', p, flags=re.I)
    return p


def is_valid_next_url(base_url: str, next_url: str, allow_series: bool = True) -> bool:
    """Validate that next_url is a safe and plausible continuation of base_url."""
    if not next_url or base_url == next_url:
        return False

    b = urlparse(base_url)
    n = urlparse(next_url)

    # Must share the same hostname
    if b.netloc.lower() != n.netloc.lower():
        return False

    # Disallow protocols other than http/https
    if n.scheme not in ('http', 'https'):
        return False

    b_stem = clean_path_stem(b.path)
    n_stem = clean_path_stem(n.path)

    # Case 1: Same base path with query param change (e.g. ?P=2, ?page=2, ?p=2)
    if b_stem == n_stem and b.query != n.query:
        return True

    # Case 2: Numbered suffix in path: e.g. news005 -> news005_2, or /articles/123 -> /articles/123/2
    b_base = re.sub(r'[_/-](?:p(?:age)?)?\d+$', '', b_stem, flags=re.I)
    if n_stem.startswith(b_base) and (n_stem != b_stem):
        return True

    # Case 3: Series link under same parent path (e.g. Nikkei column /atcl/column/112600441/112600002/ -> /atcl/column/112600441/112600003/)
    if allow_series:
        b_parent = '/'.join(b.path.rstrip('/').split('/')[:-1])
        n_parent = '/'.join(n.path.rstrip('/').split('/')[:-1])
        if b_parent and b_parent == n_parent:
            return True

    return False


def find_next_page_url(current_url: str, html_content: str, current_page_num: int = 1) -> str | None:
    """Detect next page URL from HTML content using link rel, pager, or semantic hints."""
    try:
        doc = lxml.html.fromstring(html_content)
    except Exception:
        return None

    # Negative pattern: Avoid moving to previous pages or unrelated index/navigation links
    prev_or_index_pattern = re.compile(r'^(前へ|前のページ|前ページ|前の記事|Prev|Previous|戻る|最初|TOP|トップ|一覧|＜|<|«)$', re.I)

    # 1. Standard <link rel="next" href="...">
    link_next = doc.xpath('//link[contains(translate(@rel, "NEXT", "next"), "next")]/@href')
    if link_next:
        cand = urljoin(current_url, link_next[0].strip())
        if is_valid_next_url(current_url, cand):
            return cand

    # 2. Standard <a rel="next" href="...">
    a_next = doc.xpath('//a[contains(translate(@rel, "NEXT", "next"), "next")]/@href')
    if a_next:
        cand = urljoin(current_url, a_next[0].strip())
        if is_valid_next_url(current_url, cand):
            return cand

    # 3. Explicit data-content-type="nextArticle" (e.g. Nikkei BookPlus)
    next_art = doc.xpath('//*[@data-content-type="nextArticle"]//a[@href]/@href')
    if next_art:
        cand = urljoin(current_url, next_art[0].strip())
        if is_valid_next_url(current_url, cand, allow_series=True):
            return cand

    # 4. Pager / pagination containers
    pager_xpath = (
        '//*[contains(@class, "pager") or contains(@class, "pagination") or contains(@class, "paging") '
        'or contains(@class, "page-nav") or contains(@class, "pageNav") or contains(@class, "pageList") '
        'or contains(@class, "articlePrevNextLink") or contains(@class, "wp-pagenavi") '
        'or contains(@class, "page-numbers")]//a[@href]'
    )
    for a in doc.xpath(pager_xpath):
        href = a.get('href', '').strip()
        text = ''.join(a.xpath('.//text()')).strip()
        if not href or href.startswith('#') or href.startswith('javascript:'):
            continue
        if prev_or_index_pattern.search(text):
            continue

        cand = urljoin(current_url, href)
        if not is_valid_next_url(current_url, cand, allow_series=True):
            continue

        # Next page text
        if re.search(r'^(次のページ|次へ|次ページ|次の記事|Next|next|＞|>|»)$', text, re.I):
            return cand

        # Next page number: text is current_page_num + 1
        if text.isdigit() and int(text) == current_page_num + 1:
            return cand

    # 5. Fallback: Search any anchor that explicitly says "次のページ" or "次ページ"
    for a in doc.xpath('//a[@href]'):
        href = a.get('href', '').strip()
        text = ''.join(a.xpath('.//text()')).strip()
        if not href or href.startswith('#') or href.startswith('javascript:'):
            continue
        if prev_or_index_pattern.search(text):
            continue

        if re.search(r'^(次のページ|次ページ|次のページへ)$', text):
            cand = urljoin(current_url, href)
            if is_valid_next_url(current_url, cand):
                return cand

    return None


def fetch_page_html(url: str) -> str:
    """Fetch raw HTML for a URL using trafilatura or requests fallback with proper encoding."""
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=15)
        resp.encoding = resp.apparent_encoding or resp.encoding
        downloaded = resp.text
    return downloaded


def extract_single_page(html_content: str, url: str) -> dict:
    """Extract metadata and clean main body text from raw HTML using trafilatura."""
    metadata = trafilatura.extract_metadata(html_content)
    title = metadata.title if metadata and metadata.title else ""
    site_name = ""
    if metadata and getattr(metadata, "sitename", None):
        site_name = metadata.sitename
    if not site_name:
        try:
            doc = lxml.html.fromstring(html_content)
            og_sites = doc.xpath('//meta[@property="og:site_name"]/@content')
            if og_sites and og_sites[0].strip():
                site_name = og_sites[0].strip()
        except Exception:
            pass
    if not site_name:
        parsed_netloc = urlparse(url).netloc
        site_name = parsed_netloc.replace("www.", "")

    config = use_config()
    config.set("DEFAULT", "EXTRACTION_COVERAGE", "medium")
    text = trafilatura.extract(
        html_content,
        config=config,
        include_comments=False,
        include_tables=False,
        include_images=False,
        include_links=False,
        favor_recall=True
    )
    return {"title": title, "content": text or "", "site_name": site_name, "url": url}


def extract_multipage_content(start_url: str, max_pages: int = 10) -> dict:
    """
    Extract readable content across multiple paginated pages or column series,
    merging body text while stripping duplicate headers and footers.
    """
    print(f"[*] Step A: Fetching & Extracting content from: {start_url}")
    t0 = time.time()

    visited = set()
    pages = []
    current_url = start_url

    for page_idx in range(1, max_pages + 1):
        # Normalize URL to prevent loops
        norm_url = current_url.split('#')[0].rstrip('/')
        if norm_url in visited:
            print(f"  [-] Already visited {norm_url}, stopping pagination loop.")
            break
        visited.add(norm_url)

        print(f"  [+] Fetching page {page_idx}: {current_url}")
        html = fetch_page_html(current_url)
        if not html:
            print(f"  [-] Failed to fetch HTML for {current_url}")
            break

        page_data = extract_single_page(html, current_url)
        if not page_data["content"]:
            print(f"  [-] No readable text content extracted from page {page_idx}, stopping.")
            break

        # Check for duplicate content (e.g. server ignoring pagination query params like ?p=2)
        if pages and page_data["content"] == pages[-1]["content"]:
            print(f"  [-] Page {page_idx} content is identical to page {page_idx-1}, stopping.")
            break

        pages.append(page_data)

        # Look for next page
        next_url = find_next_page_url(current_url, html, current_page_num=page_idx)
        if not next_url:
            break

        print(f"  [>] Next page detected: {next_url}")
        current_url = next_url

    if not pages:
        raise ValueError(f"Failed to extract readable content from {start_url}")

    # Primary title from first page
    main_title = pages[0]["title"] if pages[0]["title"] else "無題の記事"

    # Merge content
    if len(pages) == 1:
        merged_text = pages[0]["content"]
    else:
        parts = [pages[0]["content"]]
        for idx, p in enumerate(pages[1:], start=2):
            p_content = p["content"].strip()
            # If the page begins with repeated title, strip it
            if p["title"] and p_content.startswith(p["title"]):
                p_content = p_content[len(p["title"]):].strip()
            elif main_title and p_content.startswith(main_title):
                p_content = p_content[len(main_title):].strip()

            header = f"\n\n--- 続き（ページ {idx}） ---\n"
            if p["title"] and p["title"] != main_title:
                header = f"\n\n--- 続き（ページ {idx}：{p['title']}） ---\n"
            parts.append(header + p_content)

        merged_text = "".join(parts)

    main_site_name = pages[0].get("site_name") or ""
    elapsed = time.time() - t0
    print(f"  [+] Extracted {len(pages)} pages ({len(merged_text)} total chars) from {main_site_name} in {elapsed:.2f}s")
    return {
        "title": main_title,
        "site_name": main_site_name,
        "content": merged_text,
        "url": start_url,
        "pages": [p["url"] for p in pages],
        "page_count": len(pages),
        "page_titles": [p["title"] for p in pages],
    }


def extract_content_from_url(url: str, max_pages: int = 10) -> dict:
    """Extract article title and clean main content, automatically following pagination."""
    return extract_multipage_content(url, max_pages=max_pages)


def rewrite_for_audio(title: str, content: str, site_name: str = "", model_name: str = DEFAULT_MODEL) -> str:
    """Rewrite extracted text into natural audio podcast script using Gemini."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set. Check .env file.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    
    # Truncate content if excessively long (over 25000 chars)
    clipped_content = content[:25000] if len(content) > 25000 else content

    clean_site_name = site_name.strip() if site_name else "Web記事"
    prompt = PODCAST_REWRITE_PROMPT.format(site_name=clean_site_name, title=title, content=clipped_content)
    
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


def run_pipeline(url: str, output_mp3_path: Path, voice: str = DEFAULT_VOICE, model: str = DEFAULT_MODEL, max_pages: int = 10) -> dict:
    """Run full pipeline: Multipage Extract -> Rewrite -> TTS."""
    total_t0 = time.time()
    print("=" * 60)
    print("Readout PoC Pipeline Starting (with Multipage Support)")
    print(f"Target URL: {url}")
    print(f"Voice Model: {voice}")
    print(f"LLM Model: {model}")
    print("=" * 60)

    # Step A: Multipage Extract
    extracted = extract_content_from_url(url, max_pages=max_pages)

    # Step B: Rewrite
    script_text = rewrite_for_audio(
        extracted["title"],
        extracted["content"],
        site_name=extracted.get("site_name", ""),
        model_name=model
    )

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
        "page_count": extracted.get("page_count", 1),
        "pages": extracted.get("pages", [url]),
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
    print(f"Pages Merged: {result['page_count']}")
    print(f"Audio Output: {output_mp3_path} ({file_size_kb:.1f} KB)")
    print("=" * 60)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Readout PoC: Web URL to Podcast Audio (Multipage)")
    parser.add_argument("url", nargs="?", default=DEFAULT_TEST_URL, help=f"Target URL (default: {DEFAULT_TEST_URL})")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help=f"Edge-TTS voice (default: {DEFAULT_VOICE})")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini model (default: {DEFAULT_MODEL})")
    parser.add_argument("--max-pages", type=int, default=10, help="Max pages to crawl (default: 10)")
    parser.add_argument("--output", default=str(BASE_DIR / "output" / "test_output.mp3"), help="Output MP3 path")
    args = parser.parse_args()

    run_pipeline(
        url=args.url,
        output_mp3_path=Path(args.output).resolve(),
        voice=args.voice,
        model=args.model,
        max_pages=args.max_pages
    )
