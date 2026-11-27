#!/usr/bin/env python3
"""
Readout - Personal Knowledge Curator Engine
Implements:
  - 3 independent discovery tracks (Core, Discovery, Wild Card — Wild Card structurally isolated from user preference profile)
  - 7-axis weighted evaluation + slot-based selection (9 parts: Core×3, Discovery×3, Wild Card×3, not naive top-N, strict Chapters 1-6 quality filtering)
  - Independent 9-part, source-faithful calm Japanese audio narration script generation (no AI speculation/embellishment, no exaggeration, natural variable length, strict katakana reading)
  - Incremental VOICEPEAK pre-synthesis (each part published to daily_YYYY-MM-DD.json as soon as its MP3 is ready, with tts_engine tracking)
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
    "parts_total": 9,
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


_EVAL_QUALITY_CRITERIA = """【高く評価すべき記事（良質な論考の基準）】
- 「なぜその現象が起きるのか」という構造・メカニズムに踏み込んだ論考
- 定説や通説を一次データ・フィールド実験・実証研究で検証した記事
- 流行やブームの「その後」や実態を冷静に振り返った分析
- 成功と失敗を分ける構造的な差異を明らかにした比較・事例研究
- 常識や直感に反する発見、異分野の概念を結びつける独自の視座
- 一過性の話題にとどまらず、数年後に聴き返しても価値が残る長期的な知見

【厳しく減点すべき望ましくない記事（低評価ルール）】
- 個別企業のレイオフ・人事交代・四半期決算・新商品発売・単なるニュース速報
- 単なる AI ツールやサービスの機能紹介・表面的なハウツー記事
- 「成功するリーダーの7つの習慣」系の一般論・薄いリスト記事・自己啓発
- 根拠の薄いトレンド予測・当たり前の事実を大げさに語る表層記事・PR色の強い記事
※上記に該当する記事は容赦なく低い点（特に `question_depth`、`originality`、`evidence_quality`、`long_term_value` を 1.0〜2.0 点以下）を付けること。"""


def build_core_evaluation_prompt(
    candidates: List[Dict[str, Any]],
    core_preference_summary: str,
) -> str:
    """Track A (Core): Uses user's Core preference summary."""
    cand_text = _format_candidates_for_prompt(candidates)
    return f"""あなたは優秀な知的キュレーター（Track A: Core Deep Dive）です。
読者の関心プロファイルと過去のフィードバックを踏まえ、以下の実在・検証済み候補記事それぞれを7軸（各0〜5点、0.5点刻み可）と関心適合度・新規性で厳正に評価してください。

【読者のCore関心プロファイル ＆ 過去フィードバック傾向】
{core_preference_summary}

【選定方針（重要）】
- 本枠は他枠（Discovery / Wild Card）とは完全に独立しています。他枠と共通テーマに無理やり寄せる必要は一切なく、この1枠単体で「経営者・マーケティングリサーチ／インサイト実務家としての知的好奇心・HBR的満足感」を最大化する最高品質の記事を高く評価してください。
- 読者の過去の高評価テーマ・好まれた理由・メモの傾向に強く合致する記事は `interest_fit` を高く、逆に「既知だった」「抽象的すぎ」など低評価傾向に該当する記事は厳しめに評価してください。
- 純粋に難解な学術アブストラクトだけの記事（Access: open_abstract）より、十分な本文と具体例・実証データ・実務的洞察を持つ良質な論考記事（Knowledge at Wharton, MIT SMR, Behavioral Scientist, The Conversation, Works in Progress, Our World in Data, Greenbook, Quirk's, RIETI 等の free_full_text）を優先的に高く評価してください。

{_EVAL_QUALITY_CRITERIA}

【評価軸（各0〜5点）】
- question_depth (問いの深さ)
- explanatory_power (説明力)
- evidence_quality (根拠の質)
- originality (独自性)
- long_term_value (長期的価値)
- curiosity (好奇心)
- audio_suitability (音声適性：具体例やストーリーがあり耳で聴いて面白いほど高評価)
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
    return f"""あなたは優秀な知的キュレーター（Track B: Discovery Deep Dive）です。
読者の主領域に隣接する幅広い分野（行動科学、経済学、社会システム、心理学、歴史、科学技術の応用など）から、新しい視座と知的な発見をもたらす候補を7軸（各0〜5点）＋関心適合度＋新規性で評価してください。

【参考（読者の主領域・好みの方向性）】
{core_preference_summary}

【選定方針（重要）】
- 本枠は他枠（Core / Wild Card）と完全に独立しています。共通テーマに無理やり寄せる必要は一切なく、この1枠単体で「経営者・マーケティングリサーチ／インサイト実務家としての知的好奇心・HBR的満足感」と視野の広がりを最大化する記事を高く評価してください。
- 純粋に難解な学術アブストラクトだけの記事（Access: open_abstract）より、十分な本文と具体例・実証データ・豊かなストーリーと洞察を持つ良質な論考記事（Knowledge at Wharton, MIT SMR, Behavioral Scientist, The Conversation, Works in Progress, Our World in Data, Greenbook, Quirk's, RIETI 等の free_full_text）を優先的に高く評価してください。

{_EVAL_QUALITY_CRITERIA}

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
特定の読者の過去の好みや専門領域、他枠のテーマは一切考慮せず、純粋に「知的好奇心を強く揺さぶるか」「意外性・独自性・問いの面白さがあるか」「根拠と説明力があり耳で聴いて引き込まれるか」だけで以下の実在・検証済み候補を7軸（各0〜5点）と新規性で評価してください。

【選定方針（重要）】
- 他枠と共通テーマに寄せる必要は一切ありません。普段自分では探さない意外な領域の、独立した1本の良質な読み物として最も面白い記事を高く評価してください。
- 短い学術アブストラクトだけ（Access: open_abstract）より、十分な本文と具体例・実証データ・魅力的な語り口を持つ良質な論考記事（Works in Progress, Our World in Data, The Conversation, Behavioral Scientist 等の free_full_text）を優先的に高く評価してください。

{_EVAL_QUALITY_CRITERIA}

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


_THEME_STOPWORDS: Set[str] = {
    "the", "and", "for", "with", "how", "why", "what", "when", "where", "who",
    "from", "that", "this", "these", "those", "are", "was", "were", "have", "has",
    "into", "about", "your", "our", "their", "new", "more", "can", "will", "not",
    "but", "out", "use", "using", "study", "paper", "article", "report", "analysis",
}

MIN_SLOT_WEIGHTED_SCORE = 2.5


def _extract_theme_tokens(title: str) -> Set[str]:
    """Extract normalized significant English & Japanese tokens from a title for theme-overlap detection."""
    text = (title or "").lower()
    en_tokens = {
        tok for tok in re.findall(r"[a-z0-9]{3,}", text) if tok not in _THEME_STOPWORDS
    }
    # Japanese kanji / katakana content chunks (2+ chars)
    ja_tokens = set(re.findall(r"[\u4e00-\u9fff\u30a0-\u30ff]{2,}", text))
    return en_tokens | ja_tokens


def _is_similar_theme(title_a: str, title_b: str) -> bool:
    """Return True if two titles refer to essentially the same topic/article."""
    na = (title_a or "").strip().lower()
    nb = (title_b or "").strip().lower()
    if not na or not nb:
        return False
    if na == nb:
        return True
    toks_a = _extract_theme_tokens(na)
    toks_b = _extract_theme_tokens(nb)
    if not toks_a or not toks_b:
        return False
    overlap = toks_a & toks_b
    if len(overlap) < 2:
        return False
    min_len = min(len(toks_a), len(toks_b))
    union_len = len(toks_a | toks_b)
    return (len(overlap) / max(1, min_len) >= 0.5) or (len(overlap) / max(1, union_len) >= 0.45)


def select_slots(
    core_evaluated: List[Dict[str, Any]],
    discovery_evaluated: List[Dict[str, Any]],
    wildcard_evaluated: List[Dict[str, Any]],
    min_weighted_score: float = MIN_SLOT_WEIGHTED_SCORE,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """
    Slot-based selection for 9 daily parts (3 per track, NOT naive top-N by total weighted score):
      - Core #1 / #2 / #3: balances weighted_score (65%) + interest_fit (35%)
      - Discovery #1 / #2 / #3: balances weighted_score (65%) + novelty (35%)
      - Wild Card #1 / #2 / #3: purely from wildcard_evaluated (preference-blind),
        balancing weighted_score (45%) + curiosity (30%) + originality (25%)
    Deduplication & quality rules:
      - Never selects the same normalized URL twice.
      - Excludes candidates with overlapping/same theme as already selected items.
      - Prefers distinct domains when alternatives exist.
      - Does NOT pad with low-quality items (< min_weighted_score) or cross-track backfill for #2/#3 slots.
    """
    used_urls: Set[str] = set()
    used_titles: List[str] = []
    used_domains: Set[str] = set()

    def _pick_best(
        pool: List[Dict[str, Any]],
        key_fn,
        prefer_new_domain: bool = True,
        enforce_quality_floor: bool = True,
    ) -> Optional[Dict[str, Any]]:
        available = [
            c
            for c in pool
            if curator_sources.normalize_url(c.get("url", "")) not in used_urls
            and not any(_is_similar_theme(c.get("title", ""), t) for t in used_titles)
            and (not enforce_quality_floor or float(c.get("weighted_score", 0.0)) >= min_weighted_score)
        ]
        if not available:
            return None
        if prefer_new_domain:
            diff_dom = [c for c in available if (c.get("domain") or "") not in used_domains]
            if diff_dom:
                available = diff_dom
        best = max(available, key=key_fn)
        used_urls.add(curator_sources.normalize_url(best.get("url", "")))
        if best.get("title"):
            used_titles.append(best["title"])
        if best.get("domain"):
            used_domains.add(best["domain"])
        return best

    def _access_adj(c: Dict[str, Any]) -> float:
        # Prefer rich full-text essays/articles over abstract-only academic stubs
        return -0.15 if c.get("access_type") == "open_abstract" else 0.0

    core_key = lambda c: 0.65 * c.get("weighted_score", 0.0) + 0.35 * c.get("interest_fit", 0.0) + _access_adj(c)
    disc_key = lambda c: 0.65 * c.get("weighted_score", 0.0) + 0.35 * c.get("novelty", 0.0) + _access_adj(c)
    wc_key = lambda c: (
        0.45 * c.get("weighted_score", 0.0)
        + 0.30 * c.get("scores", {}).get("curiosity", 0.0)
        + 0.25 * c.get("scores", {}).get("originality", 0.0)
        + _access_adj(c)
    )

    # Round 1: Pick #1 for each track first so all 3 tracks are represented even in smaller pools
    core_1 = _pick_best(
        core_evaluated or discovery_evaluated or wildcard_evaluated,
        key_fn=core_key,
        prefer_new_domain=False,
    )
    discovery_1 = _pick_best(
        discovery_evaluated or core_evaluated or wildcard_evaluated,
        key_fn=disc_key,
        prefer_new_domain=True,
    )
    # Wild Card #1 strictly prefers wildcard_evaluated (preference-blind)
    wildcard_1 = _pick_best(
        wildcard_evaluated or discovery_evaluated or core_evaluated,
        key_fn=wc_key,
        prefer_new_domain=True,
    )

    # Round 2: Pick #2 for each track strictly from its own evaluated pool (no artificial padding)
    core_2 = _pick_best(
        core_evaluated,
        key_fn=core_key,
        prefer_new_domain=True,
    )
    discovery_2 = _pick_best(
        discovery_evaluated,
        key_fn=disc_key,
        prefer_new_domain=True,
    )
    wildcard_2 = _pick_best(
        wildcard_evaluated,
        key_fn=wc_key,
        prefer_new_domain=True,
    )

    # Round 3: Pick #3 for each track strictly from its own evaluated pool (no artificial padding)
    core_3 = _pick_best(
        core_evaluated,
        key_fn=core_key,
        prefer_new_domain=True,
    )
    discovery_3 = _pick_best(
        discovery_evaluated,
        key_fn=disc_key,
        prefer_new_domain=True,
    )
    wildcard_3 = _pick_best(
        wildcard_evaluated,
        key_fn=wc_key,
        prefer_new_domain=True,
    )

    return {
        "core_1": core_1,
        "core_2": core_2,
        "core_3": core_3,
        "discovery_1": discovery_1,
        "discovery_2": discovery_2,
        "discovery_3": discovery_3,
        "wildcard_1": wildcard_1,
        "wildcard_2": wildcard_2,
        "wildcard_3": wildcard_3,
    }


def build_part_script_prompt(
    part_type: str,
    candidate: Dict[str, Any],
) -> str:
    """
    Build source-faithful, calm Japanese audio narration rewrite prompt.
    Strictly prohibits AI-invented commentary, counterarguments, lessons, or exaggerated hype language.
    """
    body_excerpt = (candidate.get("content") or "")[:16000]
    access_note = (
        "※これは公開アブストラクト（研究概要）に基づく記事です。アブストラクトに記載のない数値・詳細・背景知識やAI独自の解説は一切推測・追記せず、『公開概要の範囲では』と明示したうえで、記載されている内容のみを簡潔に伝えてください。"
        if candidate.get("access_type") == "open_abstract"
        else "※元記事に書かれている問い・主張・根拠・データ・具体例・結論のみに厳密に基づき、記事にない背景補足・反論・教訓・評価を一切付け足さないでください。"
    )

    return f"""あなたは実在する海外・国内の論考記事を、耳で聴きやすい自然な日本語に忠実に翻訳・口語化する音声ナレーションライターです。
以下の記事本文の内容を、過不足なく、落ち着いた知的な語り口の日本語ナレーション原稿（「です・ます」調）にまとめてください。

【最重要方針：元記事への忠実性と落ち着いたトーン】
1. **元記事に書かれている内容のみを伝える**:
   - 元記事に含まれる「問い・主張・根拠・データ・具体例・結論」のみを忠実に翻訳・口語化してまとめてください。
   - **AI 独自の解釈・意見・反論・教訓・当たり前の補足・結びの問いかけを一切追記しないでください。**
   - 元記事にない数値・固有名詞・研究結果・背景事情を絶対に創作しないでください。
2. **大げさな表現・誇張・煽り語の厳禁**:
   - 「ここで革新的なことは」「画期的な」「パラダイムシフト」「驚くべきことに」「劇的な」「ゲームチェンジャー」「衝撃的な」などの誇張・煽り表現は一切使用しないでください。
   - 元記事の論調を尊重し、静かで知的、かつ明晰な落ち着いたトーンで淡々と分かりやすく語ってください。
3. **分量は元記事の情報量に応じて自然に決める**:
   - 文字数を稼ぐための水増しや引き伸ばしは厳禁です。
   - 目安として、元記事の要点・具体例・根拠を落とさず簡潔にまとめるため、通常 1,200〜2,800 字（音声で約 4〜8 分）程度になりますが、元記事が長大な論考なら長め、簡潔な記事なら短めに自然に伸縮させてください。
4. **冒頭と結びの形式**:
   - ラジオ風の挨拶（「こんにちは」「おはようございます」「今日も良い一日を」「〇〇の時間です」等）は一切入れないでください。
   - 冒頭は必ず「{{媒体名カタカナ}}、『{{日本語タイトル}}』。」というシンプルな宣言から始め、即座に元記事の内容へ入ってください。
   - 結びも応援やAI独自の問いかけは入れず、元記事の結論・まとめで静かに終えてください。
5. **音声合成（VOICEPEAK）向けフォーマットと徹底カタカナ化（最重要）**:
   - 表、図、URL、Markdown記号（#、*、-、・、`、~、|）、箇条書き記号は一切出力しないでください。句読点と自然な改行のみのプレーンテキストにしてください。
   - 音声合成エンジンがアルファベット表記をたどたどしく読むのを防ぐため、`script_text` 本文中の英単語・アルファベット略語・英語の媒体名・企業名・人名・専門用語は、**すべて耳で聴いて自然な日本語カタカナ読みに変換して出力し、アルファベットを本文に一切残さないでください**。
     - 変換例: AI → エーアイ、ROI → アールオーアイ、KPI → ケーピーアイ、LLM → エルエルエム、MIT → エムアイティー、Harvard Business Review → ハーバード・ビジネス・レビュー、Works in Progress → ワークス・イン・プログレス、Nature → ネイチャー、P&G → ピーアンドジー、PANAS → パナス、Deep Dive → ディープダイブ。
6. {access_note}

出力は必ず以下の JSON オブジェクトのみとしてください（解説文不要）。

出力フォーマット:
{{
  "title_ja": "元記事の内容を正確に表す簡潔な日本語タイトル（30〜50字）",
  "why_selected": "この記事が扱う問いと要点を端的に示す紹介文（80〜140字・誇張表現なし）",
  "script_text": "「媒体名カタカナ、『日本語タイトル』。」から始まる元記事忠実なプレーンテキスト音声原稿..."
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


def _generate_part_script(
    part_type: str,
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Generate source-faithful, calm ear-friendly Japanese audio narration script for Core / Discovery / Wild Card.
    Returns dict with title_ja, why_selected, script_text, estimated_minutes.
    """
    prompt = build_part_script_prompt(part_type, candidate)

    raw = _call_gemini_text(prompt, temperature=0.4, max_output_tokens=8192)
    try:
        parsed = _extract_json_block(raw)
        title_ja = (parsed.get("title_ja") or candidate.get("title") or "無題").strip()
        why_selected = (parsed.get("why_selected") or candidate.get("eval_reason") or "").strip()
        script_text = sanitize_curator_script(parsed.get("script_text") or "")
    except Exception:
        title_ja = candidate.get("title") or "無題"
        why_selected = candidate.get("eval_reason") or ""
        script_text = sanitize_curator_script(raw)

    est_minutes = max(1, round(len(script_text) / 330)) if script_text else 6
    return {
        "title_ja": title_ja,
        "why_selected": why_selected,
        "script_text": script_text,
        "estimated_minutes": est_minutes,
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
      4. Slot-based selection (9 independent parts: Core×3, Discovery×3, Wild Card×3)
      5. Generate source-faithful Japanese audio scripts & immediately persist daily JSON, scripts txt, research log
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
        parts_total=9,
        started_at=datetime.now(JST).isoformat(),
        finished_at=None,
        error=None,
    )

    try:
        cleanup_old_unadopted_audio(days=14)
        try:
            queue_manager.cleanup_listened_audio(retention_days=7)
        except Exception as ce:
            print(f"[Curator/Cleanup] Listened queue audio cleanup warning: {ce}")
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

        # Ensure each track has sufficient candidates to evaluate for 3 picks per track
        if len(core_bucket) < 8:
            extras = [c for c in verified if c not in core_bucket][: 8 - len(core_bucket)]
            core_bucket = core_bucket + extras
        if len(disc_bucket) < 8:
            extras = [c for c in verified if c not in disc_bucket][: 8 - len(disc_bucket)]
            disc_bucket = disc_bucket + extras
        if len(wc_bucket) < 8:
            extras = [c for c in verified if c not in wc_bucket][: 8 - len(wc_bucket)]
            wc_bucket = wc_bucket + extras

        # Phase 2: 3 independent 7-axis evaluations
        _update_status(
            phase="evaluating",
            message=f"検証通過 {len(verified)} 件を3トラック独立・7軸加重評価中...",
            progress=35,
        )
        core_eval, disc_eval, wc_eval = await asyncio.gather(
            asyncio.to_thread(_evaluate_track, core_bucket[:16], "core", core_pref),
            asyncio.to_thread(_evaluate_track, disc_bucket[:16], "discovery", core_pref),
            asyncio.to_thread(_evaluate_track, wc_bucket[:16], "wildcard", ""),
        )

        # Phase 3: Slot-based selection (9 parts: Core×3, Discovery×3, Wild Card×3)
        selected = select_slots(core_eval, disc_eval, wc_eval)
        selected_urls = {
            curator_sources.normalize_url(c["url"]): slot
            for slot, c in selected.items()
            if c and c.get("url")
        }

        # Phase 4: Generate independent 9-part source-faithful scripts
        _update_status(
            phase="scripting",
            message="9パート（Core×3 / Discovery×3 / Wild Card×3・元記事忠実リライト）の音声原稿を生成中...",
            progress=55,
        )

        parts: List[Dict[str, Any]] = []
        slot_meta = [
            ("part1_core_1", "core_1", "core", "Core #1: Core Deep Dive"),
            ("part1_core_2", "core_2", "core", "Core #2: Core Deep Dive"),
            ("part1_core_3", "core_3", "core", "Core #3: Core Deep Dive"),
            ("part2_discovery_1", "discovery_1", "discovery", "Discovery #1: Discovery Deep Dive"),
            ("part2_discovery_2", "discovery_2", "discovery", "Discovery #2: Discovery Deep Dive"),
            ("part2_discovery_3", "discovery_3", "discovery", "Discovery #3: Discovery Deep Dive"),
            ("part3_wildcard_1", "wildcard_1", "wildcard", "Wild Card #1: Wild Card"),
            ("part3_wildcard_2", "wildcard_2", "wildcard", "Wild Card #2: Wild Card"),
            ("part3_wildcard_3", "wildcard_3", "wildcard", "Wild Card #3: Wild Card"),
        ]
        for part_id, slot_key, part_type, part_label in slot_meta:
            cand = selected.get(slot_key)
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
                    "tts_engine": None,
                    "enqueued": False,
                    "queue_item_id": None,
                    "feedback": None,
                }
            )

        next_questions: List[str] = []

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
                        f"{re.sub(r'_(\d+)$', r' #\1', adopted_slot).upper()} 枠として採用: {c.get('eval_reason', '')}"
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

        shortfall_msgs: List[str] = []
        if crawl_stats.get("shortfall_warning"):
            shortfall_msgs.append(str(crawl_stats["shortfall_warning"]))
        if len(parts) < 9:
            shortfall_msgs.append(
                f"良質な候補が不足したため、目標 9 本に対し {len(parts)} 本のみの選出となりました（不足分は水増しせず見送り）。"
            )
        combined_shortfall = " ".join(shortfall_msgs) if shortfall_msgs else None

        daily_doc = {
            "date": target_date,
            "generated_at": datetime.now(JST).isoformat(),
            "shortfall_warning": combined_shortfall,
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
                    used_engine = await poc_pipeline.generate_speech_async(
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
                    part["tts_engine"] = used_engine
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
    src_name = (target_part.get("source") or {}).get("source_name") or ""
    part_tag = target_part.get("part_type", "curator").upper()
    display_title = f"[{part_tag}] {target_part.get('title', 'Curator Episode')}"
    script_text = target_part.get("script_text") or ""
    summary = (target_part.get("why_selected") or script_text[:150]).replace("\n", " ").strip()

    item = queue_manager.add_item(
        url=src_url,
        title=display_title,
        item_type="curator",
    )
    if src_name:
        queue_manager.update_item(item["id"], source_name=src_name)
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
            tts_engine=target_part.get("tts_engine"),
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


def mark_part_sent_to_matrix(date_str: str, part_id: str, note_id: str) -> Optional[Dict[str, Any]]:
    """Record that a Curator part was sent to Matrix Note."""
    ensure_dirs()
    daily_path = CURATOR_DIR / f"daily_{date_str}.json"
    daily = _read_json(daily_path)
    if not daily or not isinstance(daily.get("parts"), list):
        return None
    updated_part = None
    for p in daily["parts"]:
        if p.get("part_id") == part_id:
            p["sent_to_matrix_note"] = True
            p["matrix_note_id"] = note_id
            p["matrix_sent_at"] = datetime.now(JST).isoformat()
            updated_part = p
            break
    if updated_part:
        _atomic_write_json(daily_path, daily)
    return updated_part


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
        pos_memos = [f.get("memo", "").strip() for f in high_rated[-5:] if (f.get("memo") or "").strip()]
        if pos_memos:
            lines.append(f"読者からの高評価メモ: {' / '.join(pos_memos)}")
    if low_rated:
        recent_dislikes = [f"「{f.get('title', '')}」" for f in low_rated[-4:] if f.get("title")]
        top_neg = sorted(neg_reasons.items(), key=lambda x: -x[1])[:4]
        neg_str = "・".join(k for k, _ in top_neg)
        lines.append(f"既知または評価が低かった傾向: {', '.join(recent_dislikes)}" + (f"（避けたい傾向: {neg_str}）" if neg_str else ""))
        neg_memos = [f.get("memo", "").strip() for f in low_rated[-4:] if (f.get("memo") or "").strip()]
        if neg_memos:
            lines.append(f"読者からの低評価・改善メモ: {' / '.join(neg_memos)}")
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
