"""프레임 휴리스틱 단위 테스트 — API 키 없이 실행 가능."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from threetv.frames import FrameInfo, select_material_frames, white_background_ratio

CFG = {
    "white_value_min": 190,
    "white_sat_max": 45,
    "white_ratio_min": 0.35,
    "phash_distance": 6,
    "max_frames_to_vision": 80,
    "interval_sec": 10,
}


def make_material_frame() -> np.ndarray:
    """흰 배경 + 검은 텍스트 유사 이미지 (자료화면)."""
    img = np.full((480, 854, 3), 245, dtype=np.uint8)
    img[100:110, 50:700] = 20   # 텍스트 줄 흉내
    img[200:210, 50:500] = 20
    return img


def make_studio_frame() -> np.ndarray:
    """어두운 스튜디오 유사 이미지."""
    rng = np.random.default_rng(42)
    return rng.integers(10, 120, (480, 854, 3), dtype=np.uint8).astype(np.uint8)


def test_white_ratio_separates_material_from_studio():
    material = white_background_ratio(make_material_frame(), 190, 45)
    studio = white_background_ratio(make_studio_frame(), 190, 45)
    assert material > 0.9
    assert studio < 0.05


def test_select_material_frames_filters_and_dedupes(tmp_path):
    import cv2

    paths = []
    # 자료화면 3장(동일 → 중복 제거 대상) + 스튜디오 2장
    for i, img in enumerate(
        [make_material_frame(), make_material_frame(), make_material_frame(),
         make_studio_frame(), make_studio_frame()]
    ):
        p = tmp_path / f"f_{i:05d}.jpg"
        cv2.imwrite(str(p), img)
        paths.append(p)

    frames = [FrameInfo(path=p, timestamp_sec=i * 10, white_ratio=0.0)
              for i, p in enumerate(paths)]
    selected = select_material_frames(frames, CFG)

    # 스튜디오 제외 + 동일 자료화면은 1장으로 dedupe
    assert len(selected) == 1
    assert selected[0].white_ratio > 0.9
