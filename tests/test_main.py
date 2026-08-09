"""main.py의 순수 헬퍼 함수 테스트 — 캡처·전송 등 실제 부작용이 있는 run_*()
오케스트레이션은 VOD 재실행으로 검증한다(README/skill 참고).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from threetv import main as main_mod
from threetv.common import parse_kst_time

KST = ZoneInfo("Asia/Seoul")

SETTINGS = {
    "sessions": {
        "night": {
            "poll_from_kst": "22:00", "start_kst": "22:00", "end_kst": "06:00",
            "slot_duration_min": 5,
        },
    },
}


def test_night_session_date_late_hours_roll_to_next_day():
    """22/23시 슬롯은 아직 '어제'지만, digest가 부르는 '오늘' 날짜로 묶여야 한다."""
    base = datetime(2026, 7, 28, 22, 30, tzinfo=KST)   # 7/28 밤
    assert main_mod.night_session_date(22, base) == "20260729"
    assert main_mod.night_session_date(23, base) == "20260729"


def test_night_session_date_early_hours_stay_same_day():
    """00~05시 슬롯과 06시 digest는 이미 '오늘' 날짜라 그대로 쓴다."""
    base = datetime(2026, 7, 29, 2, 15, tzinfo=KST)    # 7/29 새벽
    assert main_mod.night_session_date(0, base) == "20260729"
    assert main_mod.night_session_date(5, base) == "20260729"
    assert main_mod.night_session_date(6, base) == "20260729"


def test_night_session_date_all_8_slots_plus_digest_agree():
    """8슬롯 + digest가 각자 다른 시각에 실행돼도 전부 같은 날짜로 계산돼야 한다."""
    # 22/23시는 7/28 밤에 실행, 00~06시는 7/29 새벽에 실행 — 서로 다른 실제 날짜
    late = [(22, datetime(2026, 7, 28, 22, 5, tzinfo=KST)),
            (23, datetime(2026, 7, 28, 23, 5, tzinfo=KST))]
    early = [(h, datetime(2026, 7, 29, h, 5, tzinfo=KST)) for h in range(6)]
    dates = {main_mod.night_session_date(h, base) for h, base in late + early}
    assert dates == {"20260729"}


def test_slot_settings_overrides_start_end_without_mutating_original():
    """정시 전(=지연 없음)이면 기존처럼 슬롯 정시 그대로 쓴다."""
    now = datetime(2026, 7, 28, 21, 55, tzinfo=KST)
    out = main_mod._slot_settings(SETTINGS, "22:00", 5, now)
    assert out["sessions"]["night"]["start_kst"] == "22:00"
    assert out["sessions"]["night"]["end_kst"] == "22:05"
    # 원본은 그대로 (deepcopy 확인 — 다음 슬롯이 이전 슬롯 값을 안 물려받아야 함)
    assert SETTINGS["sessions"]["night"]["start_kst"] == "22:00"
    assert SETTINGS["sessions"]["night"]["end_kst"] == "06:00"


def test_slot_settings_handles_hour_rollover():
    """05:58 슬롯 + 5분 = 06:03처럼 시간이 넘어가도 정상 계산된다."""
    now = datetime(2026, 7, 29, 5, 50, tzinfo=KST)
    out = main_mod._slot_settings(SETTINGS, "05:58", 5, now)
    assert out["sessions"]["night"]["end_kst"] == "06:03"


def test_slot_settings_midnight_boundary():
    """23:58 슬롯 + 5분은 자정을 넘어 00:03이 된다."""
    now = datetime(2026, 7, 28, 23, 50, tzinfo=KST)
    out = main_mod._slot_settings(SETTINGS, "23:58", 5, now)
    assert out["sessions"]["night"]["end_kst"] == "00:03"


# ── 2026-08-01 cron 지연 내성 (A-2) ─────────────────────────────────────

def test_slot_settings_on_time_uses_nominal_slot():
    """정시 전에 실행되면(=지연 없음) 종전처럼 슬롯 정시부터 캡처한다."""
    now = datetime(2026, 7, 28, 21, 59, tzinfo=KST)   # 22:00 슬롯 1분 전
    out = main_mod._slot_settings(SETTINGS, "22:00", 5, now)
    assert out["sessions"]["night"]["start_kst"] == "22:00"
    assert out["sessions"]["night"]["end_kst"] == "22:05"


def test_slot_settings_late_start_captures_from_now():
    """cron이 밀려 슬롯 정시가 이미 지났으면 '정시부터'가 아니라 '지금부터'
    duration_min만 캡처한다 — 안 그러면 record_stream()이 곧바로
    '녹화 종료 시각이 이미 지남'으로 실패한다(실전 8슬롯 전부 이렇게 죽었다)."""
    now = datetime(2026, 7, 28, 22, 47, tzinfo=KST)   # 22:00 슬롯인데 47분 지연
    out = main_mod._slot_settings(SETTINGS, "22:00", 5, now)
    assert out["sessions"]["night"]["start_kst"] == "22:47"
    assert out["sessions"]["night"]["end_kst"] == "22:52"


def test_slot_settings_late_start_clamped_to_broadcast_end():
    """방송 종료(06:00) 직전까지 밀렸으면 duration_min을 다 못 채우고
    방송 종료 시각에서 잘라야 한다(방송 끝난 시간대를 캡처하면 안 됨)."""
    now = datetime(2026, 7, 29, 5, 58, tzinfo=KST)    # 05:00 슬롯, 06:00 방송종료 2분 전
    settings = {"sessions": {"night": {**SETTINGS["sessions"]["night"]}}}
    out = main_mod._slot_settings(settings, "05:00", 5, now)
    assert out["sessions"]["night"]["start_kst"] == "05:58"
    assert out["sessions"]["night"]["end_kst"] == "06:00"   # 06:03이 아니라 06:00에서 잘림


def test_slot_nominal_dt_rolls_back_a_day_after_midnight():
    """22/23시 슬롯이 자정을 넘겨 실행되면(=now가 이미 다음날) 슬롯은 '어제'
    날짜로 되돌려야 한다 — night_session_date()의 hour>=12 규칙과 대칭."""
    now = datetime(2026, 7, 29, 1, 38, tzinfo=KST)    # 22:00 슬롯이 3h38m 밀려 다음날 새벽에 실행
    nominal = main_mod._slot_nominal_dt("22:00", now)
    assert nominal == datetime(2026, 7, 28, 22, 0, tzinfo=KST)


def test_broadcast_end_kst_for_evening_slot_is_next_morning():
    nominal = datetime(2026, 7, 28, 22, 0, tzinfo=KST)
    end = main_mod._broadcast_end_kst(SETTINGS["sessions"]["night"], nominal)
    assert end == datetime(2026, 7, 29, 6, 0, tzinfo=KST)


def test_broadcast_end_kst_for_early_morning_slot_is_same_day():
    nominal = datetime(2026, 7, 29, 2, 0, tzinfo=KST)
    end = main_mod._broadcast_end_kst(SETTINGS["sessions"]["night"], nominal)
    assert end == datetime(2026, 7, 29, 6, 0, tzinfo=KST)


# ── VOD 폴백 (2026-08-01 noon에서 시작, 2026-08-09 us/kr로 일반화) ──────────

def _noon_cfg():
    return {
        "live_url": "https://www.youtube.com/@gyeomsonisnothing/live",
        "start_kst": "12:00", "end_kst": "12:20",
    }


def _kr_settings():
    """us/kr은 자체 live_url이 없어 channel.live_url로 폴백한다 — 그 경로도 함께 확인."""
    return {
        "capture": {"resolution": 480},
        "channel": {"live_url": "https://www.youtube.com/@3protv/live"},
        "sessions": {"kr": {"start_kst": "07:45", "end_kst": "08:25"}},
    }


def test_vod_fallback_uses_channel_live_url_when_session_has_none(monkeypatch, tmp_path):
    """kr 세션 설정엔 live_url이 없다 — channel.live_url로 폴백해야 한다."""
    seen_url = {}

    def fake_find(live_url):
        seen_url["v"] = live_url
        return ("https://y/w", None)
    monkeypatch.setattr(main_mod, "find_recent_vod", fake_find)
    monkeypatch.setattr(main_mod, "download_vod", lambda *a, **k: a[1])

    main_mod._vod_fallback(_kr_settings(), "kr", tmp_path)
    assert seen_url["v"] == "https://www.youtube.com/@3protv/live"


def test_vod_fallback_kr_offset_from_release_ts(monkeypatch, tmp_path):
    """kr(07:45~08:25)도 noon과 동일한 오프셋 계산이 적용된다."""
    target = parse_kst_time("07:45")
    release_dt = target - timedelta(minutes=5)
    monkeypatch.setattr(main_mod, "find_recent_vod",
                        lambda live_url: ("https://y/watch?v=kr", int(release_dt.timestamp())))
    captured = {}

    def fake_download(url, out_file, resolution, start_sec, duration_sec):
        captured.update(start_sec=start_sec, duration_sec=duration_sec)
        return out_file
    monkeypatch.setattr(main_mod, "download_vod", fake_download)

    main_mod._vod_fallback(_kr_settings(), "kr", tmp_path)
    assert captured["start_sec"] == 5 * 60          # 07:45 - (07:40 시작) = 5분 오프셋
    assert captured["duration_sec"] == 40 * 60       # 07:45~08:25 구간 길이


def test_noon_vod_fallback_computes_offset_from_release_ts(monkeypatch, tmp_path):
    """방송이 12:00보다 2분30초 일찍 시작했으면 그만큼 건너뛰고 받는다."""
    target = parse_kst_time("12:00")
    release_dt = target - timedelta(minutes=2, seconds=30)
    monkeypatch.setattr(main_mod, "find_recent_vod",
                        lambda live_url: ("https://y/watch?v=abc", int(release_dt.timestamp())))
    captured = {}

    def fake_download(url, out_file, resolution, start_sec, duration_sec):
        captured.update(url=url, start_sec=start_sec, duration_sec=duration_sec)
        return out_file
    monkeypatch.setattr(main_mod, "download_vod", fake_download)

    settings = {"capture": {"resolution": 480}, "sessions": {"noon": _noon_cfg()}}
    main_mod._vod_fallback(settings, "noon", tmp_path)
    assert captured["url"] == "https://y/watch?v=abc"
    assert captured["start_sec"] == 150
    assert captured["duration_sec"] == 20 * 60


def test_noon_vod_fallback_clamps_negative_offset_to_zero():
    """방송이 12:00 이후에 시작한 것으로 나오면(비정상) 오프셋은 0으로 클램프."""
    target = parse_kst_time("12:00")
    release_dt = target + timedelta(minutes=5)
    assert max(0, int((target - release_dt).total_seconds())) == 0


def test_noon_vod_fallback_missing_release_ts_uses_default_window(monkeypatch, tmp_path):
    """release_timestamp를 못 구하면 12:00 정각 시작 가정 + 25분 여유로 받는다."""
    monkeypatch.setattr(main_mod, "find_recent_vod",
                        lambda live_url: ("https://y/watch?v=xyz", None))
    captured = {}

    def fake_download(url, out_file, resolution, start_sec, duration_sec):
        captured.update(start_sec=start_sec, duration_sec=duration_sec)
        return out_file
    monkeypatch.setattr(main_mod, "download_vod", fake_download)

    settings = {"capture": {"resolution": 480}, "sessions": {"noon": _noon_cfg()}}
    main_mod._vod_fallback(settings, "noon", tmp_path)
    assert captured["start_sec"] == 0
    assert captured["duration_sec"] == 25 * 60


def test_parse_args_night_requires_slot_or_digest():
    import argparse
    import pytest
    import sys

    orig_argv = sys.argv
    try:
        sys.argv = ["main.py", "--session", "night"]
        with pytest.raises(SystemExit):
            main_mod.parse_args()
    finally:
        sys.argv = orig_argv


def test_parse_args_night_rejects_both_slot_and_digest():
    import pytest
    import sys

    orig_argv = sys.argv
    try:
        sys.argv = ["main.py", "--session", "night", "--slot", "22:00", "--digest"]
        with pytest.raises(SystemExit):
            main_mod.parse_args()
    finally:
        sys.argv = orig_argv


def test_parse_args_slot_rejected_for_other_sessions():
    import pytest
    import sys

    orig_argv = sys.argv
    try:
        sys.argv = ["main.py", "--session", "us", "--slot", "22:00"]
        with pytest.raises(SystemExit):
            main_mod.parse_args()
    finally:
        sys.argv = orig_argv


def test_parse_args_night_digest_ok():
    import sys

    orig_argv = sys.argv
    try:
        sys.argv = ["main.py", "--session", "night", "--digest"]
        args = main_mod.parse_args()
        assert args.session == "night" and args.digest is True and args.slot is None
    finally:
        sys.argv = orig_argv


# ── 저장위치 링크 + 리포트 꼬리말 (2026-08-02) ─────────────────────────────
# obsidian:// 딥링크가 탭S9에서 열리지 않아 「저장위치」 https 링크로 바꿨다.
OBS_CFG = {
    "vault_name": "vierzhen_home",
    "vault_repo": "vierzhen-del/3tv-reports",
    "vault_branch": "main",
}
REL = "3protv/2026/08/3protv오늘_20260802_반도체.md"


def test_vault_location_link_points_at_relay_repo_file():
    from threetv.obsidian_archive import vault_location_link
    link = vault_location_link(OBS_CFG, REL)
    # 링크 글자는 볼트 안 실제 경로 — 딥링크가 안 열려도 위치를 눈으로 확인할 수 있다
    assert link.startswith("🗂 저장위치: [vierzhen_home/3protv/2026/08/")
    assert "https://github.com/vierzhen-del/3tv-reports/blob/main/3protv/2026/08/" in link
    # 경로 구분자가 %2F로 인코딩되면 GitHub URL이 깨진다
    assert "%2F" not in link


def test_vault_location_link_without_repo_is_plain_path():
    from threetv.obsidian_archive import vault_location_link
    link = vault_location_link({"vault_name": "vierzhen_home"}, REL)
    assert link == f"🗂 저장위치: vierzhen_home/{REL}"
    assert "http" not in link


def test_vault_location_link_empty_when_path_unknown():
    from threetv.obsidian_archive import vault_location_link
    assert vault_location_link(OBS_CFG, "") == ""


def test_report_footer_has_location_and_generated_time():
    from threetv.obsidian_archive import ArchiveResult
    footer = main_mod.report_footer({"obsidian": OBS_CFG},
                                    ArchiveResult(True, False, "", REL))
    assert "🗂 저장위치: [" in footer
    assert "🕘 생성 " in footer and " KST" in footer
    assert "옵시디안에서 열기" not in footer


def test_report_footer_keeps_time_when_archive_failed():
    """저장이 실패하면 위치 줄은 빼되 생성시각은 남긴다 — 죽은 링크를 붙이지 않는다."""
    from threetv.obsidian_archive import ArchiveResult
    footer = main_mod.report_footer({"obsidian": OBS_CFG},
                                    ArchiveResult(False, False, "push 실패", REL))
    assert "저장위치" not in footer
    assert "🕘 생성 " in footer
