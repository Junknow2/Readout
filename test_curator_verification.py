#!/usr/bin/env python3
"""
Automated Verification Suite for Readout Personal Knowledge Curator.
Tests:
  1. 7-axis weighted score calculation
  2. Slot-based selection (Core / Discovery / Wild Card distinct from naive top-N)
  3. Anti-overfitting guarantee (Wild Card prompt never receives preference profile)
  4. Mechanical paywall / 404 / short-body exclusion + academic abstract exception
  5. Katakana acronym safety-net sanitizer & katakana rules in all script prompts
  6. VOICEPEAK per-chunk retry recovery (code 3221225477 / code 1) & tts_engine return
  7. 3-part curation structure (no Part 4 Connections) + legacy part4 backward compatibility
  8. Zero-wait instant enqueue (with tts_engine propagation), 5-level feedback + memo, and FastAPI endpoints
"""

from __future__ import annotations

import asyncio
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
import poc_pipeline
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


def test_katakana_sanitizer_and_prompts():
    print("[Test 5] Katakana acronym safety-net sanitizer & katakana rules in all prompts...")
    sample = "最新のAIとLLMがROIやKPIを改善し、MITやRIETI、P&G、PANAS、HBRの知見も活かされる。"
    sanitized = poc_pipeline.sanitize_script_for_tts(sample)
    for expected in ("エーアイ", "エルエルエム", "アールオーアイ", "ケーピーアイ", "エムアイティー", "リエティ", "ピーアンドジー", "パナス", "エイチビーアール"):
        assert expected in sanitized, f"Expected '{expected}' in sanitized text: {sanitized}"
    for forbidden in ("AI", "LLM", "ROI", "KPI", "MIT", "RIETI", "P&G", "PANAS", "HBR"):
        assert forbidden not in sanitized, f"Unexpected raw acronym '{forbidden}' remained: {sanitized}"

    # Ensure word boundary protection (do not corrupt substrings inside longer tokens)
    safe_word = poc_pipeline.sanitize_script_for_tts("EMAIL と MAIN はそのまま")
    assert "EMAIL" in safe_word and "MAIN" in safe_word, f"Word boundary broken: {safe_word}"

    # Verify katakana instruction exists in Web/PDF rewrite prompts
    assert "カタカナ" in poc_pipeline.PODCAST_REWRITE_PROMPT
    assert "カタカナ" in poc_pipeline.PDF_REWRITE_PROMPT

    # Verify Part 4 Connections generator is removed
    assert not hasattr(curator_engine, "_generate_connections_script"), "_generate_connections_script should be removed"
    print("  PASS")


def test_voicepeak_chunk_retry_and_engine_return():
    print("[Test 6] VOICEPEAK per-chunk retry (code 3221225477 / code 1) & tts_engine return...")
    tmp_dir = Path(tempfile.mkdtemp(prefix="readout_vp_retry_test_"))
    try:
        out_mp3 = tmp_dir / "out.mp3"
        fake_vp_exe = tmp_dir / "voicepeak.exe"
        fake_vp_exe.write_bytes(b"fake")

        vp_calls = []

        def fake_subprocess_run(cmd, capture_output=True, text=True, check=False):
            exe = str(cmd[0]).lower()
            if "voicepeak" in exe:
                vp_calls.append(cmd)
                attempt_no = len(vp_calls)
                out_wav = Path(cmd[cmd.index("-o") + 1])
                if attempt_no == 1:
                    return MagicMock(returncode=3221225477, stdout="", stderr="Access Violation")
                if attempt_no == 2:
                    return MagicMock(returncode=1, stdout="", stderr="Another instance is running")
                out_wav.write_bytes(b"RIFF" + b"\x00" * 256)
                return MagicMock(returncode=0, stdout="", stderr="")
            else:
                # ffmpeg conversion
                Path(cmd[-1]).write_bytes(b"ID3" + b"\x00" * 512)
                return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch.object(poc_pipeline, "VOICEPEAK_PATH", fake_vp_exe),
            patch.object(poc_pipeline, "VOICEPEAK_RETRY_BACKOFFS", (0.01, 0.01)),
            patch.object(poc_pipeline, "VOICEPEAK_CHUNK_COOLDOWN_SEC", 0.0),
            patch("poc_pipeline.subprocess.run", side_effect=fake_subprocess_run),
        ):
            used_engine = asyncio.run(
                poc_pipeline.generate_speech_async("これはリトライ検証用のテスト原稿です。", out_mp3)
            )
            assert used_engine == "voicepeak", f"Expected 'voicepeak', got '{used_engine}'"
            assert len(vp_calls) == 3, f"Expected 3 attempts (2 failures + 1 success), got {len(vp_calls)}"
            assert out_mp3.exists() and out_mp3.stat().st_size > 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print("  PASS")


def test_instant_enqueue_and_feedback_and_api():
    print("[Test 7 & 8] 3-part structure, legacy part4 compat, instant 0s enqueue (tts_engine), feedback+memo, FastAPI...")
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

        # Include a legacy part4_connections record to verify backward compatibility doesn't crash API
        daily_doc = {
            "date": test_date,
            "generated_at": "2026-11-26T03:00:00+09:00",
            "shortfall_warning": None,
            "parts_total": 2,
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
                    "tts_engine": "voicepeak",
                    "enqueued": False,
                    "queue_item_id": None,
                    "feedback": None,
                },
                {
                    "part_id": "part4_connections",
                    "part_type": "connections",
                    "part_label": "Part 4: Connections",
                    "title": "過去ログ互換テスト",
                    "why_selected": "旧フォーマットの互換確認",
                    "source": {"source_name": "Legacy", "url": "", "access_type": "synthesis"},
                    "scores": {},
                    "weighted_score": None,
                    "estimated_minutes": 3,
                    "script_chars": 800,
                    "script_text": "過去ログのテスト。",
                    "audio_filename": f"{test_date}_part4_connections.mp3",
                    "audio_status": "script_only",
                    "audio_duration_sec": 0.0,
                    "enqueued": False,
                    "queue_item_id": None,
                    "feedback": None,
                },
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
            assert r_daily.json()["daily"]["parts"][0]["tts_engine"] == "voicepeak"
            # Legacy part4 still returned cleanly
            assert r_daily.json()["daily"]["parts"][1]["part_id"] == "part4_connections"

            # Zero-wait instant enqueue (propagates tts_engine)
            r_enq = client.post(
                "/api/curator/enqueue",
                json={"date": test_date, "part_id": "part1_core"},
            )
            assert r_enq.status_code == 200
            enq_data = r_enq.json()
            assert enq_data["instant_ready"] is True
            assert enq_data["item"]["status"] == "ready"
            assert enq_data["item"]["tts_engine"] == "voicepeak"
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
            assert "意思決定の定量化が実務に効く" in fb_data["core_preference_summary"]

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
    test_katakana_sanitizer_and_prompts()
    test_voicepeak_chunk_retry_and_engine_return()
    test_instant_enqueue_and_feedback_and_api()
    print("\n=== ALL CURATOR VERIFICATION TESTS PASSED ===")
