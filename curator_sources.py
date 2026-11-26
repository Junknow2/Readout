#!/usr/bin/env python3
"""
Readout - Personal Knowledge Curator: Source Discovery & Strict Verification
Crawls priority RSS/Atom feeds, OpenAlex, J-STAGE, and optional Gemini Search Grounding.
Verifies every candidate URL via real HTTP fetch + trafilatura extraction (or official API abstract),
mechanically rejecting 404s, login walls, paywalls, and insufficient body text.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import feedparser
import requests
import trafilatura
from trafilatura.settings import use_config

import poc_pipeline

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# Priority RSS / Atom Sources organized by editorial track
PRIORITY_FEEDS: List[Dict[str, Any]] = [
    # --- 経営・組織・戦略 (Core) ---
    {
        "name": "Knowledge at Wharton",
        "url": "https://knowledge.wharton.upenn.edu/feed/",
        "category": "management",
        "track_hint": "core",
        "lang": "en",
        "domain": "knowledge.wharton.upenn.edu",
    },
    {
        "name": "MIT Sloan Management Review",
        "url": "https://sloanreview.mit.edu/feed/",
        "category": "management",
        "track_hint": "core",
        "lang": "en",
        "domain": "sloanreview.mit.edu",
    },
    {
        "name": "RIETI コラム",
        "url": "https://www.rieti.go.jp/jp/rss/columns.xml",
        "category": "management",
        "track_hint": "core",
        "lang": "ja",
        "domain": "www.rieti.go.jp",
    },
    {
        "name": "Behavioral Scientist",
        "url": "https://behavioralscientist.org/feed/",
        "category": "behavioral_science",
        "track_hint": "discovery",
        "lang": "en",
        "domain": "behavioralscientist.org",
    },
    {
        "name": "The Conversation",
        "url": "https://theconversation.com/global/articles.atom",
        "category": "society_business",
        "track_hint": "discovery",
        "lang": "en",
        "domain": "theconversation.com",
    },
    # --- マーケティング・リサーチ (Core) ---
    {
        "name": "Greenbook",
        "url": "https://www.greenbook.org/mr/feed/",
        "category": "marketing_research",
        "track_hint": "core",
        "lang": "en",
        "domain": "www.greenbook.org",
    },
    {
        "name": "Quirk's Marketing Research",
        "url": "https://www.quirks.com/rss",
        "category": "marketing_research",
        "track_hint": "core",
        "lang": "en",
        "domain": "www.quirks.com",
    },
    {
        "name": "ESOMAR Research World",
        "url": "https://researchworld.com/feed/",
        "category": "marketing_research",
        "track_hint": "core",
        "lang": "en",
        "domain": "researchworld.com",
    },
    # --- 幅広い知的発見 (Discovery / Wild Card) ---
    {
        "name": "Our World in Data",
        "url": "https://ourworldindata.org/atom.xml",
        "category": "data_science_world",
        "track_hint": "wildcard",
        "lang": "en",
        "domain": "ourworldindata.org",
    },
    {
        "name": "Works in Progress",
        "url": "https://worksinprogress.co/rss.xml",
        "category": "progress_history_science",
        "track_hint": "wildcard",
        "lang": "en",
        "domain": "worksinprogress.co",
    },
    {
        "name": "NBER Working Papers",
        "url": "https://www.nber.org/rss/new.xml",
        "category": "economics_research",
        "track_hint": "discovery",
        "lang": "en",
        "domain": "www.nber.org",
        "allow_abstract_only": True,
    },
]

PRIORITY_DOMAINS: Set[str] = {
    f["domain"].lower().replace("www.", "") for f in PRIORITY_FEEDS
} | {"openalex.org", "jstage.jst.go.jp", "doi.org"}

# Paywall / Login-wall markers (case-insensitive)
PAYWALL_TEXT_PATTERNS = [
    r"subscribe\s+to\s+(?:continue\s+)?read(?:ing)?",
    r"sign\s+in\s+to\s+(?:continue\s+)?read(?:ing)?",
    r"log\s+in\s+to\s+(?:continue\s+)?read(?:ing)?",
    r"already\s+a\s+subscriber",
    r"this\s+article\s+is\s+for\s+subscribers\s+only",
    r"become\s+a\s+member\s+to\s+read",
    r"create\s+a\s+free\s+account\s+to\s+(?:continue|read)",
    r"register\s+to\s+continue\s+reading",
    r"purchase\s+this\s+article",
    r"buy\s+this\s+article",
    r"get\s+full\s+access\s+to\s+this\s+article",
    r"unlock\s+this\s+article",
    r"有料会員限定",
    r"この記事は会員限定です",
    r"会員登録して続きを読む",
    r"ログインして続きを読む",
    r"有料記事です",
    r"定期購読.*続きを読む",
]
PAYWALL_TEXT_RE = re.compile("|".join(PAYWALL_TEXT_PATTERNS), re.IGNORECASE)

PAYWALL_HTML_PATTERNS = [
    r'"isAccessibleForFree"\s*:\s*(?:false|"False"|"false")',
    r'class="[^"]*(?:paywall-overlay|subscriber-only-wall|metered-paywall|article-paywall)[^"]*"',
]
PAYWALL_HTML_RE = re.compile("|".join(PAYWALL_HTML_PATTERNS), re.IGNORECASE)

# Domains known to be hard-paywalled
KNOWN_PAYWALL_DOMAINS = {
    "wsj.com",
    "ft.com",
    "economist.com",
    "nytimes.com",
    "nikkei.com",
    "newspicks.com",
    "hbr.org",
    "bloomberg.com",
    "barrons.com",
    "theinformation.com",
}

MIN_WEB_BODY_CHARS = 800
MIN_ACADEMIC_ABSTRACT_CHARS = 250


def normalize_url(url: str) -> str:
    """Normalize URL for deduplication (strip fragments, tracking params, trailing slash)."""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
        filtered = [
            (k, v)
            for k, v in query_pairs
            if not k.lower().startswith("utm_")
            and k.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source"}
        ]
        clean_query = urllib.parse.urlencode(filtered)
        clean_path = parsed.path.rstrip("/") or "/"
        return urllib.parse.urlunsplit(
            (parsed.scheme.lower(), parsed.netloc.lower(), clean_path, clean_query, "")
        )
    except Exception:
        return url.strip().rstrip("/")


def extract_domain(url: str) -> str:
    """Extract normalized domain without www."""
    try:
        return urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def detect_paywall(html_text: str, body_text: str, url: str = "") -> Optional[str]:
    """
    Detect whether a page is behind a paywall or login wall.
    Returns rejection reason string if paywalled, otherwise None.
    """
    domain = extract_domain(url)
    if domain in KNOWN_PAYWALL_DOMAINS:
        return f"paywall_domain ({domain})"

    # Check schema.org isAccessibleForFree: false or explicit paywall overlay
    if html_text and PAYWALL_HTML_RE.search(html_text):
        # Some sites like MIT SMR have metered paywalls where full text is still absent
        if len(body_text or "") < 2500:
            return "paywall_html_marker"

    # Check body text for explicit paywall / login prompts
    if body_text and PAYWALL_TEXT_RE.search(body_text):
        # If body is short or the paywall notice is prominent, reject
        if len(body_text) < 2800:
            return "paywall_text_detected"

    return None


def verify_and_extract_candidate(candidate: Dict[str, Any], timeout: int = 12) -> Dict[str, Any]:
    """
    Verify a candidate URL by actually fetching and extracting text (or validating official academic abstract).
    Returns updated candidate dict with:
      - verified: bool
      - status: "verified" | "rejected"
      - rejection_reason: Optional[str]
      - content: str
      - content_length: int
      - access_type: "free_full_text" | "open_abstract" | "rejected"
    """
    cand = dict(candidate)
    url = (cand.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        cand["verified"] = False
        cand["status"] = "rejected"
        cand["rejection_reason"] = "invalid_url"
        cand["access_type"] = "rejected"
        return cand

    domain = extract_domain(url)
    cand["domain"] = domain
    if domain in KNOWN_PAYWALL_DOMAINS:
        cand["verified"] = False
        cand["status"] = "rejected"
        cand["rejection_reason"] = f"paywall_domain ({domain})"
        cand["access_type"] = "rejected"
        return cand

    allow_abstract = bool(cand.get("allow_abstract_only", False))
    api_abstract = (cand.get("abstract") or "").strip()

    # For OpenAlex / J-STAGE / NBER where we already have an official API abstract,
    # still verify URL reachability (HEAD/GET) so we never recommend dead/404 links.
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout, allow_redirects=True)
    except requests.exceptions.Timeout:
        cand["verified"] = False
        cand["status"] = "rejected"
        cand["rejection_reason"] = "fetch_timeout"
        cand["access_type"] = "rejected"
        return cand
    except Exception as e:
        cand["verified"] = False
        cand["status"] = "rejected"
        cand["rejection_reason"] = f"fetch_error ({type(e).__name__})"
        cand["access_type"] = "rejected"
        return cand

    if resp.status_code in (401, 402, 403):
        # Some academic sites (e.g. DOI redirects to publisher) return 403 to bots even when OA abstract exists
        if allow_abstract and len(api_abstract) >= MIN_ACADEMIC_ABSTRACT_CHARS:
            cand["verified"] = True
            cand["status"] = "verified"
            cand["rejection_reason"] = None
            cand["content"] = api_abstract
            cand["content_length"] = len(api_abstract)
            cand["access_type"] = "open_abstract"
            return cand
        cand["verified"] = False
        cand["status"] = "rejected"
        cand["rejection_reason"] = f"http_{resp.status_code}_blocked"
        cand["access_type"] = "rejected"
        return cand

    if resp.status_code >= 400:
        cand["verified"] = False
        cand["status"] = "rejected"
        cand["rejection_reason"] = f"http_{resp.status_code}"
        cand["access_type"] = "rejected"
        return cand

    # Check final redirected URL against paywall domains
    final_url = resp.url or url
    final_domain = extract_domain(final_url)
    if final_domain in KNOWN_PAYWALL_DOMAINS:
        cand["verified"] = False
        cand["status"] = "rejected"
        cand["rejection_reason"] = f"paywall_redirect ({final_domain})"
        cand["access_type"] = "rejected"
        return cand

    content_type = (resp.headers.get("content-type") or "").lower()
    if "application/pdf" in content_type:
        # If it's an academic OA PDF and we have a substantial API abstract, accept via abstract
        if allow_abstract and len(api_abstract) >= MIN_ACADEMIC_ABSTRACT_CHARS:
            cand["verified"] = True
            cand["status"] = "verified"
            cand["rejection_reason"] = None
            cand["content"] = api_abstract
            cand["content_length"] = len(api_abstract)
            cand["access_type"] = "open_abstract"
            return cand
        cand["verified"] = False
        cand["status"] = "rejected"
        cand["rejection_reason"] = "direct_pdf_without_abstract"
        cand["access_type"] = "rejected"
        return cand

    resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
    html_text = resp.text

    # Extract main body with trafilatura
    cfg = use_config()
    cfg.set("DEFAULT", "EXTRACTION_COVERAGE", "medium")
    try:
        extracted_text = (
            trafilatura.extract(
                html_text,
                config=cfg,
                include_comments=False,
                include_tables=False,
                include_images=False,
                include_links=False,
                favor_recall=True,
            )
            or ""
        ).strip()
    except Exception:
        extracted_text = ""

    # Fill missing metadata from HTML
    try:
        meta = trafilatura.extract_metadata(html_text)
        if meta:
            if not cand.get("title") and getattr(meta, "title", None):
                cand["title"] = meta.title.strip()
            if not cand.get("author") and getattr(meta, "author", None):
                cand["author"] = meta.author.strip()
            if not cand.get("published_at") and getattr(meta, "date", None):
                cand["published_at"] = str(meta.date).strip()
            if not cand.get("source_name") and getattr(meta, "sitename", None):
                cand["source_name"] = meta.sitename.strip()
    except Exception:
        pass

    # Paywall check
    pw_reason = detect_paywall(html_text, extracted_text, final_url)
    if pw_reason:
        cand["verified"] = False
        cand["status"] = "rejected"
        cand["rejection_reason"] = pw_reason
        cand["access_type"] = "rejected"
        return cand

    # Decide content: extracted full text vs academic abstract
    if len(extracted_text) >= MIN_WEB_BODY_CHARS:
        cand["verified"] = True
        cand["status"] = "verified"
        cand["rejection_reason"] = None
        cand["content"] = extracted_text
        cand["content_length"] = len(extracted_text)
        cand["access_type"] = "free_full_text"
        return cand

    # Academic abstract fallback (NBER / OpenAlex / J-STAGE)
    best_abstract = api_abstract if len(api_abstract) >= len(extracted_text) else extracted_text
    if allow_abstract and len(best_abstract) >= MIN_ACADEMIC_ABSTRACT_CHARS:
        cand["verified"] = True
        cand["status"] = "verified"
        cand["rejection_reason"] = None
        cand["content"] = best_abstract
        cand["content_length"] = len(best_abstract)
        cand["access_type"] = "open_abstract"
        return cand

    cand["verified"] = False
    cand["status"] = "rejected"
    cand["rejection_reason"] = f"content_too_short ({len(extracted_text)} chars)"
    cand["access_type"] = "rejected"
    return cand


def _parse_feed_date(entry: Any) -> str:
    """Format feedparser entry published date as YYYY-MM-DD."""
    for attr in ("published_parsed", "updated_parsed"):
        val = entry.get(attr)
        if val:
            try:
                return f"{val.tm_year:04d}-{val.tm_mon:02d}-{val.tm_mday:02d}"
            except Exception:
                pass
    raw = entry.get("published") or entry.get("updated") or ""
    return str(raw)[:10] if raw else ""


def fetch_priority_rss_candidates(max_per_feed: int = 4) -> List[Dict[str, Any]]:
    """Fetch recent items from priority RSS/Atom feeds in parallel."""
    candidates: List[Dict[str, Any]] = []

    def _fetch_one(feed_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        try:
            resp = requests.get(feed_cfg["url"], headers=REQUEST_HEADERS, timeout=10)
            if resp.status_code >= 400:
                print(f"  [Curator/RSS] {feed_cfg['name']} HTTP {resp.status_code}")
                return items
            parsed = feedparser.parse(resp.content)
            for entry in parsed.entries[:max_per_feed]:
                link = (entry.get("link") or "").strip()
                title = (entry.get("title") or "").strip()
                if not link or not title:
                    continue
                summary = re.sub(r"<[^>]+>", " ", entry.get("summary") or entry.get("description") or "")
                summary = re.sub(r"\s+", " ", summary).strip()
                author = (entry.get("author") or "").strip()
                items.append(
                    {
                        "title": title,
                        "url": link,
                        "source_name": feed_cfg["name"],
                        "author": author,
                        "published_at": _parse_feed_date(entry),
                        "category": feed_cfg["category"],
                        "track_hint": feed_cfg["track_hint"],
                        "lang": feed_cfg["lang"],
                        "abstract": summary,
                        "allow_abstract_only": bool(feed_cfg.get("allow_abstract_only", False)),
                        "discovery_channel": "priority_rss",
                    }
                )
        except Exception as e:
            print(f"  [Curator/RSS] Error fetching {feed_cfg['name']}: {e}")
        return items

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(_fetch_one, cfg) for cfg in PRIORITY_FEEDS]
        for fut in as_completed(futures):
            candidates.extend(fut.result())

    print(f"[Curator/Sources] Priority RSS fetched {len(candidates)} raw candidates")
    return candidates


def _reconstruct_openalex_abstract(inv_index: Optional[Dict[str, List[int]]]) -> str:
    """Reconstruct plain-text abstract from OpenAlex abstract_inverted_index."""
    if not inv_index or not isinstance(inv_index, dict):
        return ""
    pos_map: Dict[int, str] = {}
    for word, positions in inv_index.items():
        for pos in positions:
            pos_map[pos] = word
    if not pos_map:
        return ""
    return " ".join(pos_map[i] for i in sorted(pos_map.keys()))


def fetch_openalex_candidates(
    queries: List[Tuple[str, str]],
    per_query: int = 3,
) -> List[Dict[str, Any]]:
    """
    Search OpenAlex free academic API for open-access works.
    queries: list of (query_string, track_hint)
    """
    candidates: List[Dict[str, Any]] = []
    for query, track_hint in queries:
        try:
            params = {
                "search": query,
                "filter": "open_access.is_oa:true,has_abstract:true,from_publication_date:2022-01-01",
                "sort": "cited_by_count:desc",
                "per-page": per_query,
                "mailto": "readout-curator@local.dev",
            }
            resp = requests.get(
                "https://api.openalex.org/works",
                params=params,
                headers={"User-Agent": "ReadoutCurator/1.0 (mailto:readout-curator@local.dev)"},
                timeout=12,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            for work in data.get("results", []):
                title = (work.get("title") or "").strip()
                if not title:
                    continue
                oa_url = (work.get("open_access") or {}).get("oa_url")
                primary_loc = work.get("primary_location") or {}
                landing_url = primary_loc.get("landing_page_url")
                doi = work.get("doi")
                url = landing_url or oa_url or doi
                if not url:
                    continue
                abstract = _reconstruct_openalex_abstract(work.get("abstract_inverted_index"))
                if len(abstract) < MIN_ACADEMIC_ABSTRACT_CHARS:
                    continue
                authors = [
                    (a.get("author") or {}).get("display_name", "")
                    for a in (work.get("authorships") or [])[:3]
                ]
                authors_str = ", ".join(a for a in authors if a)
                source_title = ((primary_loc.get("source") or {}).get("display_name") or "OpenAlex OA")
                pub_date = work.get("publication_date") or str(work.get("publication_year") or "")
                candidates.append(
                    {
                        "title": title,
                        "url": url,
                        "source_name": f"{source_title} (OpenAlex)",
                        "author": authors_str,
                        "published_at": pub_date,
                        "category": "academic_openalex",
                        "track_hint": track_hint,
                        "lang": "en",
                        "abstract": abstract,
                        "allow_abstract_only": True,
                        "discovery_channel": "openalex_api",
                    }
                )
        except Exception as e:
            print(f"  [Curator/OpenAlex] Query '{query}' failed: {e}")
    print(f"[Curator/Sources] OpenAlex fetched {len(candidates)} raw candidates")
    return candidates


def fetch_jstage_candidates(
    queries: List[Tuple[str, str]],
    per_query: int = 3,
) -> List[Dict[str, Any]]:
    """
    Search J-STAGE free public API for Japanese academic papers.
    queries: list of (japanese_query, track_hint)
    """
    candidates: List[Dict[str, Any]] = []
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "prism": "http://prismstandard.org/namespaces/basic/2.0/",
    }
    for query, track_hint in queries:
        try:
            params = {
                "service": "3",
                "text": query,
                "count": str(per_query),
            }
            resp = requests.get(
                "https://api.jstage.jst.go.jp/searchapi/do",
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=12,
            )
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.content)
            for entry in root.findall("atom:entry", ns):
                title_el = entry.find("atom:title", ns)
                link_el = entry.find("atom:link", ns)
                title = (title_el.text or "").strip() if title_el is not None else ""
                url = (link_el.attrib.get("href", "") or "").strip() if link_el is not None else ""
                if not title or not url:
                    continue
                author_els = entry.findall("atom:author/atom:name", ns)
                authors = ", ".join((a.text or "").strip() for a in author_els[:3] if a.text)
                pub_el = entry.find("prism:publicationName", ns)
                pub_name = (pub_el.text or "").strip() if pub_el is not None else "J-STAGE"
                year_el = entry.find("prism:publicationDate", ns)
                pub_date = (year_el.text or "").strip() if year_el is not None else ""
                summary_el = entry.find("atom:summary", ns)
                summary = (summary_el.text or "").strip() if summary_el is not None else ""
                candidates.append(
                    {
                        "title": title,
                        "url": url,
                        "source_name": f"{pub_name} (J-STAGE)",
                        "author": authors,
                        "published_at": pub_date,
                        "category": "academic_jstage",
                        "track_hint": track_hint,
                        "lang": "ja",
                        "abstract": summary,
                        "allow_abstract_only": True,
                        "discovery_channel": "jstage_api",
                    }
                )
        except Exception as e:
            print(f"  [Curator/J-STAGE] Query '{query}' failed: {e}")
    print(f"[Curator/Sources] J-STAGE fetched {len(candidates)} raw candidates")
    return candidates


def fetch_gemini_grounding_candidates(
    track_prompts: List[Tuple[str, str]],
) -> List[Dict[str, Any]]:
    """
    Use Gemini Google Search Grounding (if available in SDK & API key) to discover
    free-access essays/articles from unknown/adjacent sources (English & Japanese).
    Gracefully returns [] if unavailable.
    """
    if not poc_pipeline.GEMINI_API_KEY:
        return []

    candidates: List[Dict[str, Any]] = []
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=poc_pipeline.GEMINI_API_KEY)
        search_tool = types.Tool(google_search=types.GoogleSearch())
    except Exception as e:
        print(f"  [Curator/Grounding] Google Search Grounding unavailable: {e}")
        return []

    for prompt_text, track_hint in track_prompts:
        try:
            resp = client.models.generate_content(
                model=poc_pipeline.DEFAULT_MODEL,
                contents=prompt_text,
                config=types.GenerateContentConfig(
                    tools=[search_tool],
                    temperature=0.4,
                ),
            )
            # Extract real grounded URLs from grounding_metadata (never trust hallucinated URLs)
            g_chunks = []
            if resp.candidates:
                gmeta = getattr(resp.candidates[0], "grounding_metadata", None)
                if gmeta and getattr(gmeta, "grounding_chunks", None):
                    g_chunks = gmeta.grounding_chunks

            for ch in g_chunks[:4]:
                web = getattr(ch, "web", None)
                if not web:
                    continue
                uri = getattr(web, "uri", None) or ""
                title = getattr(web, "title", None) or ""
                if not uri.startswith(("http://", "https://")):
                    continue
                # Note: grounding redirect URLs will be resolved during verify_and_extract_candidate
                candidates.append(
                    {
                        "title": title.strip() or "Web Article",
                        "url": uri.strip(),
                        "source_name": title.strip() or "Web Discovery",
                        "author": "",
                        "published_at": "",
                        "category": "web_grounding",
                        "track_hint": track_hint,
                        "lang": "ja" if any("\u3040" <= c <= "\u9fff" for c in title) else "en",
                        "abstract": "",
                        "allow_abstract_only": False,
                        "discovery_channel": "gemini_grounding",
                    }
                )
        except Exception as e:
            print(f"  [Curator/Grounding] Search failed for {track_hint}: {e}")

    print(f"[Curator/Sources] Gemini Grounding fetched {len(candidates)} raw candidates")
    return candidates


def collect_and_verify_candidates(
    seen_urls: Set[str],
    seen_titles: Set[str],
    core_preference_summary: str = "",
    enable_grounding: bool = True,
    max_verify_workers: int = 8,
) -> Dict[str, Any]:
    """
    Run full multi-source collection and strict Python-side verification.
    Returns:
      {
        "verified": [ ... ],
        "rejected": [ ... ],
        "new_sources_discovered": [ ... ],
        "stats": { ... },
      }
    """
    raw_candidates: List[Dict[str, Any]] = []

    # 1. Priority RSS feeds
    raw_candidates.extend(fetch_priority_rss_candidates(max_per_feed=4))

    # 2. OpenAlex free academic API (Core + Discovery + Wild Card)
    openalex_queries = [
        ("organizational strategy decision making innovation", "core"),
        ("consumer behavior marketing research field experiment", "core"),
        ("collective intelligence complex systems adaptation", "discovery"),
        ("history of science urban evolution ecology", "wildcard"),
    ]
    raw_candidates.extend(fetch_openalex_candidates(openalex_queries, per_query=3))

    # 3. J-STAGE Japanese free academic API (Core + Discovery + Wild Card)
    jstage_queries = [
        ("組織 意思決定 経営戦略", "core"),
        ("マーケティング 消費者行動 リサーチ", "core"),
        ("行動経済学 社会心理学 意思決定", "discovery"),
        ("科学史 認知科学 複雑系", "wildcard"),
    ]
    raw_candidates.extend(fetch_jstage_candidates(jstage_queries, per_query=2))

    # 4. Optional Gemini Google Search Grounding (Unknown sources, EN + JA)
    # Note: Wild Card grounding prompt deliberately omits core_preference_summary!
    if enable_grounding:
        core_hint = (
            f"特に以下の関心軸に関連する無料公開の長文解説・調査記事を探してください: {core_preference_summary[:250]}"
            if core_preference_summary
            else "経営戦略・組織論・マーケティングリサーチ・意思決定に関する無料公開の長文解説記事を探してください。"
        )
        grounding_prompts = [
            (
                f"{core_hint}\n"
                "有料会員限定やペイウォール記事は除外し、誰でも全文読める日本語または英語の良質な記事を3件見つけて要約してください。",
                "core",
            ),
            (
                "科学史、生態学、都市論、認知科学、技術史、芸術・文化など、ビジネス以外の分野で知的好奇心を強く刺激する"
                "無料公開の長文エッセイや解説記事（日本語または英語）を3件見つけて要約してください。",
                "wildcard",
            ),
        ]
        raw_candidates.extend(fetch_gemini_grounding_candidates(grounding_prompts))

    # Deduplicate by normalized URL and exclude already-seen URLs/titles
    deduped: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    seen_in_batch: Set[str] = set()
    norm_seen_urls = {normalize_url(u) for u in seen_urls if u}
    norm_seen_titles = {t.strip().lower() for t in seen_titles if t}

    for cand in raw_candidates:
        n_url = normalize_url(cand.get("url", ""))
        n_title = (cand.get("title") or "").strip().lower()
        if not n_url or n_url in seen_in_batch:
            continue
        seen_in_batch.add(n_url)

        if n_url in norm_seen_urls or (n_title and n_title in norm_seen_titles):
            r = dict(cand)
            r["verified"] = False
            r["status"] = "rejected"
            r["rejection_reason"] = "already_seen"
            r["access_type"] = "rejected"
            rejected.append(r)
            continue

        deduped.append(cand)

    # Cap verification batch to keep runtime crisp (~45 candidates max, balanced across tracks)
    if len(deduped) > 48:
        by_track: Dict[str, List[Dict[str, Any]]] = {"core": [], "discovery": [], "wildcard": []}
        for c in deduped:
            t = c.get("track_hint", "discovery")
            by_track.setdefault(t, []).append(c)
        balanced: List[Dict[str, Any]] = []
        for t in ("core", "discovery", "wildcard"):
            balanced.extend(by_track.get(t, [])[:16])
        deduped = balanced

    verified: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_verify_workers) as ex:
        futures = [ex.submit(verify_and_extract_candidate, c) for c in deduped]
        for fut in as_completed(futures):
            res = fut.result()
            if res.get("verified"):
                verified.append(res)
            else:
                rejected.append(res)

    # Identify newly discovered sources outside priority list
    new_sources: Set[str] = set()
    for c in verified:
        dom = (c.get("domain") or extract_domain(c.get("url", ""))).lower()
        if dom and dom not in PRIORITY_DOMAINS:
            new_sources.add(f"{c.get('source_name', dom)} ({dom})")

    stats = {
        "raw_collected": len(raw_candidates),
        "deduped_checked": len(deduped),
        "verified_count": len(verified),
        "rejected_count": len(rejected),
        "shortfall_warning": (
            f"検証通過候補が {len(verified)} 件（目標15件未満）でした。"
            if len(verified) < 15
            else None
        ),
    }
    print(
        f"[Curator/Sources] Verification complete: "
        f"{len(verified)} verified, {len(rejected)} rejected (raw={len(raw_candidates)})"
    )
    return {
        "verified": verified,
        "rejected": rejected,
        "new_sources_discovered": sorted(new_sources),
        "stats": stats,
    }
