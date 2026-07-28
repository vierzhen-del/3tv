"""main.py의 순수 헬퍼 함수 테스트 — 캡처·전송 등 실제 부작용이 있는 run_*()
오케스트레이션은 VOD 재실행으로 검증한다(README/skill 참고).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from threetv import main as main_mod

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
    out = main_mod._slot_settings(SETTINGS, "22:00", 5)
    assert out["sessions"]["night"]["start_kst"] == "22:00"
    assert out["sessions"]["night"]["end_kst"] == "22:05"
    # 원본은 그대로 (deepcopy 확인 — 다음 슬롯이 이전 슬롯 값을 안 물려받아야 함)
    assert SETTINGS["sessions"]["night"]["start_kst"] == "22:00"
    assert SETTINGS["sessions"]["night"]["end_kst"] == "06:00"


def test_slot_settings_handles_hour_rollover():
    """05:58 슬롯 + 5분 = 06:03처럼 시간이 넘어가도 정상 계산된다."""
    out = main_mod._slot_settings(SETTINGS, "05:58", 5)
    assert out["sessions"]["night"]["end_kst"] == "06:03"


def test_slot_settings_midnight_boundary():
    """23:58 슬롯 + 5분은 자정을 넘어 00:03이 된다."""
    out = main_mod._slot_settings(SETTINGS, "23:58", 5)
    assert out["sessions"]["night"]["end_kst"] == "00:03"


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
