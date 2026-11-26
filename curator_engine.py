#!/usr/bin/env python3
"""
Readout - Personal Knowledge Curator Engine
Implements:
  - 3 independent discovery tracks (Core, Discovery, Wild Card — Wild Card structurally isolated from user preference profile)
  - 7-axis weighted evaluation + slot-based selection (not naive top-N)
  - 4-part, 7-step ear-friendly Japanese audio script generation (variable length, quality-first)
  - Incremental VOICEPEAK pre-synthesis (each part published to daily_YYYY-MM-DD.json as soon as its MP3 is ready)
  - 14-day auto-cleanup of unadopted pre-synthesized MP3s (JSON, scripts, logs, history preserved permanently)
  - 5-level feedback learning loop updating Core-only preference summary
  - Zero-wait instant enqueue into Readout's main playback queue
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from mutagen.mp3 import MP3

import curator_sources
import poc_pipeline
import queue_manager

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CURATOR_DIR = DATA_DIR / "curator"
CURATOR_AUDIO_DIR = CURATOR_DIR / "audio"
HISTORY_FILE = CURATOR_DIR / "history.json"
OUTPUT_DIR = BASE_DIR / "output"

JST = timezone(timedelta(hours=9))

_FILE_LOCK = threading.Lock()
_STATUS_LOCK = threading.Lock()

# Exact 7-axis weights (sum = 1.00)
SEVEN_AXIS_WEIGHTS: Dict[str, float] = {
    "question_depth": 0.20,     # 問いの深さ
    "explanatory_power": 0.20,  # 説明力
    "evidence_quality": 0.20,   # 根拠の質
    "originality": 0.15,        # 独自性
    "long_term_value": 0.10,    # 長期的価値
    "curiosity": 0.10,          # 好奇心
    "audio_suitability": 0.05,  # 音声適性
}

SEVEN_AXIS_LABELS: Dict[str, str] = {
    "question_depth": "問いの深さ",
    "explanatory_power": "説明力",
    "evidence_quality": "根拠の質",
    "originality": "独自性",
    "long_term_value": "長期的価値",
    "curiosity": "好奇心",
    "audio_suitability": "音声適性",
}

RATING_LABELS: Dict[int, str] = {
    5: "非常に面白かった",
    4: "面白かった",
    3: "普通",
    2: "あまり面白くなかった",
    1: "既に知っていた",
}
LABEL_TO_RATING: Dict[str, int] = {v: k for k, v in RATING_LABELS.items()}

DEFAULT_CORE_PREFERENCE = (
    "経営戦略・組織論・意思決定・マーケティングリサーチ・消費者行動・"
    "行動科学・データに基づく実務的かつ構造的な洞察を重視。表面的なトレンド解説より、"
    "一次データ・実証研究・歴史的背景から本質を掘り下げる解説を好む。"
)

# Live runtime status for API polling
_CURATOR_STATUS: Dict[str, Any] = {
    "running": False,
    "date": None,
    "phase": "idle",
    "message": "待機中",
    "progress": 0,
    "parts_ready": 0,
    "parts_total": 4,
    "started_at": None,
    "finished_at": None,
    "error": None,
}


def ensure_dirs() -> None:
    CURATOR_DIR.mkdir(parents=True, exist_ok=True)
    CURATOR_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    queue_manager.ensure_dirs()
    if not HISTORY_FILE.exists():
        _atomic_write_json(
            HISTORY_FILE,
            {
                "seen_urls": [],
                "seen_titles": [],
                "feedback": [],
                "core_preference_summary": DEFAULT_CORE_PREFERENCE,
                "updated_at": datetime.now(JST).isoformat(),
            },
        )


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _FILE_LOCK:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        shutil.move(str(tmp), str(path))


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with _FILE_LOCK:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default


def today_jst_str() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


def get_status() -> Dict[str, Any]:
    with _STATUS_LOCK:
        return dict(_CURATOR_STATUS)


def _update_status(**kwargs: Any) -> None:
    with _STATUS_LOCK:
        _CURATOR_STATUS.update(kwargs)


def load_history() -> Dict[str, Any]:
    ensure_dirs()
    data = _read_json(HISTORY_FILE, {}) or {}
    data.setdefault("seen_urls", [])
    data.setdefault("seen_titles", [])
    data.setdefault("feedback", [])
    data.setdefault("core_preference_summary", DEFAULT_CORE_PREFERENCE)
    return data


def save_history(history: Dict[str, Any]) -> None:
    history["updated_at"] = datetime.now(JST).isoformat()
    _atomic_write_json(HISTORY_FILE, history)


def list_curated_dates() -> List[str]:
    ensure_dirs()
    dates: List[str] = []
    for p in CURATOR_DIR.glob("daily_*.json"):
        m = re.match(r"daily_(\d{4}-\d{2}-\d{2})\.json$", p.name)
        if m:
            dates.append(m.group(1))
    dates.sort(reverse=True)
    return dates


def load_daily(date_str: Optional[str] = None) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Load daily selection and research log for a given date (or latest available)."""
    ensure_dirs()
    target_date = date_str
    if not target_date:
        dates = list_curated_dates()
        target_date = dates[0] if dates else None
    if not target_date:
        return None, None, None
    daily = _read_json(CURATOR_DIR / f"daily_{target_date}.json")
    rlog = _read_json(CURATOR_DIR / f"research_log_{target_date}.json")
    return target_date, daily, rlog


def compute_weighted_score(scores: Dict[str, Any]) -> float:
    """
    Deterministically compute 7-axis weighted score (0.00 - 5.00).
    Missing axes default to 0.0; values are clamped to [0.0, 5.0].
    """
    total = 0.0
    for axis, weight in SEVEN_AXIS_WEIGHTS.items():
        raw = scores.get(axis, 0.0)
        try:
            val = float(raw)
        except (TypeError, ValueError):
            val = 0.0
        val = max(0.0, min(5.0, val))
        total += val * weight
    return round(total, 2)


def sanitize_curator_script(text: str) -> str:
    """
    Strict sanitization for TTS narration:
    - Strip URLs
    - Strip Markdown headers, bullets, emphasis, code fences, table pipes
    - Strip common radio greetings if accidentally emitted at start
    """
    cleaned = text or ""
    # Remove URLs
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    # Remove markdown links [label](url) -> label
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    # Remove markdown heading / list prefixes at line start
    cleaned = re.sub(r"(?m)^\s*(?:#{1,6}\s*|[-*・•]\s+|\d+\.\s+)", "", cleaned)
    # Remove table pipes and symbols
    cleaned = re.sub(r"[|*_#`~<>]", "", cleaned)
    # Remove radio greetings on the very first line if any
    cleaned = re.sub(
        r"^(?:こんにちは|おはようございます|こんばんは|リスナーの皆さん|今日も良い一日を)[。！!、,\s]*",
        "",
        cleaned.strip(),
    )
    cleaned = poc_pipeline.sanitize_script_for_tts(cleaned)
    return cleaned.strip()


def _extract_json_block(text: str) -> Any:
    """Extract JSON object/array from LLM response (handles ```json fences)."""
    raw = (text or "").strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if m:
        raw = m.group(1).strip()
    # Try direct parse
    try:
        return json.loads(raw)
    except Exception:
        pass
    # Try locating outermost { ... } or [ ... ]
    for start_ch, end_ch in (("{", "}"), ("[", "]")):
        s = raw.find(start_ch)
        e = raw.rfind(end_ch)
        if s != -1 and e != -1 and e > s:
            try:
                return json.loads(raw[s : e + 1])
            except Exception:
                pass
    raise ValueError(f"Could not parse JSON from LLM output: {raw[:200]}")


def _call_gemini_text(prompt: str, temperature: float = 0.5, max_output_tokens: int = 8192) -> str:
    """Call Gemini with Readout's model fallback list."""
    if not poc_pipeline.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set.")
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=poc_pipeline.GEMINI_API_KEY)
    models = [poc_pipeline.DEFAULT_MODEL] + [
        m for m in poc_pipeline.FALLBACK_MODELS if m != poc_pipeline.DEFAULT_MODEL
    ]
    last_err: Optional[Exception] = None
    for model in models:
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                ),
            )
            if resp and resp.text:
                return resp.text.strip()
        except Exception as e:
            print(f"  [Curator/Gemini] Model {model} failed: {e}")
            last_err = e
    raise RuntimeError(f"All Gemini models failed: {last_err}")


def _format_candidates_for_prompt(candidates: List[Dict[str, Any]]) -> str:
    blocks = []
    for idx, c in enumerate(candidates):
        snippet = (c.get("content") or "")[:900].replace("\n", " ")
        blocks.append(
            f"[{idx}] Title: {c.get('title')}\n"
            f"Source: {c.get('source_name')} | Author: {c.get('author') or '不明'} | "
            f"Date: {c.get('published_at') or '不明'} | Access: {c.get('access_type')}\n"
            f"URL: {c.get('url')}\n"
            f"Excerpt: {snippet}"
        )
    return "\n\n".join(blocks)


def build_core_evaluation_prompt(
    candidates: List[Dict[str, Any]],
    core_preference_summary: str,
) -> str:
    """Track A (Core): Uses user's Core preference summary."""
    cand_text = _format_candidates_for_prompt(candidates)
    return f"""あなたは優秀な知的キュレーター（Track A: Core Interest Discovery）です。
ユーザーの関心プロファイルを踏まえ、以下の実在・検証済み候補記事それぞれを7軸（各0〜5点、0.5点刻み可）と関心適合度・新規性で厳正に評価してください。

【ユーザーのCore関心プロファイル】
{core_preference_summary}

【評価軸（各0〜5点）】
- question_depth (問いの深さ)
- explanatory_power (説明力)
- evidence_quality (根拠の質)
- originality (独自性)
- long_term_value (長期的価値)
- curiosity (好奇心)
- audio_suitability (音声適性)
- interest_fit (Core関心適合度 0〜5)
- novelty (新規性 0〜5)

【厳守事項】
- 候補に書かれていない事実や研究結果を絶対に創作しないこと。
- 必ず以下の JSON 配列のみを出力すること（解説文不要）。

出力フォーマット:
[
  {{
    "index": 0,
    "scores": {{
      "question_depth": 4.5,
      "explanatory_power": 4.0,
      "evidence_quality": 4.5,
      "originality": 4.0,
      "long_term_value": 4.5,
      "curiosity": 4.0,
      "audio_suitability": 4.0
    }},
    "interest_fit": 4.5,
    "novelty": 4.0,
    "recommended_slot": "core",
    "reason_ja": "日本語で簡潔な評価理由（60〜100字）"
  }}
]

【候補一覧】
{cand_text}
"""


def build_discovery_evaluation_prompt(
    candidates: List[Dict[str, Any]],
    core_preference_summary: str,
) -> str:
    """Track B (Adjacent Discovery): Connects adjacent disciplines to broader insights."""
    cand_text = _format_candidates_for_prompt(candidates)
    return f"""あなたは優秀な知的キュレーター（Track B: Adjacent Discovery）です。
ユーザーの主領域に隣接する分野（行動科学、経済学、社会システム、科学技術の応用など）から、新しい視座をもたらす候補を7軸（各0〜5点）＋関心適合度＋新規性で評価してください。

【参考（主領域の方向性）】
{core_preference_summary}

【評価軸（各0〜5点）】
question_depth, explanatory_power, evidence_quality, originality, long_term_value, curiosity, audio_suitability, interest_fit, novelty

【厳守事項】
- 候補本文にない事実を創作しないこと。
- 必ず JSON 配列のみ出力すること。

出力フォーマット:
[
  {{
    "index": 0,
    "scores": {{
      "question_depth": 4.0,
      "explanatory_power": 4.0,
      "evidence_quality": 4.0,
      "originality": 4.5,
      "long_term_value": 4.0,
      "curiosity": 4.5,
      "audio_suitability": 4.0
    }},
    "interest_fit": 3.5,
    "novelty": 4.5,
    "recommended_slot": "discovery",
    "reason_ja": "日本語で簡潔な評価理由（60〜100字）"
  }}
]

【候補一覧】
{cand_text}
"""


def build_wildcard_evaluation_prompt(candidates: List[Dict[str, Any]]) -> str:
    """
    Track C (Serendipity / Wild Card):
    STRUCTURALLY ANTI-OVERFITTING: Does NOT accept or reference user preference profile!
    Only evaluates pure intellectual surprise, originality, curiosity, and depth.
    """
    cand_text = _format_candidates_for_prompt(candidates)
    return f"""あなたは独立した知的キュレーター（Track C: Serendipity / Wild Card）です。
特定の読者の過去の好みや専門領域は一切考慮せず、純粋に「知的好奇心を強く揺さぶるか」「意外性・独自性・問いの面白さがあるか」「根拠と説明力があるか」だけで以下の実在・検証済み候補を7軸（各0〜5点）と新規性で評価してください。

【評価軸（各0〜5点）】
question_depth, explanatory_power, evidence_quality, originality, long_term_value, curiosity, audio_suitability, novelty
（interest_fit は 0.0 固定で構いません）

【厳守事項】
- 候補本文にない事実を創作しないこと。
- 必ず JSON 配列のみ出力すること。

出力フォーマット:
[
  {{
    "index": 0,
    "scores": {{
      "question_depth": 4.5,
      "explanatory_power": 4.0,
      "evidence_quality": 4.0,
      "originality": 5.0,
      "long_term_value": 4.5,
      "curiosity": 5.0,
      "audio_suitability": 4.5
    }},
    "interest_fit": 0.0,
    "novelty": 5.0,
    "recommended_slot": "wildcard",
    "reason_ja": "日本語で簡潔な評価理由（60〜100字）"
  }}
]

【候補一覧】
{cand_text}
"""


def _evaluate_track(
    candidates: List[Dict[str, Any]],
    track: str,
    core_preference_summary: str = "",
) -> List[Dict[str, Any]]:
    """Run LLM evaluation for a single track and compute deterministic weighted scores."""
    if not candidates:
        return []

    if track == "core":
        prompt = build_core_evaluation_prompt(candidates, core_preference_summary)
    elif track == "discovery":
        prompt = build_discovery_evaluation_prompt(candidates, core_preference_summary)
    elif track == "wildcard":
        # Explicitly never pass core_preference_summary!
        prompt = build_wildcard_evaluation_prompt(candidates)
    else:
        raise ValueError(f"Unknown track: {track}")

    eval_map: Dict[int, Dict[str, Any]] = {}
    try:
        raw = _call_gemini_text(prompt, temperature=0.3, max_output_tokens=4096)
        parsed = _extract_json_block(raw)
        if isinstance(parsed, list):
            for row in parsed:
                if isinstance(row, dict) and "index" in row:
                    eval_map[int(row["index"])] = row
    except Exception as e:
        print(f"  [Curator/Eval] Track '{track}' LLM evaluation failed ({e}); applying neutral fallback scores.")

    results: List[Dict[str, Any]] = []
    for idx, c in enumerate(candidates):
        ev = eval_map.get(idx, {})
        raw_scores = ev.get("scores") if isinstance(ev.get("scores"), dict) else {}
        clean_scores = {}
        for axis in SEVEN_AXIS_WEIGHTS:
            try:
                v = float(raw_scores.get(axis, 3.0))
            except (TypeError, ValueError):
                v = 3.0
            clean_scores[axis] = round(max(0.0, min(5.0, v)), 2)

        weighted = compute_weighted_score(clean_scores)
        try:
            interest_fit = round(max(0.0, min(5.0, float(ev.get("interest_fit", 3.0 if track != "wildcard" else 0.0)))), 2)
        except (TypeError, ValueError):
            interest_fit = 0.0
        try:
            novelty = round(max(0.0, min(5.0, float(ev.get("novelty", 3.0)))), 2)
        except (TypeError, ValueError):
            novelty = 3.0

        item = dict(c)
        item["eval_track"] = track
        item["scores"] = clean_scores
        item["weighted_score"] = weighted
        item["interest_fit"] = interest_fit
        item["novelty"] = novelty
        item["eval_reason"] = (ev.get("reason_ja") or "検証済み無料公開記事として評価。").strip()
        results.append(item)

    return results


def select_slots(
    core_evaluated: List[Dict[str, Any]],
    discovery_evaluated: List[Dict[str, Any]],
    wildcard_evaluated: List[Dict[str, Any]],
) -> Dict[str, Optional[Dict[str, Any]]]:
    """
    Slot-based selection (NOT naive top-N by total weighted score):
      - Core slot: balances weighted_score (65%) + interest_fit (35%)
      - Discovery slot: balances weighted_score (65%) + novelty (35%), avoiding Core URL & preferring distinct domain
      - Wild Card slot: purely from wildcard_evaluated (preference-blind), balancing weighted_score (45%) + curiosity (30%) + originality (25%)
    """
    used_urls: Set[str] = set()
    used_domains: Set[str] = set()

    def _pick_best(pool: List[Dict[str, Any]], key_fn, prefer_new_domain: bool = True) -> Optional[Dict[str, Any]]:
        available = [
            c for c in pool if curator_sources.normalize_url(c.get("url", "")) not in used_urls
        ]
        if not available:
            return None
        if prefer_new_domain:
            diff_dom = [c for c in available if (c.get("domain") or "") not in used_domains]
            if diff_dom:
                available = diff_dom
        best = max(available, key=key_fn)
        used_urls.add(curator_sources.normalize_url(best.get("url", "")))
        if best.get("domain"):
            used_domains.add(best["domain"])
        return best

    # 1. Core
    core_pool = core_evaluated or discovery_evaluated or wildcard_evaluated
    part1_core = _pick_best(
        core_pool,
        key_fn=lambda c: 0.65 * c.get("weighted_score", 0.0) + 0.35 * c.get("interest_fit", 0.0),
        prefer_new_domain=False,
    )

    # 2. Discovery
    disc_pool = discovery_evaluated or core_evaluated or wildcard_evaluated
    part2_discovery = _pick_best(
        disc_pool,
        key_fn=lambda c: 0.65 * c.get("weighted_score", 0.0) + 0.35 * c.get("novelty", 0.0),
        prefer_new_domain=True,
    )

    # 3. Wild Card (strictly from wildcard_evaluated first)
    wc_pool = wildcard_evaluated or discovery_evaluated or core_evaluated
    part3_wildcard = _pick_best(
        wc_pool,
        key_fn=lambda c: (
            0.45 * c.get("weighted_score", 0.0)
            + 0.30 * c.get("scores", {}).get("curiosity", 0.0)
            + 0.25 * c.get("scores", {}).get("originality", 0.0)
        ),
        prefer_new_domain=True,
    )

    return {
        "core": part1_core,
        "discovery": part2_discovery,
        "wildcard": part3_wildcard,
    }


def _generate_part_script(
    part_type: str,
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Generate 7-step ear-friendly Japanese audio narration script for Core / Discovery / Wild Card.
    Returns dict with title_ja, why_selected, script_text, estimated_minutes.
    """
    if part_type in ("core", "discovery"):
        target_chars = "3,500〜5,000字（音声で約10〜15分）"
        est_min = 12
        role_label = "Core Deep Dive（主領域の深掘り解説）" if part_type == "core" else "Discovery Deep Dive（隣接領域からの新視点）"
    else:
        target_chars = "2,100〜3,000字（音声で約6〜9分）"
        est_min = 8
        role_label = "Wild Card（知的好奇心を刺激する意外な発見）"

    body_excerpt = (candidate.get("content") or "")[:16000]
    access_note = (
        "※これは公開アブストラクト（研究概要）に基づく解説です。アブストラクトに記載のない数値や詳細は推測せず、『公開概要の範囲では』と明示し、背景知識や分析部分はAIの解説として明確に区別してください。"
        if candidate.get("access_type") == "open_abstract"
        else "※記事の主張・データと、AIによる構造的分析・示唆を言葉の上で明確に区別してください。"
    )

    prompt = f"""あなたは知的で明晰な音声解説ライターです。
以下の実在記事（{role_label}）をもとに、歩行中や移動中に耳で聴いて深く理解できる日本語ナレーション原稿を執筆してください。

【分量目安（質優先・可変尺）】
{target_chars}

【必須の7ステップ構成（見出し記号は使わず自然な語り口で順番に展開すること）】
1. なぜこの問いが面白いか（導入・知的フック）
2. 通説・歴史（従来の見方や背景）
3. 重要主張（本記事・研究の核心的なテーゼ）
4. 具体例・データ・証拠（本文に基づく具体的な根拠）
5. 反論・限界（前提の制約、異論、適用限界）
6. 示唆（意思決定・視点・実務や思考への応用）
7. さらに考えたい問い（聴き終えた後に残る深い問い）

【厳守ルール】
- ラジオ風の挨拶（「こんにちは」「今日も良い一日を」「〇〇の時間です」等）は一切入れないこと。
- 表、図、URL、Markdown記号（#、*、-、・、`、~、|）、箇条書き記号は一切出力しないこと。句読点と自然な改行のみのプレーンテキストにすること。
- 著作権に配慮し、原文の直訳や丸写しではなく、構造を噛み砕いた独自の日本語解説にすること。
- 元記事にない数値・固有名詞・研究結果を創作しないこと。
- {access_note}
- 出力は必ず以下の JSON オブジェクトのみとすること。

出力フォーマット:
{{
  "title_ja": "日本語の知的で簡潔なタイトル（30〜50字）",
  "why_selected": "この記事を選んだ理由と聴きどころ（80〜140字）",
  "script_text": "7ステップ構成のプレーンテキスト音声原稿..."
}}

【記事メタデータ】
原題: {candidate.get('title')}
媒体: {candidate.get('source_name')}
著者: {candidate.get('author') or '記載なし'}
公開日: {candidate.get('published_at') or '記載なし'}
アクセス形態: {candidate.get('access_type')}

【記事本文 / 公開概要】
{body_excerpt}
"""

    raw = _call_gemini_text(prompt, temperature=0.6, max_output_tokens=8192)
    try:
        parsed = _extract_json_block(raw)
        title_ja = (parsed.get("title_ja") or candidate.get("title") or "無題").strip()
        why_selected = (parsed.get("why_selected") or candidate.get("eval_reason") or "").strip()
        script_text = sanitize_curator_script(parsed.get("script_text") or "")
    except Exception:
        title_ja = candidate.get("title") or "無題"
        why_selected = candidate.get("eval_reason") or ""
        script_text = sanitize_curator_script(raw)

    est_minutes = max(2, round(len(script_text) / 330)) if script_text else est_min
    return {
        "title_ja": title_ja,
        "why_selected": why_selected,
        "script_text": script_text,
        "estimated_minutes": est_minutes,
    }


def _generate_connections_script(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Generate Part 4: Connections (2-3 min, ~700-1,100 chars) + next research questions."""
    summary_lines = []
    for p in parts:
        summary_lines.append(
            f"- [{p['part_type'].upper()}] {p['title']} (出典: {p['source']['source_name']})\n"
            f"  選定理由: {p['why_selected']}\n"
            f"  原稿抜粋: {(p.get('script_text') or '')[:600]}"
        )
    joined = "\n\n".join(summary_lines)

    prompt = f"""あなたは知的キュレーターです。
本日選んだ以下の3つのテーマ（Core / Discovery / Wild Card）を横断し、Part 4「Connections」の日本語音声原稿（700〜1,100字、音声で約2〜3分）と、次回調査したい問いを3つ作成してください。

【Part 4 で語るべきこと】
- 3つのテーマの間に潜む構造的な繋がり・共通パターン
- 逆に、テーマ同士が示す緊張関係・矛盾・視点のズレ
- それらを踏まえて次に考えたくなる大きな問い

【厳守ルール】
- ラジオ風の挨拶や締めの掛け声は一切入れないこと。
- Markdown記号、URL、箇条書き記号は原稿内に一切入れないこと。
- 出力は必ず JSON オブジェクトのみとすること。

出力フォーマット:
{{
  "title_ja": "今日の3テーマを結ぶ構造的視点（簡潔なタイトル）",
  "why_selected": "3テーマ間の繋がりと緊張関係の要点（80〜120字）",
  "script_text": "700〜1,100字のプレーンテキスト音声原稿...",
  "next_research_questions": [
    "次回調査したい問い1",
    "次回調査したい問い2",
    "次回調査したい問い3"
  ]
}}

【本日の3テーマ】
{joined}
"""
    raw = _call_gemini_text(prompt, temperature=0.65, max_output_tokens=4096)
    try:
        parsed = _extract_json_block(raw)
        title_ja = (parsed.get("title_ja") or "今日の3テーマを結ぶ視点").strip()
        why_selected = (parsed.get("why_selected") or "3つの独立したテーマの共通構造と矛盾を整理しました。").strip()
        script_text = sanitize_curator_script(parsed.get("script_text") or "")
        next_qs = parsed.get("next_research_questions") or []
        if not isinstance(next_qs, list):
            next_qs = []
    except Exception:
        title_ja = "今日の3テーマを結ぶ視点"
        why_selected = "3つのテーマの繋がりと今後の問いを整理しました。"
        script_text = sanitize_curator_script(raw)
        next_qs = []

    est_minutes = max(2, round(len(script_text) / 330)) if script_text else 3
    return {
        "title_ja": title_ja,
        "why_selected": why_selected,
        "script_text": script_text,
        "estimated_minutes": est_minutes,
        "next_research_questions": [str(q).strip() for q in next_qs if str(q).strip()],
    }


def cleanup_old_unadopted_audio(days: int = 14) -> List[str]:
    """
    Delete pre-synthesized curator MP3 files older than `days` if they were NOT adopted (enqueued).
    JSON files, scripts, research logs, and history.json are NEVER deleted.
    """
    ensure_dirs()
    cutoff = datetime.now(JST).date() - timedelta(days=days)
    deleted: List[str] = []

    for daily_file in CURATOR_DIR.glob("daily_*.json"):
        m = re.match(r"daily_(\d{4}-\d{2}-\d{2})\.json$", daily_file.name)
        if not m:
            continue
        date_str = m.group(1)
        try:
            file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date >= cutoff:
            continue

        daily = _read_json(daily_file)
        if not daily or not isinstance(daily.get("parts"), list):
            continue

        changed = False
        for part in daily["parts"]:
            if part.get("enqueued"):
                continue
            audio_fn = part.get("audio_filename")
            if not audio_fn:
                continue
            mp3_path = CURATOR_AUDIO_DIR / audio_fn
            if mp3_path.exists():
                try:
                    mp3_path.unlink()
                    deleted.append(str(mp3_path.name))
                    part["audio_status"] = "expired"
                    changed = True
                except Exception as e:
                    print(f"  [Curator/Cleanup] Failed to delete {mp3_path}: {e}")
        if changed:
            _atomic_write_json(daily_file, daily)

    if deleted:
        print(f"[Curator/Cleanup] Removed {len(deleted)} old unadopted MP3(s): {deleted}")
    return deleted


def _save_combined_scripts_txt(date_str: str, parts: List[Dict[str, Any]]) -> Path:
    """Save UTF-8 combined scripts_YYYY-MM-DD.txt artifact."""
    txt_path = CURATOR_DIR / f"scripts_{date_str}.txt"
    sections = []
    for p in parts:
        header = (
            f"============================================================\n"
            f"[{p['part_id']}] {p['part_label']} : {p['title']}\n"
            f"出典: {p.get('source', {}).get('source_name', 'Cross-Theme Synthesis')} "
            f"({p.get('source', {}).get('url', '')})\n"
            f"============================================================\n"
        )
        sections.append(header + (p.get("script_text") or "").strip())
    txt_path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    return txt_path


async def run_daily_curation(
    date_str: Optional[str] = None,
    synthesize_audio: bool = True,
    enable_grounding: bool = True,
) -> Dict[str, Any]:
    """
    Execute the complete Personal Knowledge Curator pipeline for `date_str` (default: today JST):
      1. Cleanup unadopted MP3s older than 14 days
      2. Crawl & strictly verify candidates (RSS, OpenAlex, J-STAGE, Gemini Grounding)
      3. Run 3 independent evaluations (Core, Discovery, Wild Card — Wild Card isolated from preferences)
      4. Slot-based selection (Core, Discovery, Wild Card) + Part 4 Connections
      5. Generate 7-step Japanese audio scripts & immediately persist daily JSON, scripts txt, research log
      6. Incrementally synthesize VOICEPEAK MP3s part-by-part, updating daily_YYYY-MM-DD.json after EACH part!
    """
    ensure_dirs()
    target_date = date_str or today_jst_str()

    if _CURATOR_STATUS.get("running"):
        raise RuntimeError("Curator pipeline is already running.")

    _update_status(
        running=True,
        date=target_date,
        phase="cleanup",
        message="古い未採用音声を整理中...",
        progress=5,
        parts_ready=0,
        parts_total=4,
        started_at=datetime.now(JST).isoformat(),
        finished_at=None,
        error=None,
    )

    try:
        cleanup_old_unadopted_audio(days=14)
        history = load_history()
        seen_urls = set(history.get("seen_urls", []))
        seen_titles = set(history.get("seen_titles", []))
        core_pref = history.get("core_preference_summary") or DEFAULT_CORE_PREFERENCE

        # Phase 1: Crawl & verify sources
        _update_status(
            phase="crawling",
            message="優先ソース・OpenAlex・J-STAGE・Web探索と本文検証を実行中...",
            progress=15,
        )
        crawl_res = await asyncio.to_thread(
            curator_sources.collect_and_verify_candidates,
            seen_urls,
            seen_titles,
            core_pref,
            enable_grounding,
        )
        verified = crawl_res["verified"]
        rejected = crawl_res["rejected"]
        new_sources = crawl_res["new_sources_discovered"]
        crawl_stats = crawl_res["stats"]

        if not verified:
            raise RuntimeError("検証を通過した無料公開記事が0件でした。ネットワーク接続を確認してください。")

        # Partition verified candidates for 3 independent tracks (with fallback overlap if a bucket is small)
        core_bucket = [c for c in verified if c.get("track_hint") == "core"]
        disc_bucket = [c for c in verified if c.get("track_hint") == "discovery"]
        wc_bucket = [c for c in verified if c.get("track_hint") == "wildcard"]

        # Ensure each track has sufficient candidates to evaluate
        if len(core_bucket) < 5:
            extras = [c for c in verified if c not in core_bucket][: 5 - len(core_bucket)]
            core_bucket = core_bucket + extras
        if len(disc_bucket) < 5:
            extras = [c for c in verified if c not in disc_bucket][: 5 - len(disc_bucket)]
            disc_bucket = disc_bucket + extras
        if len(wc_bucket) < 5:
            extras = [c for c in verified if c not in wc_bucket][: 5 - len(wc_bucket)]
            wc_bucket = wc_bucket + extras

        # Phase 2: 3 independent 7-axis evaluations
        _update_status(
            phase="evaluating",
            message=f"検証通過 {len(verified)} 件を3トラック独立・7軸加重評価中...",
            progress=35,
        )
        core_eval, disc_eval, wc_eval = await asyncio.gather(
            asyncio.to_thread(_evaluate_track, core_bucket[:12], "core", core_pref),
            asyncio.to_thread(_evaluate_track, disc_bucket[:12], "discovery", core_pref),
            asyncio.to_thread(_evaluate_track, wc_bucket[:12], "wildcard", ""),
        )

        # Phase 3: Slot-based selection
        selected = select_slots(core_eval, disc_eval, wc_eval)
        selected_urls = {
            curator_sources.normalize_url(c["url"]): slot
            for slot, c in selected.items()
            if c and c.get("url")
        }

        # Phase 4: Generate 4-part 7-step scripts
        _update_status(
            phase="scripting",
            message="4パート（7ステップ構成）の音声解説原稿を生成中...",
            progress=55,
        )

        parts: List[Dict[str, Any]] = []
        slot_meta = [
            ("part1_core", "core", "Part 1: Core Deep Dive"),
            ("part2_discovery", "discovery", "Part 2: Discovery Deep Dive"),
            ("part3_wildcard", "wildcard", "Part 3: Wild Card"),
        ]
        for part_id, part_type, part_label in slot_meta:
            cand = selected.get(part_type)
            if not cand:
                continue
            gen = await asyncio.to_thread(_generate_part_script, part_type, cand)
            parts.append(
                {
                    "part_id": part_id,
                    "part_type": part_type,
                    "part_label": part_label,
                    "title": gen["title_ja"],
                    "why_selected": gen["why_selected"],
                    "source": {
                        "original_title": cand.get("title", ""),
                        "source_name": cand.get("source_name", ""),
                        "author": cand.get("author") or "記載なし",
                        "published_at": cand.get("published_at") or "記載なし",
                        "url": cand.get("url", ""),
                        "access_type": cand.get("access_type", "free_full_text"),
                        "lang": cand.get("lang", "en"),
                    },
                    "scores": cand.get("scores", {}),
                    "weighted_score": cand.get("weighted_score", 0.0),
                    "interest_fit": cand.get("interest_fit", 0.0),
                    "novelty": cand.get("novelty", 0.0),
                    "estimated_minutes": gen["estimated_minutes"],
                    "script_chars": len(gen["script_text"]),
                    "script_text": gen["script_text"],
                    "audio_filename": f"{target_date}_{part_id}.mp3",
                    "audio_status": "pending" if synthesize_audio else "script_only",
                    "audio_duration_sec": 0.0,
                    "enqueued": False,
                    "queue_item_id": None,
                    "feedback": None,
                }
            )

        # Part 4: Connections
        next_questions: List[str] = []
        if parts:
            conn = await asyncio.to_thread(_generate_connections_script, parts)
            next_questions = conn.get("next_research_questions", [])
            parts.append(
                {
                    "part_id": "part4_connections",
                    "part_type": "connections",
                    "part_label": "Part 4: Connections",
                    "title": conn["title_ja"],
                    "why_selected": conn["why_selected"],
                    "source": {
                        "original_title": "Cross-Theme Synthesis",
                        "source_name": "Readout Curator Synthesis",
                        "author": "AI Curator",
                        "published_at": target_date,
                        "url": "",
                        "access_type": "synthesis",
                        "lang": "ja",
                    },
                    "scores": {},
                    "weighted_score": None,
                    "interest_fit": None,
                    "novelty": None,
                    "estimated_minutes": conn["estimated_minutes"],
                    "script_chars": len(conn["script_text"]),
                    "script_text": conn["script_text"],
                    "audio_filename": f"{target_date}_part4_connections.mp3",
                    "audio_status": "pending" if synthesize_audio else "script_only",
                    "audio_duration_sec": 0.0,
                    "enqueued": False,
                    "queue_item_id": None,
                    "feedback": None,
                }
            )

        # Build Research Log (all evaluated + rejected candidates, excluding raw full body to keep JSON compact)
        all_eval_by_url: Dict[str, Dict[str, Any]] = {}
        for ev_list in (core_eval, disc_eval, wc_eval):
            for c in ev_list:
                nu = curator_sources.normalize_url(c.get("url", ""))
                if nu not in all_eval_by_url or c.get("weighted_score", 0) > all_eval_by_url[nu].get("weighted_score", 0):
                    all_eval_by_url[nu] = c

        log_candidates: List[Dict[str, Any]] = []
        for nu, c in all_eval_by_url.items():
            adopted_slot = selected_urls.get(nu)
            log_candidates.append(
                {
                    "title": c.get("title", ""),
                    "url": c.get("url", ""),
                    "source_name": c.get("source_name", ""),
                    "author": c.get("author", ""),
                    "published_at": c.get("published_at", ""),
                    "access_type": c.get("access_type", ""),
                    "content_length": c.get("content_length", 0),
                    "eval_track": c.get("eval_track", ""),
                    "weighted_score": c.get("weighted_score", 0.0),
                    "scores": c.get("scores", {}),
                    "status": f"selected:{adopted_slot}" if adopted_slot else "not_selected",
                    "reason": (
                        f"{adopted_slot.upper()} 枠として採用: {c.get('eval_reason', '')}"
                        if adopted_slot
                        else f"枠選考で不採用（スコア {c.get('weighted_score', 0.0)}）: {c.get('eval_reason', '')}"
                    ),
                }
            )
        # Sort evaluated candidates by score descending
        log_candidates.sort(key=lambda x: (not x["status"].startswith("selected:"), -(x.get("weighted_score") or 0.0)))

        for r in rejected:
            log_candidates.append(
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "source_name": r.get("source_name", ""),
                    "author": r.get("author", ""),
                    "published_at": r.get("published_at", ""),
                    "access_type": "rejected",
                    "content_length": r.get("content_length", 0),
                    "eval_track": r.get("track_hint", ""),
                    "weighted_score": None,
                    "scores": {},
                    "status": "rejected",
                    "reason": f"検証除外: {r.get('rejection_reason', 'unknown')}",
                }
            )

        research_log = {
            "date": target_date,
            "generated_at": datetime.now(JST).isoformat(),
            "stats": crawl_stats,
            "new_sources_discovered": new_sources,
            "next_research_questions": next_questions,
            "candidates": log_candidates,
        }

        daily_doc = {
            "date": target_date,
            "generated_at": datetime.now(JST).isoformat(),
            "shortfall_warning": crawl_stats.get("shortfall_warning"),
            "parts_total": len(parts),
            "parts_ready": 0,
            "parts": parts,
        }

        # Persist scripts, research log, and initial daily JSON BEFORE TTS starts
        daily_path = CURATOR_DIR / f"daily_{target_date}.json"
        rlog_path = CURATOR_DIR / f"research_log_{target_date}.json"
        _atomic_write_json(daily_path, daily_doc)
        _atomic_write_json(rlog_path, research_log)
        _save_combined_scripts_txt(target_date, parts)

        # Update seen_urls / seen_titles in history.json for selected items
        for p in parts:
            src_url = (p.get("source") or {}).get("url") or ""
            orig_title = (p.get("source") or {}).get("original_title") or ""
            if src_url:
                nu = curator_sources.normalize_url(src_url)
                if nu not in history["seen_urls"]:
                    history["seen_urls"].append(nu)
            if orig_title and orig_title != "Cross-Theme Synthesis":
                nt = orig_title.strip().lower()
                if nt not in history["seen_titles"]:
                    history["seen_titles"].append(nt)
        save_history(history)

        # Phase 5: Incremental VOICEPEAK TTS synthesis — publish each part immediately upon completion!
        if synthesize_audio:
            total_parts = len(parts)
            for idx, part in enumerate(parts):
                part["audio_status"] = "synthesizing"
                _atomic_write_json(daily_path, daily_doc)
                _update_status(
                    phase="synthesizing",
                    message=f"音声合成中 ({idx + 1}/{total_parts}): {part['part_label']} — {part['title']}",
                    progress=60 + int((idx / max(1, total_parts)) * 38),
                    parts_ready=daily_doc["parts_ready"],
                    parts_total=total_parts,
                )
                mp3_path = CURATOR_AUDIO_DIR / part["audio_filename"]
                try:
                    await poc_pipeline.generate_speech_async(
                        part["script_text"],
                        mp3_path,
                        voice=poc_pipeline.DEFAULT_VOICE,
                    )
                    dur = 0.0
                    try:
                        dur = round(MP3(str(mp3_path)).info.length, 1)
                    except Exception:
                        dur = round(len(part["script_text"]) / 5.5, 1)
                    part["audio_status"] = "ready"
                    part["audio_duration_sec"] = dur
                    daily_doc["parts_ready"] = sum(1 for x in parts if x.get("audio_status") == "ready")
                except Exception as e:
                    print(f"  [Curator/TTS] Failed for {part['part_id']}: {e}")
                    part["audio_status"] = "failed"
                    part["audio_error"] = str(e)

                # Immediately publish updated daily JSON so user can listen to ready parts right away!
                _atomic_write_json(daily_path, daily_doc)
                _update_status(
                    parts_ready=daily_doc["parts_ready"],
                    parts_total=total_parts,
                )

        _update_status(
            running=False,
            phase="completed",
            message=f"{target_date} のキュレーション完了（音声準備完了: {daily_doc['parts_ready']}/{len(parts)}）",
            progress=100,
            parts_ready=daily_doc["parts_ready"],
            parts_total=len(parts),
            finished_at=datetime.now(JST).isoformat(),
            error=None,
        )
        return daily_doc

    except Exception as e:
        print(f"[Curator] Pipeline failed: {e}")
        _update_status(
            running=False,
            phase="failed",
            message=f"エラー: {e}",
            finished_at=datetime.now(JST).isoformat(),
            error=str(e),
        )
        raise


def enqueue_curator_part(date_str: str, part_id: str) -> Tuple[Dict[str, Any], bool, Optional[str]]:
    """
    Add a Curator recommended part into Readout's main playback queue.
    Returns: (queue_item, needs_bg_tts, script_text_if_needed)
    - If pre-synthesized MP3 exists and is ready: copies MP3 & TXT immediately -> status='ready' (0-second wait!)
    - Otherwise: creates processing item so caller can synthesize from existing script in background.
    """
    ensure_dirs()
    daily_path = CURATOR_DIR / f"daily_{date_str}.json"
    daily = _read_json(daily_path)
    if not daily:
        raise FileNotFoundError(f"Curator daily file not found for {date_str}")

    target_part = None
    for p in daily.get("parts", []):
        if p.get("part_id") == part_id:
            target_part = p
            break
    if not target_part:
        raise ValueError(f"Part '{part_id}' not found in daily {date_str}")

    src_url = (target_part.get("source") or {}).get("url") or f"curator:{date_str}:{part_id}"
    part_tag = target_part.get("part_type", "curator").upper()
    display_title = f"[{part_tag}] {target_part.get('title', 'Curator Episode')}"
    script_text = target_part.get("script_text") or ""
    summary = (target_part.get("why_selected") or script_text[:150]).replace("\n", " ").strip()

    item = queue_manager.add_item(
        url=src_url,
        title=display_title,
        item_type="curator",
    )
    item_id = item["id"]
    dest_mp3 = OUTPUT_DIR / f"{item_id}.mp3"
    dest_txt = OUTPUT_DIR / f"{item_id}.txt"
    dest_txt.write_text(script_text, encoding="utf-8")

    pre_mp3 = CURATOR_AUDIO_DIR / (target_part.get("audio_filename") or f"{date_str}_{part_id}.mp3")
    needs_bg_tts = True

    if target_part.get("audio_status") == "ready" and pre_mp3.exists() and pre_mp3.stat().st_size > 0:
        shutil.copy2(str(pre_mp3), str(dest_mp3))
        dur = target_part.get("audio_duration_sec") or 0.0
        if not dur:
            try:
                dur = round(MP3(str(dest_mp3)).info.length, 1)
            except Exception:
                dur = round(len(script_text) / 5.5, 1)
        item = queue_manager.update_item(
            item_id,
            title=display_title,
            summary=summary,
            audio_duration_sec=dur,
            status="ready",
            error_message=None,
        ) or item
        needs_bg_tts = False
    else:
        item = queue_manager.update_item(
            item_id,
            title=display_title,
            summary=summary,
            status="processing",
        ) or item

    # Mark enqueued in daily JSON so 14-day cleanup protects it and UI shows queued state
    target_part["enqueued"] = True
    target_part["queue_item_id"] = item_id
    _atomic_write_json(daily_path, daily)

    return item, needs_bg_tts, (script_text if needs_bg_tts else None)


def _rebuild_core_preference_summary(feedback_list: List[Dict[str, Any]]) -> str:
    """
    Update Core-only preference summary deterministically from accumulated feedback.
    (Used exclusively by Track A Core & Track B Discovery — NEVER passed to Wild Card!)
    """
    if not feedback_list:
        return DEFAULT_CORE_PREFERENCE

    high_rated = [f for f in feedback_list if (f.get("rating") or 0) >= 4]
    low_rated = [f for f in feedback_list if (f.get("rating") or 0) <= 2]

    pos_reasons: Dict[str, int] = {}
    neg_reasons: Dict[str, int] = {}
    for f in high_rated[-20:]:
        for r in f.get("reasons") or []:
            pos_reasons[r] = pos_reasons.get(r, 0) + 1
    for f in low_rated[-20:]:
        for r in f.get("reasons") or []:
            neg_reasons[r] = neg_reasons.get(r, 0) + 1

    lines = [DEFAULT_CORE_PREFERENCE]
    if high_rated:
        recent_likes = [f"「{f.get('title', '')}」" for f in high_rated[-5:] if f.get("title")]
        top_pos = sorted(pos_reasons.items(), key=lambda x: -x[1])[:4]
        pos_str = "・".join(k for k, _ in top_pos)
        lines.append(f"最近高評価だったテーマ: {', '.join(recent_likes)}" + (f"（好まれた理由: {pos_str}）" if pos_str else ""))
    if low_rated:
        recent_dislikes = [f"「{f.get('title', '')}」" for f in low_rated[-4:] if f.get("title")]
        top_neg = sorted(neg_reasons.items(), key=lambda x: -x[1])[:4]
        neg_str = "・".join(k for k, _ in top_neg)
        lines.append(f"既知または評価が低かった傾向: {', '.join(recent_dislikes)}" + (f"（避けたい傾向: {neg_str}）" if neg_str else ""))
    return "\n".join(lines)


def record_feedback(
    date_str: str,
    part_id: str,
    rating: Any,
    reasons: Optional[List[str]] = None,
    memo: str = "",
) -> Dict[str, Any]:
    """Save 5-level feedback + reason chips + memo into both daily JSON and history.json, updating Core preference summary."""
    ensure_dirs()
    # Normalize rating (int 1..5 or Japanese label)
    if isinstance(rating, str) and rating.strip() in LABEL_TO_RATING:
        rating_int = LABEL_TO_RATING[rating.strip()]
    else:
        rating_int = int(rating)
    if rating_int not in RATING_LABELS:
        raise ValueError(f"Invalid rating: {rating}. Must be 1..5 or valid label.")
    rating_label = RATING_LABELS[rating_int]

    clean_reasons = [str(r).strip() for r in (reasons or []) if str(r).strip()]
    clean_memo = (memo or "").strip()

    daily_path = CURATOR_DIR / f"daily_{date_str}.json"
    daily = _read_json(daily_path)
    part_title = ""
    part_type = ""
    part_url = ""

    fb_entry = {
        "date": date_str,
        "part_id": part_id,
        "rating": rating_int,
        "rating_label": rating_label,
        "reasons": clean_reasons,
        "memo": clean_memo,
        "timestamp": datetime.now(JST).isoformat(),
    }

    if daily and isinstance(daily.get("parts"), list):
        for p in daily["parts"]:
            if p.get("part_id") == part_id:
                part_title = p.get("title", "")
                part_type = p.get("part_type", "")
                part_url = (p.get("source") or {}).get("url", "")
                p["feedback"] = fb_entry
                break
        _atomic_write_json(daily_path, daily)

    fb_entry["title"] = part_title
    fb_entry["part_type"] = part_type
    fb_entry["url"] = part_url

    history = load_history()
    # Replace existing feedback for same (date, part_id) if re-submitted
    history["feedback"] = [
        f
        for f in history.get("feedback", [])
        if not (f.get("date") == date_str and f.get("part_id") == part_id)
    ]
    history["feedback"].append(fb_entry)
    history["core_preference_summary"] = _rebuild_core_preference_summary(history["feedback"])
    save_history(history)

    return {
        "feedback": fb_entry,
        "core_preference_summary": history["core_preference_summary"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Readout Personal Knowledge Curator CLI")
    parser.add_argument("--run", action="store_true", help="Run daily curation and pre-synthesize audio")
    parser.add_argument("--date", type=str, default=None, help="Target date YYYY-MM-DD (default: today JST)")
    parser.add_argument("--no-audio", action="store_true", help="Skip TTS synthesis (scripts only)")
    parser.add_argument("--no-grounding", action="store_true", help="Disable Gemini Search Grounding")
    args = parser.parse_args()

    if args.run:
        res = asyncio.run(
            run_daily_curation(
                date_str=args.date,
                synthesize_audio=not args.no_audio,
                enable_grounding=not args.no_grounding,
            )
        )
        print(f"[+] Daily curation finished for {res['date']} ({res['parts_ready']}/{res['parts_total']} audio ready)")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
