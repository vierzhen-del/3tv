"""공통 유틸 단위 테스트 — API 키 없이 실행 가능."""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from threetv.common import is_kr_holiday, output_dir, parse_duration

KST = ZoneInfo("Asia/Seoul")


def test_parse_duration_formats():
    assert parse_duration("30") == 30
    assert parse_duration("6:00") == 360
    assert parse_duration("5:30") == 330
    assert parse_duration("1:02:03") == 3723
    assert parse_duration(" 0:09 ") == 9


def test_output_dir_tag_separates_trim_from_full(tmp_path, monkeypatch):
    import threetv.common as common
    monkeypatch.setattr(common, "OUTPUT_ROOT", tmp_path)

    full = output_dir("us")
    trimmed = output_dir("us", tag="trim")

    assert full != trimmed
    assert full.name == "us"
    assert trimmed.name == "us_trim"
    assert full.parent == trimmed.parent  # 같은 날짜 폴더 아래 나란히


def test_is_kr_holiday_detects_chuseok_weekday_2026():
    """2026-09-23 실측: 추석(9/24 목~26 토)이 평일(1-5) cron과 겹친다 —
    cron/n8n은 요일만 볼 뿐 공휴일을 모르므로 이 함수가 따로 걸러야 한다."""
    thu_chuseok_eve = datetime(2026, 9, 24, 6, 0, tzinfo=KST)
    fri_chuseok = datetime(2026, 9, 25, 6, 0, tzinfo=KST)
    assert is_kr_holiday(thu_chuseok_eve) is True
    assert is_kr_holiday(fri_chuseok) is True


def test_is_kr_holiday_false_on_ordinary_weekday():
    ordinary_tue = datetime(2026, 9, 22, 6, 0, tzinfo=KST)
    assert is_kr_holiday(ordinary_tue) is False
