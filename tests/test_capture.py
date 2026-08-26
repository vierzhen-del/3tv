"""noon 세션 VOD 폴백(2026-08-01)의 핵심 — capture.find_recent_vod().

라이브 캡처(wait_for_live/record_stream)는 실제 네트워크·ffmpeg가 필요해
VOD 재실행으로만 검증하지만(README/skill 참고), find_recent_vod()는 yt-dlp
서브프로세스 호출 두 번(목록 조회 → 상세 조회)을 순수하게 조합하는 로직이라
subprocess.run을 모킹해 단위테스트할 수 있다.
"""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from threetv import capture


def _run_result(stdout: str, returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def test_find_recent_vod_picks_latest_entry_and_release_ts(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--flat-playlist" in cmd:
            return _run_result(json.dumps({
                "entries": [{"id": "abc123"}, {"id": "older1"}],
            }))
        return _run_result(json.dumps({"release_timestamp": 1735707600}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(capture, "_cookies_file", lambda: None)

    url, ts = capture.find_recent_vod("https://www.youtube.com/@gyeomsonisnothing/live")
    assert url == "https://www.youtube.com/watch?v=abc123"
    assert ts == 1735707600
    assert len(calls) == 2
    assert calls[0][-1].endswith("/@gyeomsonisnothing/streams")


def test_find_recent_vod_missing_release_ts_returns_none(monkeypatch):
    def fake_run(cmd, **kwargs):
        if "--flat-playlist" in cmd:
            return _run_result(json.dumps({"entries": [{"id": "xyz"}]}))
        return _run_result(json.dumps({}))   # release_timestamp 없음(비공개 처리 등)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(capture, "_cookies_file", lambda: None)

    url, ts = capture.find_recent_vod("https://www.youtube.com/@gyeomsonisnothing/live")
    assert url == "https://www.youtube.com/watch?v=xyz"
    assert ts is None


def test_find_recent_vod_empty_list_raises(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _run_result(json.dumps({"entries": []}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(capture, "_cookies_file", lambda: None)

    with pytest.raises(capture.CaptureError):
        capture.find_recent_vod("https://www.youtube.com/@gyeomsonisnothing/live")


def test_find_recent_vod_detail_call_failure_still_returns_url(monkeypatch):
    """release_timestamp를 못 구해도(두 번째 호출 실패) 영상 URL은 그대로 준다 —
    호출 측이 오프셋 0 + 여유시간 전략으로 대체할 수 있게."""
    def fake_run(cmd, **kwargs):
        if "--flat-playlist" in cmd:
            return _run_result(json.dumps({"entries": [{"id": "xyz"}]}))
        raise subprocess.TimeoutExpired(cmd, 60)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(capture, "_cookies_file", lambda: None)

    url, ts = capture.find_recent_vod("https://www.youtube.com/@gyeomsonisnothing/live")
    assert url == "https://www.youtube.com/watch?v=xyz"
    assert ts is None


def test_find_recent_vod_list_call_failure_raises(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="차단됨")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(capture, "_cookies_file", lambda: None)

    with pytest.raises(capture.CaptureError):
        capture.find_recent_vod("https://www.youtube.com/@gyeomsonisnothing/live")


# ───────────────────── download_vod 타임아웃 재시도 ─────────────────────

def test_download_vod_success_no_timeout(tmp_path, monkeypatch):
    out_file = tmp_path / "out.mp4"

    def fake_run(cmd, **kwargs):
        out_file.write_bytes(b"0" * 1000)
        return _run_result("", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(capture, "_cookies_file", lambda: None)

    result = capture.download_vod("https://y/v", out_file, 480, 269, 2700)
    assert result == out_file


def test_download_vod_trimmed_timeout_falls_back_to_full(tmp_path, monkeypatch):
    """2026-08-27 실측: 방금 올라온 VOD는 --download-sections가 30분 안에
    안 끝났다(us-session run #50). 트리밍이 타임아웃되면 트리밍 없이
    전체 다운로드를 자동으로 1회 더 시도해야 한다."""
    out_file = tmp_path / "out.mp4"
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--download-sections" in cmd:
            raise subprocess.TimeoutExpired(cmd, 1800)
        out_file.write_bytes(b"0" * 1000)
        return _run_result("", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(capture, "_cookies_file", lambda: None)

    result = capture.download_vod("https://y/v", out_file, 480, 269, 2700)
    assert result == out_file
    assert len(calls) == 2
    assert "--download-sections" in calls[0] and "--download-sections" not in calls[1]


def test_download_vod_both_attempts_timeout_raises(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1800)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(capture, "_cookies_file", lambda: None)

    with pytest.raises(capture.CaptureError):
        capture.download_vod("https://y/v", tmp_path / "out.mp4", 480, 269, 2700)


def test_download_vod_untrimmed_timeout_raises_without_retry(tmp_path, monkeypatch):
    """트리밍 없이(start_sec/duration_sec 없이) 호출됐는데 타임아웃되면
    재시도할 게 없다 — 바로 실패해야 한다(무한 재시도 방지)."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, 1800)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(capture, "_cookies_file", lambda: None)

    with pytest.raises(capture.CaptureError):
        capture.download_vod("https://y/v", tmp_path / "out.mp4", 480)
    assert len(calls) == 1
