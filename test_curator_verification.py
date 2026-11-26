#!/usr/bin/env python3
"""
Automated Verification Suite for Readout Personal Knowledge Curator.
Tests:
  1. 7-axis weighted score calculation
  2. Slot-based selection (Core / Discovery / Wild Card distinct from naive top-N)
  3. Anti-overfitting guarantee (Wild Card prompt never receives preference profile)
  4. Mechanical paywall / 404 / short-body exclusion + academic abstract exception
  5. Zero-wait instant enqueue when pre-synthesized MP3 exists
  6. 5-level feedback saving & Core preference summary update
  7. FastAPI Curator endpoints smoke test via TestClient
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import curator_engine
import curator_sources
import main
import queue_manager


def test_seven_axis_weighted_score():
    print("[Test 1] 7-axis weighted score calculation...")
    # Weights: 0.20, 0.20, 0.20, 0.15, 0.10, 0.10, 0.05
    scores = {
        "question_depth": 5.0,     # 1.00
        "explanatory_power": 4.0,  # 0.80
        "evidence_quality": 4.0,   # 0.80
        "originality": 3.0,        # 0.45
        "long_term_value": 5.0,    # 0.50
        "curiosity": 2.0,          # 0.20
        "audio_suitability": 4.0,  # 0.20
    }
    expected = 3.95
    actual = curator_engine.compute_weighted_score(scores)
    assert actual == expected, f"Expected {expected}, got {actual}"

    # Clamping out-of-range values
    clamped = curator_engine.compute_weighted_score(
        {k: 99.0 for k in curator_engine.SEVEN_AXIS_WEIGHTS}
    )
    assert clamped == 5.0, f"Expected clamped 5.0, got {clamped}"
    print("  PASS")


def test_slot_based_selection():
    print("[Test 2] Slot-based selection (not naive top-N by weighted_score)...")
    # Candidate A has highest weighted_score (4.6) but low interest_fit (1.0)
    # Candidate B has slightly lower weighted_score (4.2) but max interest_fit (5.0) -> should win Core!
    core_pool = [
        {
            "title": "General High Score",
            "url": "https://a.example.com/1",
            "domain": "a.example.com",
            "weighted_score": 4.6,
            "interest_fit": 1.0,
            "novelty": 2.0,
            "scores": {"curiosity": 2.0, "originality": 2.0},
        },
        {
            "title": "Core Fit Winner",
            "url": "https://b.example.com/2",
            "domain": "b.example.com",
            "weighted_score": 4.2,
            "interest_fit": 5.0,
            "novelty": 3.0,
            "scores": {"curiosity": 3.0, "originality": 3.0},
        },
    ]
    disc_pool = [
        {
            "title": "Boring Familiar",
            "url": "https://c.example.com/3",
            "domain": "c.example.com",
            "weighted_score": 4.4,
            "interest_fit": 4.0,
            "novelty": 1.0,
            "scores": {"curiosity": 2.0, "originality": 2.0},
        },
        {
            "title": "Adjacent Novel Winner",
            "url": "https://d.example.com/4",
            "domain": "d.example.com",
            "weighted_score": 4.1,
            "interest_fit": 3.0,
            "novelty": 5.0,
            "scores": {"curiosity": 4.0, "originality": 4.0},
        },
    ]
    wc_pool = [
        {
            "title": "Dry Academic",
            "url": "https://e.example.com/5",
            "domain": "e.example.com",
            "weighted_score": 4.3,
            "interest_fit": 0.0,
            "novelty": 2.0,
            "scores": {"curiosity": 1.5, "originality": 1.5},
        },
        {
            "title": "Wild Card Curiosity Winner",
            "url": "https://f.example.com/6",
            "domain": "f.example.com",
            "weighted_score": 3.9,
            "interest_fit": 0.0,
            "novelty": 5.0,
            "scores": {"curiosity": 5.0, "originality": 5.0},
        },
    ]
    selected = curator_engine.select_slots(core_pool, disc_pool, wc_pool)
    assert selected["core"]["title"] == "Core Fit Winner"
    assert selected["discovery"]["title"] == "Adjacent Novel Winner"
    assert selected["wildcard"]["title"] == "Wild Card Curiosity Winner"
    print("  PASS")


def test_wildcard_anti_overfitting():
    print("[Test 3] Anti-overfitting: Wild Card prompt never receives user preference profile...")
    secret_pref = "SUPER_SECRET_USER_PREFERENCE_TOKEN_XYZ_999"
    sample_cands = [
        {
            "title": "Why Cities Grow Like Organisms",
            "url": "https://example.org/cities",
            "source_name": "Example Essay",
            "author": "Jane Doe",
            "published_at": "2026-11-01",
            "access_type": "free_full_text",
            "content": "A" * 1000,
        }
    ]
    core_prompt = curator_engine.build_core_evaluation_prompt(sample_cands, secret_pref)
    wc_prompt = curator_engine.build_wildcard_evaluation_prompt(sample_cands)
    assert secret_pref in core_prompt, "Core prompt must include preference summary"
    assert secret_pref not in wc_prompt, "Wild Card prompt MUST NOT include preference summary"

    # Also verify _evaluate_track(..., track='wildcard', core_preference_summary=secret_pref) ignores it
    captured_prompts = []

    def fake_gemini(prompt, **kwargs):
        captured_prompts.append(prompt)
        return json.dumps(
            [
                {
                    "index": 0,
                    "scores": {k: 4.0 for k in curator_engine.SEVEN_AXIS_WEIGHTS},
                    "interest_fit": 0.0,
                    "novelty": 4.5,
                    "reason_ja": "テスト評価",
                }
            ]
        )

    with patch.object(curator_engine, "_call_gemini_text", side_effect=fake_gemini):
        curator_engine._evaluate_track(sample_cands, "wildcard", core_preference_summary=secret_pref)

    assert len(captured_prompts) == 1
    assert secret_pref not in captured_prompts[0], "Wild Card LLM call leaked preference profile!"
    print("  PASS")


def test_paywall_and_verification_filters():
    print("[Test 4] Paywall, 404, short-body exclusion & academic abstract exception...")
    # A. Known paywall domain
    res_pw_dom = curator_sources.verify_and_extract_candidate(
        {"title": "WSJ Piece", "url": "https://www.wsj.com/articles/test-123"}
    )
    assert not res_pw_dom["verified"] and "paywall_domain" in (res_pw_dom["rejection_reason"] or "")

    # B. HTTP 404
    mock_404 = MagicMock(status_code=404, url="https://open.example.com/404", headers={})
    with patch("curator_sources.requests.get", return_value=mock_404):
        res_404 = curator_sources.verify_and_extract_candidate(
            {"title": "Dead Link", "url": "https://open.example.com/404"}
        )
    assert not res_404["verified"] and res_404["rejection_reason"] == "http_404"

    # C. Paywall text in short body
    mock_pw_html = MagicMock(
        status_code=200,
        url="https://open.example.com/member",
        headers={"content-type": "text/html"},
        apparent_encoding="utf-8",
        encoding="utf-8",
        text="<html><body><article><p>Subscribe to continue reading this exclusive article.</p></article></body></html>",
    )
    with patch("curator_sources.requests.get", return_value=mock_pw_html):
        res_pw = curator_sources.verify_and_extract_candidate(
            {"title": "Locked", "url": "https://open.example.com/member"}
        )
    assert not res_pw["verified"] and "paywall" in (res_pw["rejection_reason"] or "")

    # D. Short body (< 800 chars) rejected for regular web article
    short_html = "<html><body><article><p>" + ("Short paragraph. " * 10) + "</p></article></body></html>"
    mock_short = MagicMock(
        status_code=200,
        url="https://open.example.com/short",
        headers={"content-type": "text/html"},
        apparent_encoding="utf-8",
        encoding="utf-8",
        text=short_html,
    )
    with patch("curator_sources.requests.get", return_value=mock_short):
        res_short = curator_sources.verify_and_extract_candidate(
            {"title": "Short", "url": "https://open.example.com/short"}
        )
    assert not res_short["verified"] and "content_too_short" in (res_short["rejection_reason"] or "")

    # E. Academic abstract exception (allow_abstract_only=True with >= 250 chars abstract)
    with patch("curator_sources.requests.get", return_value=mock_short):
        res_acad = curator_sources.verify_and_extract_candidate(
            {
                "title": "NBER Paper",
                "url": "https://www.nber.org/papers/w99999",
                "allow_abstract_only": True,
                "abstract": "This empirical study investigates organizational incentives across 500 firms. " * 6,
            }
        )
    assert res_acad["verified"] and res_acad["access_type"] == "open_abstract"
    print("  PASS")


def test_instant_enqueue_and_feedback_and_api():
    print("[Test 5 & 6 & 7] Instant 0s enqueue, feedback persistence, and FastAPI endpoints...")
    tmp_dir = Path(tempfile.mkdtemp(prefix="readout_curator_test_"))
    try:
        orig_c_dir = curator_engine.CURATOR_DIR
        orig_ca_dir = curator_engine.CURATOR_AUDIO_DIR
        orig_h_file = curator_engine.HISTORY_FILE
        orig_out_dir = curator_engine.OUTPUT_DIR
        orig_q_file = queue_manager.QUEUE_FILE
        orig_q_out = queue_manager.OUTPUT_DIR
        orig_main_out = main.OUTPUT_DIR

        curator_engine.CURATOR_DIR = tmp_dir / "curator"
        curator_engine.CURATOR_AUDIO_DIR = curator_engine.CURATOR_DIR / "audio"
        curator_engine.HISTORY_FILE = curator_engine.CURATOR_DIR / "history.json"
        curator_engine.OUTPUT_DIR = tmp_dir / "output"
        queue_manager.QUEUE_FILE = tmp_dir / "queue.json"
        queue_manager.OUTPUT_DIR = tmp_dir / "output"
        main.OUTPUT_DIR = tmp_dir / "output"
        curator_engine.ensure_dirs()

        test_date = "2026-11-26"
        mp3_name = f"{test_date}_part1_core.mp3"
        # Create a dummy non-empty MP3 file
        (curator_engine.CURATOR_AUDIO_DIR / mp3_name).write_bytes(b"ID3" + b"\x00" * 1024)

        daily_doc = {
            "date": test_date,
            "generated_at": "2026-11-26T03:00:00+09:00",
            "shortfall_warning": None,
            "parts_total": 1,
            "parts_ready": 1,
            "parts": [
                {
                    "part_id": "part1_core",
                    "part_type": "core",
                    "part_label": "Part 1: Core Deep Dive",
                    "title": "組織の意思決定バイアスを測る新手法",
                    "why_selected": "実証データに基づく高い説明力",
                    "source": {
                        "original_title": "Measuring Decision Bias",
                        "source_name": "Knowledge at Wharton",
                        "author": "Test Author",
                        "published_at": "2026-11-20",
                        "url": "https://knowledge.wharton.upenn.edu/article/test-bias/",
                        "access_type": "free_full_text",
                        "lang": "en",
                    },
                    "scores": {k: 4.5 for k in curator_engine.SEVEN_AXIS_WEIGHTS},
                    "weighted_score": 4.5,
                    "interest_fit": 4.8,
                    "novelty": 4.0,
                    "estimated_minutes": 12,
                    "script_chars": 4000,
                    "script_text": "これはテスト用の音声解説原稿です。",
                    "audio_filename": mp3_name,
                    "audio_status": "ready",
                    "audio_duration_sec": 720.0,
                    "enqueued": False,
                    "queue_item_id": None,
                    "feedback": None,
                }
            ],
        }
        (curator_engine.CURATOR_DIR / f"daily_{test_date}.json").write_text(
            json.dumps(daily_doc, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Test API with auth temporarily bypassed if configured
        orig_u, orig_p = main.READOUT_USERNAME, main.READOUT_PASSWORD
        main.READOUT_USERNAME, main.READOUT_PASSWORD = None, None
        try:
            client = TestClient(main.app)

            r_dates = client.get("/api/curator/dates")
            assert r_dates.status_code == 200
            assert test_date in r_dates.json()["dates"]

            r_daily = client.get(f"/api/curator/daily?date={test_date}")
            assert r_daily.status_code == 200
            assert r_daily.json()["daily"]["parts"][0]["part_id"] == "part1_core"

            # Zero-wait instant enqueue
            r_enq = client.post(
                "/api/curator/enqueue",
                json={"date": test_date, "part_id": "part1_core"},
            )
            assert r_enq.status_code == 200
            enq_data = r_enq.json()
            assert enq_data["instant_ready"] is True
            assert enq_data["item"]["status"] == "ready"
            assert (curator_engine.OUTPUT_DIR / f"{enq_data['item']['id']}.mp3").exists()

            # 5-level feedback + reason chips + memo
            r_fb = client.post(
                "/api/curator/feedback",
                json={
                    "date": test_date,
                    "part_id": "part1_core",
                    "rating": "非常に面白かった",
                    "reasons": ["視点が新鮮", "根拠が強い"],
                    "memo": "意思決定の定量化が実務に効く",
                },
            )
            assert r_fb.status_code == 200
            fb_data = r_fb.json()
            assert fb_data["feedback"]["rating"] == 5
            assert "視点が新鮮" in fb_data["core_preference_summary"]

            # Verify persisted in history.json
            hist = curator_engine.load_history()
            assert len(hist["feedback"]) == 1
            assert hist["feedback"][0]["rating_label"] == "非常に面白かった"
        finally:
            main.READOUT_USERNAME, main.READOUT_PASSWORD = orig_u, orig_p
            curator_engine.CURATOR_DIR = orig_c_dir
            curator_engine.CURATOR_AUDIO_DIR = orig_ca_dir
            curator_engine.HISTORY_FILE = orig_h_file
            curator_engine.OUTPUT_DIR = orig_out_dir
            queue_manager.QUEUE_FILE = orig_q_file
            queue_manager.OUTPUT_DIR = orig_q_out
            main.OUTPUT_DIR = orig_main_out
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print("  PASS")


if __name__ == "__main__":
    test_seven_axis_weighted_score()
    test_slot_based_selection()
    test_wildcard_anti_overfitting()
    test_paywall_and_verification_filters()
    test_instant_enqueue_and_feedback_and_api()
    print("\n=== ALL CURATOR VERIFICATION TESTS PASSED ===")
