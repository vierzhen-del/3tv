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
    selected = select_material_frames(frames, CFG, mode="white")

    # 스튜디오 제외 + 동일 자료화면은 1장으로 dedupe
    assert len(selected) == 1
    assert selected[0].white_ratio > 0.9


def test_all_mode_keeps_dark_data_screens(tmp_path):
    """us 세션(all 모드): 어두운 배경 데이터화면(Finviz류)도 살아남아야 함."""
    import cv2

    def make_dark_dashboard() -> "np.ndarray":
        # 어두운 배경 + 밝은 텍스트/차트 흉내 (Finviz류) — 노이즈가 아닌 규칙 패턴
        img = np.full((480, 854, 3), 25, dtype=np.uint8)
        for y in range(60, 460, 40):
            img[y : y + 8, 30:820] = (90, 200, 90)
        return img

    imgs = [make_material_frame(), make_dark_dashboard(), make_studio_frame()]
    paths = []
    for i, img in enumerate(imgs):
        p = tmp_path / f"f_{i:05d}.jpg"
        cv2.imwrite(str(p), img)
        paths.append(p)
    frames = [FrameInfo(path=p, timestamp_sec=i * 10, white_ratio=0.0)
              for i, p in enumerate(paths)]

    white_selected = select_material_frames(frames, CFG, mode="white")
    all_selected = select_material_frames(frames, CFG, mode="all")

    # white 모드는 어두운 대시보드를 버리지만, all 모드는 Gemini 분류용으로 유지
    assert len(white_selected) == 1
    assert len(all_selected) == 3
    # 상한 초과 시 흰배경 우선 정렬 확인
    small_cap = dict(CFG, max_frames_to_vision=2)
    capped = select_material_frames(frames, small_cap, mode="all")
    assert len(capped) == 2
    assert any(f.white_ratio > 0.9 for f in capped)  # 흰배경 자료화면은 반드시 포함
