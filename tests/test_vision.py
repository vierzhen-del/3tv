"""vision.analyze_frames의 할당량 예산·429 중단·재시도 로직 테스트 (Gemini mock)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from threetv import vision
from threetv.frames import FrameInfo


class FakeModels:
    """responses 리스트를 순서대로 소비 — Exception이면 raise, 문자열이면 응답 텍스트."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[str] = []  # 호출된 model명 기록

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(text=r)


def _fake_frames(tmp_path, n):
    frames = []
    for i in range(n):
        p = tmp_path / f"f_{i:05d}.jpg"
        p.write_bytes(b"\xff\xd8fake")
        frames.append(FrameInfo(path=p, timestamp_sec=i * 10, white_ratio=0.5))
    return frames


def _ok_response(batch_len):
    return json.dumps(
        [{"index": i, "type": "자료화면", "text": f"t{i}"} for i in range(batch_len)]
    )


@pytest.fixture
def patch_client(monkeypatch):
    def _install(responses):
        fake = FakeModels(responses)
        monkeypatch.setattr(
            vision, "_client", lambda: SimpleNamespace(models=fake)
        )
        return fake
    return _install


def test_batch_size_controls_request_count(tmp_path, patch_client):
    frames = _fake_frames(tmp_path, 4)
    fake = patch_client([_ok_response(2), _ok_response(2)])
    results = vision.analyze_frames(frames, "m", tmp_path, batch_size=2)
    assert len(fake.calls) == 2
    assert len(results) == 4


def test_quota_error_without_fallback_aborts_remaining_batches(tmp_path, patch_client):
    frames = _fake_frames(tmp_path, 4)
    # 배치 0에서 429 → 배치 1은 아예 전송하지 않아야 함
    fake = patch_client([Exception("429 RESOURCE_EXHAUSTED: quota"), _ok_response(2)])
    results = vision.analyze_frames(
        frames, "m", tmp_path, batch_size=2, fallback_model=""
    )
    assert len(fake.calls) == 1
    assert results == []


def test_quota_error_switches_to_fallback_model(tmp_path, patch_client):
    frames = _fake_frames(tmp_path, 4)
    # 배치 0: 기본 모델 429 → 폴백으로 같은 배치 재시도 성공 → 배치 1도 폴백으로
    fake = patch_client(
        [Exception("RESOURCE_EXHAUSTED"), _ok_response(2), _ok_response(2)]
    )
    results = vision.analyze_frames(
        frames, "main-model", tmp_path, batch_size=2, fallback_model="lite-model"
    )
    assert fake.calls == ["main-model", "lite-model", "lite-model"]
    assert len(results) == 4


def test_fallback_quota_error_aborts(tmp_path, patch_client):
    frames = _fake_frames(tmp_path, 4)
    # 기본·폴백 모두 429 → 즉시 중단, 배치 1 미전송
    fake = patch_client(
        [Exception("RESOURCE_EXHAUSTED"), Exception("RESOURCE_EXHAUSTED")]
    )
    results = vision.analyze_frames(
        frames, "main-model", tmp_path, batch_size=2, fallback_model="lite-model"
    )
    assert fake.calls == ["main-model", "lite-model"]
    assert results == []


def test_transient_failure_retried_once_then_skipped(tmp_path, patch_client):
    frames = _fake_frames(tmp_path, 4)
    # 배치 0: 파싱 불가 응답 2회(최초+재시도) → 포기, 배치 1은 정상 진행
    fake = patch_client(["not json", "still not json", _ok_response(2)])
    results = vision.analyze_frames(frames, "m", tmp_path, batch_size=2)
    assert len(fake.calls) == 3
    assert len(results) == 2
    assert all(r["frame_file"].startswith("f_0000") for r in results)


def test_max_requests_budget_guard(tmp_path, patch_client):
    frames = _fake_frames(tmp_path, 6)
    # 예산 2회: 배치 0이 2회(실패+재시도 실패) 소모 → 배치 1·2는 전송 없이 중단
    fake = patch_client(["broken", "broken", _ok_response(2), _ok_response(2)])
    results = vision.analyze_frames(
        frames, "m", tmp_path, batch_size=2, max_requests=2
    )
    assert len(fake.calls) == 2
    assert results == []
