"""Gemini 비전: 자료화면 프레임 분류 + 텍스트/표/그래프 구조화 추출.

흰 배경 휴리스틱을 통과한 프레임을 배치로 보내
- type: 자료화면 / 스튜디오 / 광고 / 기타  (광고·배경화면은 최종 분석에서 제외)
- 추출 텍스트, 언급 종목, 수치, 그래프 설명
을 JSON으로 받는다.

무료 티어 할당량(gemini-2.5-flash 기준 20요청/일) 대응:
- 배치 크기·실행당 요청 상한은 settings.yaml frames.vision_batch_size /
  vision_max_requests 로 제어 (운영 2세션 합산이 한도의 절반 이하가 되도록)
- 429(RESOURCE_EXHAUSTED) 발생 시 남은 배치 전송을 즉시 중단하고 부분 결과로 진행
  (폴백 모델이 설정되어 있으면 먼저 폴백으로 전환해 계속 시도)
"""
from __future__ import annotations

import json
from pathlib import Path

from .common import env_token, log
from .frames import FrameInfo

PROMPT = """당신은 한국 경제방송(삼프로TV) 화면 분석 전문가입니다.
첨부된 이미지들은 아침 시황 방송에서 10초 간격으로 캡처한 프레임입니다.
각 이미지를 순서대로 분석해 JSON 배열로만 답하세요 (다른 텍스트 금지).

각 이미지당 하나의 객체:
{
  "index": <이미지 순번, 0부터>,
  "type": "자료화면" | "스튜디오" | "광고" | "진행자광고" | "기타",
  "text": "<자료화면의 본 자료 텍스트를 읽어서 기록. 표는 행 단위로. 영문 자료는 핵심 내용을 한국어로 정리하되 고유명사·수치는 원문 유지>",
  "stocks": [{"name": "<종목/지수명>", "price": "<표시된 가격/지수, 없으면 null>", "change": "<표시된 등락률/폭, 없으면 null>", "market": "US"|"KR"|"OTHER"}],
  "chart": "<그래프/차트가 있으면 무엇의 추이인지, 방향(상승/하락/횡보)과 함께 설명. 없으면 null>"
}

판별 기준:
- 자료화면(분석 대상):
  * 흰색 배경에 텍스트/표/그래프가 있는 방송용 자료 슬라이드 (뉴스 헤드라인, 지수표, 종목표, 경제지표 등)
  * 전체화면 데이터/브라우저 화면 — Finviz 맵, Investing.com 차트, 스크리너 등은 어두운 배경이어도 자료화면임
  * 영문 기사/자료 화면(월가 인사이트 등) — 영문이어도 반드시 분석 대상 (하단 한국어 자막이 있으면 함께 반영)
- 광고: 화면 전체가 상품/서비스 홍보, 협찬, 구독 유도인 경우 → text는 상품명만 간단히
- 진행자광고: 진행자가 앞에 놓인 보드/패널형 광고 상품을 들거나 소개하는 화면 (방송 종료 신호로 사용됨)
- 스튜디오: 진행자/패널이 보이는 일반 진행 화면

⚠️ 중요 — 자료화면 내 배너 광고 처리:
자료화면·스튜디오 화면의 배경 배너와 하단 고정 배너(ETF 상품, 의류, 식품, 멤버십 광고 등)는
**무시하고 본 자료 내용만 추출**하세요. 배너 속 ETF/상품명을 stocks에 넣지 마세요.
type 판정도 본 자료 기준으로 하세요 (배너가 있어도 본 자료가 시황 자료면 자료화면).

- 텍스트가 흐릿해도 읽을 수 있는 만큼 최대한 추출하세요. 숫자(가격·등락률)는 특히 정확하게."""


def _client():
    from google import genai

    api_key = env_token("GEMINI_API_KEY")
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


def _is_quota_error(e: Exception) -> bool:
    s = str(e)
    return "RESOURCE_EXHAUSTED" in s or "429" in s


def analyze_frames(
    frames: list[FrameInfo],
    model: str,
    out_dir: Path,
    batch_size: int = 16,
    max_requests: int = 6,
    fallback_model: str = "",
) -> list[dict]:
    """프레임 배치를 Gemini로 분석. 프레임별 결과 dict 리스트 반환.

    - max_requests: 이 실행에서 보낼 총 요청 수 상한 (재시도 포함) — 무료 티어
      일일 할당량 폭주 방지 안전벨트
    - 429/RESOURCE_EXHAUSTED: fallback_model이 있으면 남은 배치를 폴백으로 전환,
      폴백까지 소진되면 즉시 중단하고 그때까지의 부분 결과로 진행
    - 그 외 실패(파싱 오류 등): 배치당 1회만 재시도
    """
    from google.genai import types

    client = _client()
    results: list[dict] = []
    active_model = model
    n_requests = 0
    aborted = False

    for batch_start in range(0, len(frames), batch_size):
        if aborted:
            break
        batch = frames[batch_start : batch_start + batch_size]
        batch_no = batch_start // batch_size
        parts: list = [PROMPT]
        for fi in batch:
            parts.append(
                types.Part.from_bytes(
                    data=fi.path.read_bytes(), mime_type="image/jpeg"
                )
            )

        parsed: list[dict] | None = None
        attempts_left = 2  # 최초 1회 + 재시도 1회
        while attempts_left > 0 and not aborted:
            if n_requests >= max_requests:
                log.warning(
                    "Gemini 요청 예산(%d회) 소진 — 배치 %d 이후 생략, 부분 결과로 진행",
                    max_requests, batch_no,
                )
                aborted = True
                break
            n_requests += 1
            try:
                resp = client.models.generate_content(
                    model=active_model,
                    contents=parts,
                    config=types.GenerateContentConfig(temperature=0.1),
                )
                parsed = _parse_json_array(resp.text or "")
                break
            except Exception as e:
                if _is_quota_error(e):
                    if fallback_model and active_model != fallback_model:
                        log.warning(
                            "Gemini 할당량 소진(%s) → 폴백 모델 %s로 전환해 재시도",
                            active_model, fallback_model,
                        )
                        active_model = fallback_model
                        continue  # 같은 배치를 폴백으로 즉시 재시도 (재시도 횟수 미소모)
                    log.warning(
                        "Gemini 할당량 소진 — 남은 배치 분석 중단, 부분 결과로 진행: %s", e
                    )
                    aborted = True
                    break
                attempts_left -= 1
                if attempts_left > 0:
                    log.warning("Gemini 배치 %d 실패, 1회 재시도: %s", batch_no, e)
                else:
                    log.warning("Gemini 배치 %d 분석 실패(재시도 포함): %s", batch_no, e)

        if parsed is None:
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
            "Gemini 배치 %d 완료 (%d/%d 프레임 분석됨, 요청 %d/%d회)",
            batch_no, len(results), len(frames), n_requests, max_requests,
        )

    n_material = sum(1 for r in results if r.get("type") == "자료화면")
    log.info("자료화면: %d장 / 광고·스튜디오·진행자광고 등: %d장",
             n_material, len(results) - n_material)

    out_file = out_dir / "vision_results.json"
    out_file.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 전체 분류 결과 반환 — 자료화면 필터링과 진행자광고(방송 종료 신호) 감지는
    # 호출 측(main)에서 수행
    return sorted(results, key=lambda r: r.get("timestamp_sec", 0))
