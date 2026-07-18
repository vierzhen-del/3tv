"""공통 유틸 단위 테스트 — API 키 없이 실행 가능."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from threetv.common import output_dir, parse_duration


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
