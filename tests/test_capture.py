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


# ───────────────────── download_vod 타임아웃 처리 ─────────────────────
# 2026-08-27: 트리밍 타임아웃 시 전체 재다운로드를 자동 재시도하는 로직이
# 있었으나, 같은 날 전체 다운로드도 동일하게 타임아웃돼(run #51) 재시도가
# 문제를 우회하지 못한다는 게 실측됐다. 재시도는 없앴고, 대신 타임아웃
# 시 진행률 로그를 남기는 것만 검증한다.

def test_download_vod_success_no_timeout(tmp_path, monkeypatch):
    out_file = tmp_path / "out.mp4"

    def fake_run(cmd, **kwargs):
        out_file.write_bytes(b"0" * 1000)
        return _run_result("", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(capture, "_cookies_file", lambda: None)

    result = capture.download_vod("https://y/v", out_file, 480, 269, 2700)
    assert result == out_file


def test_download_vod_omits_quiet_but_keeps_no_warnings(tmp_path, monkeypatch):
    """--quiet가 있으면 타임아웃 시 진행률이 전혀 안 남는다(2026-08-27 실측
    — run #51 로그가 비어 있었음) — --newline으로 대체해 진행률을 남긴다."""
    out_file = tmp_path / "out.mp4"
    seen_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        seen_cmd.extend(cmd)
        out_file.write_bytes(b"0" * 1000)
        return _run_result("", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(capture, "_cookies_file", lambda: None)

    capture.download_vod("https://y/v", out_file, 480)
    assert "--quiet" not in seen_cmd
    assert "--newline" in seen_cmd
    assert "--no-warnings" in seen_cmd


def test_download_vod_timeout_raises_with_progress_in_message(tmp_path, monkeypatch):
    """재시도는 없다 — 타임아웃되면 바로 실패하되, 잡힌 진행률(stdout)을
    에러 메시지에 담아 다음 진단이 가능하게 한다."""
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd, 3600, output="[download]  42.0% of 500MiB at 1.20MiB/s\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(capture, "_cookies_file", lambda: None)

    with pytest.raises(capture.CaptureError, match="42.0%"):
        capture.download_vod("https://y/v", tmp_path / "out.mp4", 480, 269, 2700)


def test_download_vod_timeout_no_progress_still_raises_cleanly(tmp_path, monkeypatch):
    """진행률 로그가 하나도 안 잡혀도(run #51 실측 사례) 에러 없이 CaptureError로
    깔끔하게 떨어져야 한다."""
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 3600)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(capture, "_cookies_file", lambda: None)

    with pytest.raises(capture.CaptureError, match="진행률 로그 없음"):
        capture.download_vod("https://y/v", tmp_path / "out.mp4", 480)
