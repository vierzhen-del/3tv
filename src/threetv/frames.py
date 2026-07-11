"""프레임 추출 + 자료화면 선별.

1) ffmpeg로 N초당 1프레임 추출
2) 흰 배경 휴리스틱(고명도·저채도 픽셀 비율)으로 자료화면 후보 1차 선별
   — 사용자 정의: 흰색 배경 텍스트 화면 = 자료화면 (스튜디오/광고와 구분)
3) perceptual hash로 유사 프레임 중복 제거 → 비전 API 비용 절감
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import imagehash
import numpy as np
from PIL import Image

from .common import log


@dataclass
class FrameInfo:
    path: Path
    timestamp_sec: int      # 영상 내 위치 (초)
    white_ratio: float


def extract_frames(video: Path, out_dir: Path, interval_sec: int) -> list[FrameInfo]:
    """ffmpeg로 interval_sec당 1프레임을 jpg로 추출."""
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    pattern = frames_dir / "f_%05d.jpg"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video),
        "-vf", f"fps=1/{interval_sec}",
        "-q:v", "3",
        str(pattern),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=1800)
    files = sorted(frames_dir.glob("f_*.jpg"))
    log.info("프레임 추출: %d장 (%d초 간격)", len(files), interval_sec)
    # f_00001.jpg 은 영상 시작 직후 → timestamp = (n-1)*interval
    return [
        FrameInfo(path=f, timestamp_sec=(i) * interval_sec, white_ratio=0.0)
        for i, f in enumerate(files)
    ]


def white_background_ratio(img_bgr: np.ndarray, value_min: int, sat_max: int) -> float:
    """흰 배경 픽셀(고명도·저채도) 비율. 자료화면 판별의 핵심 휴리스틱."""
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    white_mask = (val >= value_min) & (sat <= sat_max)
    return float(np.count_nonzero(white_mask)) / white_mask.size


def select_material_frames(
    frames: list[FrameInfo], frames_cfg: dict
) -> list[FrameInfo]:
    """흰 배경 필터 → phash 중복 제거 → 상한 적용."""
    value_min = frames_cfg["white_value_min"]
    sat_max = frames_cfg["white_sat_max"]
    ratio_min = frames_cfg["white_ratio_min"]
    phash_dist = frames_cfg["phash_distance"]
    max_frames = frames_cfg["max_frames_to_vision"]

    # 1차: 흰 배경 비율 필터
    candidates: list[FrameInfo] = []
    for fi in frames:
        img = cv2.imread(str(fi.path))
        if img is None:
            continue
        fi.white_ratio = white_background_ratio(img, value_min, sat_max)
        if fi.white_ratio >= ratio_min:
            candidates.append(fi)
    log.info("흰 배경 자료화면 후보: %d/%d장", len(candidates), len(frames))

    # 2차: perceptual hash 중복 제거 (같은 슬라이드가 오래 떠 있는 경우)
    unique: list[FrameInfo] = []
    hashes: list[imagehash.ImageHash] = []
    for fi in candidates:
        with Image.open(fi.path) as im:
            h = imagehash.phash(im)
        if any(h - prev <= phash_dist for prev in hashes):
            continue
        hashes.append(h)
        unique.append(fi)
    log.info("중복 제거 후 유니크 자료화면: %d장", len(unique))

    # 3차: 비용 상한 — 초과 시 시간축에서 균등 샘플링
    if len(unique) > max_frames:
        idx = np.linspace(0, len(unique) - 1, max_frames).astype(int)
        unique = [unique[i] for i in idx]
        log.info("상한 적용: %d장으로 샘플링", max_frames)
    return unique


def prepare_frames(video: Path, out_dir: Path, frames_cfg: dict) -> list[FrameInfo]:
    frames = extract_frames(video, out_dir, frames_cfg["interval_sec"])
    if not frames:
        raise RuntimeError("프레임 추출 결과가 비어 있음 — 영상 파일 확인 필요")
    return select_material_frames(frames, frames_cfg)
