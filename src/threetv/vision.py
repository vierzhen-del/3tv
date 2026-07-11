"""Gemini 비전: 자료화면 프레임 분류 + 텍스트/표/그래프 구조화 추출.

흰 배경 휴리스틱을 통과한 프레임을 배치로 보내
- type: 자료화면 / 스튜디오 / 광고 / 기타  (광고·배경화면은 최종 분석에서 제외)
- 추출 텍스트, 언급 종목, 수치, 그래프 설명
을 JSON으로 받는다.
"""
from __future__ import annotations

import json
from pathlib import Path

from .common import env, log
from .frames import FrameInfo

BATCH_SIZE = 8

PROMPT = """당신은 한국 경제방송(삼프로TV) 화면 분석 전문가입니다.
첨부된 이미지들은 아침 시황 방송에서 10초 간격으로 캡처한 프레임입니다.
각 이미지를 순서대로 분석해 JSON 배열로만 답하세요 (다른 텍스트 금지).

각 이미지당 하나의 객체:
{
  "index": <이미지 순번, 0부터>,
  "type": "자료화면" | "스튜디오" | "광고" | "기타",
  "text": "<화면의 모든 텍스트를 읽어서 기록. 표는 행 단위로>",
  "stocks": [{"name": "<종목/지수명>", "price": "<표시된 가격/지수, 없으면 null>", "change": "<표시된 등락률/폭, 없으면 null>", "market": "US"|"KR"|"OTHER"}],
  "chart": "<그래프/차트가 있으면 무엇의 추이인지, 방향(상승/하락/횡보)과 함께 설명. 없으면 null>"
}

판별 기준:
- 자료화면: 흰색 배경에 텍스트/표/그래프가 있는 방송용 자료 슬라이드 (뉴스 헤드라인, 지수표, 종목표, 경제지표 등)
- 광고: 상품/서비스 홍보, 협찬, 구독 유도 화면 → text는 간단히만
- 스튜디오: 진행자/패널이 보이는 화면
- 텍스트가 흐릿해도 읽을 수 있는 만큼 최대한 추출하세요. 숫자(가격·등락률)는 특히 정확하게."""


def _client():
    from google import genai

    api_key = env("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 미설정")
    return genai.Client(api_key=api_key)


def _parse_json_array(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text.removeprefix("json").strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"JSON 배열을 찾지 못함: {text[:200]}")
    return json.loads(text[start : end + 1])


def analyze_frames(frames: list[FrameInfo], model: str, out_dir: Path) -> list[dict]:
    """프레임 배치를 Gemini로 분석. 프레임별 결과 dict 리스트 반환."""
    from google.genai import types

    client = _client()
    results: list[dict] = []

    for batch_start in range(0, len(frames), BATCH_SIZE):
        batch = frames[batch_start : batch_start + BATCH_SIZE]
        parts: list = [PROMPT]
        for fi in batch:
            parts.append(
                types.Part.from_bytes(
                    data=fi.path.read_bytes(), mime_type="image/jpeg"
                )
            )
        try:
            resp = client.models.generate_content(
                model=model,
                contents=parts,
                config=types.GenerateContentConfig(temperature=0.1),
            )
            parsed = _parse_json_array(resp.text or "")
        except Exception as e:  # 배치 하나 실패해도 전체는 계속
            log.warning("Gemini 배치 %d 분석 실패: %s", batch_start // BATCH_SIZE, e)
            continue

        for item in parsed:
            idx = item.get("index")
            if not isinstance(idx, int) or not (0 <= idx < len(batch)):
                continue
            fi = batch[idx]
            item["frame_file"] = fi.path.name
            item["timestamp_sec"] = fi.timestamp_sec
            results.append(item)
        log.info(
            "Gemini 배치 %d 완료 (%d/%d 프레임 분석됨)",
            batch_start // BATCH_SIZE, len(results), len(frames),
        )

    material = [r for r in results if r.get("type") == "자료화면"]
    log.info("자료화면 확정: %d장 (광고/스튜디오 %d장 제외)",
             len(material), len(results) - len(material))

    out_file = out_dir / "vision_results.json"
    out_file.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return material
