#!/usr/bin/env python3
"""
Readout - Minimal PoC Pipeline
Step 1: URL Content Extraction (trafilatura + multipage crawler)
Step 2: Ear-friendly Rewriting & Translation (Gemini 3.8 Flash)
Step 3: High-Quality Audio Synthesis (VOICEPEAK with Edge-TTS Fallback)
"""

import os
import sys
import time
import re
import argparse
import asyncio
import shutil
import subprocess
import tempfile
import threading
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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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
FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-3.5-flash-lite"]
VOICEPEAK_PATH = Path(os.getenv("VOICEPEAK_PATH", r"C:\Program Files\VOICEPEAK\voicepeak.exe"))
DEFAULT_VOICE = "Japanese Female 2"  # VOICEPEAK narrator
DEFAULT_TTS_ENGINE = "voicepeak"    # "voicepeak" or "edge_tts"
FALLBACK_EDGE_VOICE = "ja-JP-NanamiNeural"  # Fallback voice when using Edge-TTS
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

PDF_REWRITE_PROMPT = """\
あなたは歩行中やランニング中、移動中に耳で聴いてインプットするための「音声ナレーション原稿」の専属プロライターです。

添付されたPDFドキュメント（学術論文、技術レポート、調査資料、スライド、白書など）を精読し、耳で聴くだけでスッと情景や論理展開が頭に入ってくる「自然で無駄のない日本語ナレーション原稿」を作成してください。

【重要なリライト要件】
1. **タイトルの特定と構成**:
   - ドキュメント全体から最も適切で簡潔なタイトル（原題、論文名、または日本語訳タイトル）を特定してください。
   - 出力の1行目は必ず「TITLE: {特定したタイトル}」としてください。
   - 2行目以降のナレーション原稿（本文）は、必ず「{特定したタイトル}についての解説。」というシンプルな宣言から始めて、即座に本文の解説に入ってください。
2. **冒頭・結びの無駄な挨拶の完全排除**:
   - 「こんにちは」「〇〇の時間です」「今日も良い一日を」「リスナーの皆さん」といったラジオ風・ポッドキャスト風の前置き挨拶や結びの掛け声は【一切出力しないでください】。
   - 結びも応援や挨拶は入れず、ドキュメントの核心・結論のまとめで潔く終了してください。
3. **ノイズの完全無視・除外**:
   - 脚注（Footnotes）、ヘッダー、フッター、ページ番号、図表番号や細かな注記、参考文献リスト（References / Bibliography）、謝辞（Acknowledgements）などは【完全に無視・除外】してください。
   - Markdownの見出し記号（# や ##）、箇条書き記号（* や - や ・）、アスタリスク、波線、絵文字は一切出力しないでください。音声合成エンジンが記号をそのまま読んでしまうのを防ぐため、句読点（「、」「。」）や疑問符・感嘆符のみを用いたプレーンテキストにしてください。
4. **構造・2段組のシームレス結合**:
   - 2段組（2-column layout）やページ跨ぎの文章を、文脈や論理の途切れがないようにシームレスに結合して読んでください。
   - 複雑な数式や図表の数値羅列は避け、その意味や導かれる結論を言葉で分かりやすく噛み砕いて説明してください。
5. **耳心地の良さと翻訳・口語化**:
   - 英文・和文問わず、耳で聴きやすい自然な日本語ナレーション（「です・ます」調）にリライトしてください（英語論文等の場合は自然な日本語翻訳）。
   - 1文を短くし、テンポよく心地よい話し言葉にしてください。難解な専門用語が出るときは平易な補足や言い換えを交えてください。

【出力形式】
TITLE: {特定したタイトル}
{特定したタイトル}についての解説。{ここから本文のナレーション原稿...}
"""


def sanitize_script_for_tts(text: str) -> str:
    """Strip markdown markers and symbols that TTS engines might read literally."""
    text = re.sub(r'[*_#`~]', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


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
            return sanitize_script_for_tts(script_text)
        except Exception as e:
            print(f"  [-] Failed with model {model}: {e}")
            last_error = e

    raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")


def rewrite_pdf_for_audio(
    pdf_bytes: bytes,
    filename: str = "",
    model_name: str = DEFAULT_MODEL
) -> dict:
    """
    Rewrite PDF document into natural audio podcast narration script using Gemini multimodal input.
    Extracts document title, excludes footnotes/references/headers/footers, and rewrites body for listening.
    Returns dict containing 'title', 'script_text', and 'model'.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set. Check .env file.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    pdf_part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")

    models_to_try = [model_name] + [m for m in FALLBACK_MODELS if m != model_name]

    last_error = None
    for model in models_to_try:
        try:
            print(f"[*] Step B (PDF): Analyzing & rewriting PDF with {model}...")
            t0 = time.time()
            response = client.models.generate_content(
                model=model,
                contents=[pdf_part, PDF_REWRITE_PROMPT],
                config=types.GenerateContentConfig(
                    temperature=0.7,
                )
            )
            raw_text = response.text.strip()
            elapsed = time.time() - t0
            print(f"  [+] Successfully generated PDF script ({len(raw_text)} chars) using {model} in {elapsed:.2f}s")

            # Parse TITLE and script body
            lines = raw_text.splitlines()
            extracted_title = ""
            script_body_lines = []

            for idx, line in enumerate(lines):
                line_stripped = line.strip()
                if line_stripped.upper().startswith("TITLE:"):
                    if not extracted_title:
                        extracted_title = line_stripped[6:].strip()
                else:
                    script_body_lines.append(line)

            script_text = "\n".join(script_body_lines).strip()

            # Fallback title if not detected in output
            if not extracted_title:
                if filename:
                    extracted_title = Path(filename).stem.replace("_", " ").strip()
                else:
                    extracted_title = "PDFドキュメント"

            # Ensure script body starts with the required phrase
            expected_prefix = f"{extracted_title}についての解説。"
            if not script_text.startswith(expected_prefix) and not script_text.startswith(f"{extracted_title}についての解説"):
                script_text = f"{expected_prefix}\n\n{script_text}"

            script_text = sanitize_script_for_tts(script_text)

            return {
                "title": extracted_title,
                "script_text": script_text,
                "model": model,
            }
        except Exception as e:
            print(f"  [-] Failed with model {model}: {e}")
            last_error = e

    raise RuntimeError(f"All Gemini models failed for PDF. Last error: {last_error}")


def split_text_for_voicepeak(text: str, max_chars: int = 120) -> list[str]:
    """
    Split text into chunks of <= max_chars (VOICEPEAK single-run limit is 140 chars).
    Splits naturally on sentence boundaries (。, ！, ？, \n) and phrase boundaries (、).
    """
    lines = text.strip().splitlines()
    sentences = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = re.split(r'([。！？\?!])', line)
        for i in range(0, len(parts), 2):
            part = parts[i]
            punct = parts[i+1] if i + 1 < len(parts) else ''
            seg = (part + punct).strip()
            if not seg:
                continue
            if len(seg) > max_chars:
                sub_parts = re.split(r'([、,])', seg)
                sub_cur = ''
                for j in range(0, len(sub_parts), 2):
                    sp = sub_parts[j]
                    punc = sub_parts[j+1] if j + 1 < len(sub_parts) else ''
                    sseg = sp + punc
                    if not sseg:
                        continue
                    if len(sub_cur) + len(sseg) <= max_chars:
                        sub_cur += sseg
                    else:
                        if sub_cur:
                            sentences.append(sub_cur)
                        while len(sseg) > max_chars:
                            sentences.append(sseg[:max_chars])
                            sseg = sseg[max_chars:]
                        sub_cur = sseg
                if sub_cur:
                    sentences.append(sub_cur)
            else:
                sentences.append(seg)

    chunks = []
    cur = ''
    for s in sentences:
        if len(cur) + len(s) <= max_chars:
            cur += s
        else:
            if cur:
                chunks.append(cur)
            cur = s
    if cur:
        chunks.append(cur)
    return chunks


_VOICEPEAK_LOCK = threading.Lock()


def _synthesize_voicepeak_sync(text: str, output_mp3_path: Path, narrator: str = DEFAULT_VOICE) -> None:
    """Synchronous worker to run VOICEPEAK CLI in chunks and convert/concat WAV to MP3 via ffmpeg."""
    if not VOICEPEAK_PATH.exists():
        raise FileNotFoundError(f"VOICEPEAK executable not found at {VOICEPEAK_PATH}")

    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"

    output_mp3_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time() * 1000)
    pid = os.getpid()
    work_dir = output_mp3_path.parent / f"_vp_tmp_{pid}_{timestamp}"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        chunks = split_text_for_voicepeak(text, max_chars=120)
        if not chunks:
            raise ValueError("No text provided for speech synthesis.")

        wav_files = []
        with _VOICEPEAK_LOCK:
            for idx, chunk in enumerate(chunks):
                temp_txt = work_dir / f"chunk_{idx:04d}.txt"
                temp_wav = work_dir / f"chunk_{idx:04d}.wav"
                temp_txt.write_text(chunk, encoding="utf-8")

                vp_cmd = [
                    str(VOICEPEAK_PATH),
                    "-t", str(temp_txt),
                    "-n", narrator,
                    "-o", str(temp_wav)
                ]
                res = subprocess.run(vp_cmd, capture_output=True, text=True, check=False)
                if res.returncode != 0:
                    err_msg = (res.stderr or res.stdout or "").strip()
                    raise RuntimeError(f"VOICEPEAK failed on chunk {idx} (code {res.returncode}): {err_msg}")

                if not temp_wav.exists() or temp_wav.stat().st_size == 0:
                    raise RuntimeError(f"VOICEPEAK produced no output file for chunk {idx}")

                wav_files.append(temp_wav)

        # If single chunk, convert directly; if multiple chunks, concatenate using ffmpeg
        if len(wav_files) == 1:
            ff_cmd = [
                ffmpeg_bin,
                "-y",
                "-i", str(wav_files[0]),
                "-b:a", "192k",
                str(output_mp3_path)
            ]
        else:
            concat_list = work_dir / "concat_list.txt"
            with open(concat_list, "w", encoding="utf-8") as f:
                for w in wav_files:
                    escaped = str(w.resolve()).replace("\\", "/")
                    f.write(f"file '{escaped}'\n")

            ff_cmd = [
                ffmpeg_bin,
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list),
                "-b:a", "192k",
                str(output_mp3_path)
            ]

        ff_res = subprocess.run(ff_cmd, capture_output=True, text=True, check=False)
        if ff_res.returncode != 0:
            err_msg = (ff_res.stderr or ff_res.stdout or "").strip()
            raise RuntimeError(f"ffmpeg conversion failed (code {ff_res.returncode}): {err_msg}")

        if not output_mp3_path.exists() or output_mp3_path.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg produced no MP3 file at {output_mp3_path}")

    finally:
        # Clean up temporary working directory
        if work_dir.exists():
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:
                pass


async def generate_speech_voicepeak(text: str, output_path: Path, narrator: str = DEFAULT_VOICE):
    """Generate MP3 audio from text using local VOICEPEAK CLI and ffmpeg."""
    print(f"[*] Step C: Synthesizing speech with VOICEPEAK (narrator: {narrator})...")
    t0 = time.time()
    await asyncio.to_thread(_synthesize_voicepeak_sync, text, output_path, narrator)
    elapsed = time.time() - t0
    file_size_kb = output_path.stat().st_size / 1024
    print(f"  [+] Generated audio file via VOICEPEAK: {output_path} ({file_size_kb:.1f} KB) in {elapsed:.2f}s")


async def generate_speech_edge_tts(text: str, output_path: Path, voice: str = FALLBACK_EDGE_VOICE):
    """Generate MP3 audio from text using Edge-TTS."""
    print(f"[*] Step C: Synthesizing speech with edge-tts (voice: {voice})...")
    t0 = time.time()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(output_path))
    elapsed = time.time() - t0
    file_size_kb = output_path.stat().st_size / 1024
    print(f"  [+] Generated audio file via edge-tts: {output_path} ({file_size_kb:.1f} KB) in {elapsed:.2f}s")


async def generate_speech_async(text: str, output_path: Path, voice: str = DEFAULT_VOICE, engine: str = DEFAULT_TTS_ENGINE):
    """
    Generate MP3 audio from text.
    Primary engine is VOICEPEAK (default: 'Japanese Female 2').
    Automatically falls back to Edge-TTS ('ja-JP-NanamiNeural') if VOICEPEAK fails or is unavailable.
    """
    chosen_engine = engine
    if voice and voice.startswith("ja-JP-"):
        chosen_engine = "edge_tts"

    if chosen_engine == "voicepeak":
        try:
            await generate_speech_voicepeak(text, output_path, narrator=voice)
            return
        except Exception as e:
            print(f"  [!] VOICEPEAK synthesis failed ({e}). Automatically falling back to Edge-TTS ({FALLBACK_EDGE_VOICE})...")

    edge_voice = voice if (voice and voice.startswith("ja-JP-")) else FALLBACK_EDGE_VOICE
    await generate_speech_edge_tts(text, output_path, voice=edge_voice)


def run_pipeline(url: str, output_mp3_path: Path, voice: str = DEFAULT_VOICE, model: str = DEFAULT_MODEL, max_pages: int = 10, engine: str = DEFAULT_TTS_ENGINE) -> dict:
    """Run full pipeline: Multipage Extract -> Rewrite -> TTS."""
    total_t0 = time.time()
    print("=" * 60)
    print("Readout PoC Pipeline Starting (with Multipage Support)")
    print(f"Target URL: {url}")
    print(f"TTS Engine: {engine}")
    print(f"Voice / Narrator: {voice}")
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
    asyncio.run(generate_speech_async(script_text, output_mp3_path, voice=voice, engine=engine))

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
        "engine": engine,
        "voice": voice,
        "model": model,
    }

    print("=" * 60)
    print(f"Pipeline Completed Successfully in {total_time:.2f} seconds!")
    print(f"Pages Merged: {result['page_count']}")
    print(f"Audio Output: {output_mp3_path} ({file_size_kb:.1f} KB)")
    print("=" * 60)
    return result


def run_pdf_pipeline(
    pdf_bytes: bytes,
    output_mp3_path: Path,
    filename: str = "",
    voice: str = DEFAULT_VOICE,
    model: str = DEFAULT_MODEL,
    engine: str = DEFAULT_TTS_ENGINE
) -> dict:
    """Run full PDF pipeline: Multimodal Gemini rewrite -> TTS (VOICEPEAK with Edge-TTS fallback)."""
    total_t0 = time.time()
    print("=" * 60)
    print("Readout PDF Pipeline Starting")
    print(f"Filename: {filename}")
    print(f"TTS Engine: {engine}")
    print(f"Voice / Narrator: {voice}")
    print(f"LLM Model: {model}")
    print("=" * 60)

    rewritten = rewrite_pdf_for_audio(pdf_bytes, filename=filename, model_name=model)
    title = rewritten["title"]
    script_text = rewritten["script_text"]

    output_mp3_path.parent.mkdir(parents=True, exist_ok=True)
    txt_path = output_mp3_path.with_suffix(".txt")
    txt_path.write_text(script_text, encoding="utf-8")
    print(f"  [+] Saved script text to: {txt_path}")

    asyncio.run(generate_speech_async(script_text, output_mp3_path, voice=voice, engine=engine))

    total_time = time.time() - total_t0
    file_size_kb = output_mp3_path.stat().st_size / 1024

    result = {
        "title": title,
        "filename": filename,
        "script_chars": len(script_text),
        "script_preview": script_text[:250] + "...",
        "output_mp3": str(output_mp3_path),
        "output_txt": str(txt_path),
        "file_size_kb": file_size_kb,
        "total_time_seconds": total_time,
        "engine": engine,
        "voice": voice,
        "model": model,
    }

    print("=" * 60)
    print(f"PDF Pipeline Completed Successfully in {total_time:.2f} seconds!")
    print(f"Title: {title}")
    print(f"Audio Output: {output_mp3_path} ({file_size_kb:.1f} KB)")
    print("=" * 60)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Readout PoC: Web URL or PDF to Podcast Audio")
    parser.add_argument("target", nargs="?", default=DEFAULT_TEST_URL, help=f"Target URL or local PDF file path (default: {DEFAULT_TEST_URL})")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help=f"Voice / Narrator name (default: {DEFAULT_VOICE})")
    parser.add_argument("--engine", default=DEFAULT_TTS_ENGINE, choices=["voicepeak", "edge_tts"], help=f"TTS engine (default: {DEFAULT_TTS_ENGINE})")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini model (default: {DEFAULT_MODEL})")
    parser.add_argument("--max-pages", type=int, default=10, help="Max pages to crawl for web URLs (default: 10)")
    parser.add_argument("--output", default=str(BASE_DIR / "output" / "test_output.mp3"), help="Output MP3 path")
    args = parser.parse_args()

    target_path = Path(args.target)
    out_path = Path(args.output).resolve()

    if target_path.exists() and target_path.suffix.lower() == ".pdf":
        print(f"Detected local PDF file: {target_path}")
        pdf_data = target_path.read_bytes()
        run_pdf_pipeline(
            pdf_bytes=pdf_data,
            output_mp3_path=out_path,
            filename=target_path.name,
            voice=args.voice,
            engine=args.engine,
            model=args.model
        )
    else:
        run_pipeline(
            url=args.target,
            output_mp3_path=out_path,
            voice=args.voice,
            engine=args.engine,
            model=args.model,
            max_pages=args.max_pages
        )
