"""종목 추출 + 최종 시황 리포트 생성 (Claude 우선, Gemini 폴백).

1단계: 자료화면 추출물 + 전사 텍스트에서 언급 종목 목록 추출 (티커 추정 포함)
        → market.py가 실시세로 검증
2단계: 화면 추출물 + 전사 + 검증된 시세 + 보유종목을 종합해
        텔레그램용 요약과 옵시디안용 마크다운 리포트 생성

무과금 운영 대응: Anthropic 크레딧 소진(400)·키 미설정 시 Gemini로 자동 폴백해
리포트가 끊기지 않게 한다 (2026-07-19 실측: 크레딧 부족으로 스케줄 런 전면 실패).
폴백은 세션당 Gemini 요청을 최대 2회(추출 1 + 리포트 1) 추가 소모하므로
frames.vision_max_requests 예산(하루 20요청) 안에서 여유분으로 흡수된다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .common import env_token, log, now_kst


def _client():
    import anthropic

    api_key = env_token("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정")
    return anthropic.Anthropic(api_key=api_key)


def _parse_json_obj(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text.removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"JSON 객체를 찾지 못함: {text[:200]}")
    return json.loads(text[start : end + 1])


def _call_claude(model: str, prompt: str, max_tokens: int = 8000) -> str:
    client = _client()
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def _call_gemini(model: str, prompt: str, max_tokens: int = 8000) -> str:
    from google import genai
    from google.genai import types

    api_key = env_token("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 미설정")
    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.2, max_output_tokens=max_tokens
        ),
    )
    return resp.text or ""


def _call_llm(models: dict, prompt: str, max_tokens: int = 8000) -> str:
    """Gemini 확정 운영 + Claude 크레딧 충전 시 자동 복귀.

    - models.claude_disabled=true (기본값, 2026-07-19 사용자 확정: 무과금 = Gemini 전용):
      Claude 호출 자체를 생략하고 바로 Gemini — 매 실행 실패 API 호출/로그 낭비 방지.
      크레딧을 충전하면 settings.yaml에서 이 플래그만 false로 바꾸면 코드 변경 없이
      Claude가 다시 primary가 된다 (14fiance CAPTURE_CLAUDE_API_DISABLED와 동일 계열).
    - claude_disabled=false인데 ANTHROPIC_API_KEY 미설정/호출 실패(크레딧 소진 400 등):
      Gemini로 폴백
    - Gemini 기본 모델 실패: models.gemini_fallback으로 1회 더 시도
    """
    claude_model = models.get("claude", "")
    if models.get("claude_disabled"):
        log.info("claude_disabled=true → Gemini로 진행 (Claude 호출 생략)")
    elif claude_model and env_token("ANTHROPIC_API_KEY"):
        try:
            return _call_claude(claude_model, prompt, max_tokens)
        except Exception as e:
            log.warning("Claude 호출 실패 → Gemini 폴백: %s", e)
    else:
        log.warning("ANTHROPIC_API_KEY 미설정 → Gemini로 진행")

    gemini_model = models.get("gemini", "gemini-2.5-flash")
    try:
        return _call_gemini(gemini_model, prompt, max_tokens)
    except Exception as e:
        fallback = models.get("gemini_fallback", "")
        if not fallback or fallback == gemini_model:
            raise
        log.warning("Gemini %s 실패 → %s 재시도: %s", gemini_model, fallback, e)
        try:
            return _call_gemini(fallback, prompt, max_tokens)
        except Exception as e2:
            # 어떤 모델이 왜 죽었는지 상위 열화 경로 로그에 남도록 사유를 합쳐 올린다
            raise RuntimeError(
                f"Gemini {gemini_model}·{fallback} 모두 실패: {e2}"
            ) from e2


def _material_digest(vision_results: list[dict], limit_chars: int = 30000) -> str:
    """자료화면 추출물을 시간순 텍스트로 압축."""
    lines = []
    for r in vision_results:
        ts = r.get("timestamp_sec", 0)
        mm, ss = divmod(int(ts), 60)
        parts = [f"[{mm:02d}:{ss:02d}]"]
        if r.get("text"):
            parts.append(str(r["text"]))
        if r.get("chart"):
            parts.append(f"(차트: {r['chart']})")
        for s in r.get("stocks") or []:
            seg = f"{s.get('name')} {s.get('price') or ''} {s.get('change') or ''}".strip()
            parts.append(f"<종목표시: {seg}>")
        lines.append(" ".join(parts))
    digest = "\n".join(lines)
    return digest[:limit_chars]


def _quote_line(q: dict) -> str:
    return f"- {q['name']}: {q['close']:,} ({q['direction']}{abs(q['change_pct'])}%)"


def _mentions_holding(needle: str, hay_lower: str) -> bool:
    """보유종목명/alias가 텍스트에 등장하는지.

    ASCII 티커·영문명(MU, TSLA, Tesla)은 **영숫자에 인접하지 않을 때만** 인정한다.
    - 'must'/'museum'이 'MU'로 잘못 걸리는 것을 막고,
    - 한국어 전사 특성상 조사가 바로 붙는 'Tesla도', 'TSLA는'은 정상 인정한다.
      (`\\b`는 한글도 word 문자로 봐서 'Tesla도'를 걸러버리므로 쓸 수 없다)
    한글명은 조사가 붙어 나오므로(삼성전자'가') 부분문자열 매칭이 맞다.
    """
    n = needle.lower()
    if n.isascii():
        pattern = rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])"
        return re.search(pattern, hay_lower) is not None
    return n in hay_lower


def _match_holdings_mentions(holdings_data: dict, haystack: str) -> list[dict]:
    """보유/관심 종목 언급 여부를 문자열 매칭으로 판정 (LLM 불필요).

    generate_report()가 LLM에게 시키는 것과 같은 형태
    [{name, mentioned, context}] 를 반환한다.
    """
    hay_lower = haystack.lower()
    result: list[dict] = []
    for h in holdings_data["holdings"] + holdings_data["watchlist"]:
        name = (h.get("name") or "").strip()
        if not name:
            continue
        # 티커도 매칭 대상 — 방송 자료화면·발언에 'TSLA', '005930'처럼 그대로 나온다
        needles = [name, str(h.get("ticker") or "")]
        needles += [str(a) for a in (h.get("aliases") or []) if a]
        hit = next((n for n in needles if n and _mentions_holding(n, hay_lower)), None)
        result.append({
            "name": name,
            "mentioned": hit is not None,
            "context": f"'{hit}' 방송 언급 (자동 문자열 매칭)" if hit else None,
        })
    return result


def _fallback_report(
    settings: dict,
    session: str,
    vision_results: list[dict],
    transcript: str,
    indices: list[dict],
    verified_mentions: list[dict],
    holdings_data: dict,
    holdings_quotes: list[dict],
    reason: str,
) -> dict:
    """LLM 없이 원자료만으로 리포트 생성 (LLM 완전 불가 시 열화 경로).

    Whisper 전사(로컬 추론)와 yfinance/pykrx 시세는 API 키·할당량과 무관하게
    항상 확보되므로, AI 요약이 불가능해도 이 둘만으로 유용한 리포트가 된다.
    반환 형태는 generate_report()와 동일해 호출 측(전송·아카이브)이 그대로 동작한다.
    """
    label = settings["sessions"][session]["label"]
    today = now_kst().strftime("%Y-%m-%d (%a)")
    material = _material_digest(vision_results)
    holdings_mentioned = _match_holdings_mentions(
        holdings_data, f"{transcript}\n{material}"
    )

    banner = (
        "⚠️ *AI 요약 없음 — 원자료 기반 자동 리포트*\n"
        f"LLM 호출이 모두 실패해(사유: {reason}) 방송 음성 전사와 "
        "API 검증 시세만으로 자동 생성했습니다. 요약·전망 해석은 포함되지 않습니다."
    )

    idx_block = "\n".join(_quote_line(q) for q in indices) or "- 조회 실패"
    hold_block = "\n".join(_quote_line(q) for q in holdings_quotes) or "- 조회 실패"
    mention_block = "\n".join(
        f"- {m['name']}: {'✅ ' + (m['context'] or '언급') if m['mentioned'] else '언급 없음'}"
        for m in holdings_mentioned
    ) or "- 대조할 보유종목 없음"
    named = [v.get("name") for v in verified_mentions if v.get("name")]
    mentioned_stocks = ", ".join(named) if named else "추출 실패(LLM 필요)"

    # ── 옵시디안용: 전사 전문 보존 (방송 내용 자체가 유일한 산출물이므로 자르지 않음)
    md_parts = [
        banner,
        f"\n### 💹 주요 지수/자산 ({today})\n{idx_block}",
        f"\n### 💼 보유종목 시세\n{hold_block}",
        f"\n### 💼 보유종목 언급 체크 (문자열 매칭)\n{mention_block}",
        f"\n### 📈 방송 언급 종목\n{mentioned_stocks}",
    ]
    if material:
        md_parts.append(f"\n### 📄 방송 자료화면 원문 (시간순)\n```\n{material}\n```")
    else:
        md_parts.append(
            "\n### 📄 방송 자료화면 원문\n"
            "비전 분석도 같은 사유로 실패해 자료화면 추출물이 없습니다."
        )
    md_parts.append(f"\n### 🎙 음성 전사 (전문)\n```\n{transcript}\n```")
    md_parts.append(f"\n{settings['report']['disclaimer']}")
    markdown_report = "\n".join(md_parts)

    # ── 텔레그램용: 1건 한도 안에서 전사를 최대한 (전문은 옵시디안·아티팩트에)
    tg_head = "\n".join([
        banner,
        f"\n💹 주요 지수/자산 ({today})\n{idx_block}",
        f"\n💼 보유종목 시세\n{hold_block}",
        f"\n💼 보유종목 언급 체크\n{mention_block}",
    ])
    budget = settings["telegram"]["max_message_len"] - len(tg_head) - 400
    excerpt = transcript[:budget] if budget > 0 else ""
    tail = "…(이하 생략 — 전문은 옵시디안 리포트·Actions 아티팩트 참조)" \
        if len(transcript) > len(excerpt) else ""
    telegram_text = f"{tg_head}\n\n🎙 음성 전사 발췌\n{excerpt}{tail}"

    log.warning("열화 리포트 생성 (LLM 없이 원자료 기반): %s / 전사 %d자, 지수 %d건",
                label, len(transcript), len(indices))
    return {
        "title_keyword": "원자료시황",
        "telegram_text": telegram_text,
        "markdown_report": markdown_report,
        "holdings_mentioned": holdings_mentioned,
    }


def extract_mentions(
    models: dict, vision_results: list[dict], transcript: str
) -> list[dict]:
    """방송에서 언급된 종목 목록 추출 → [{name, market, ticker_guess, context}]."""
    prompt = f"""다음은 삼프로TV 아침 시황 방송의 (A) 자료화면 추출 텍스트와 (B) 음성 전사입니다.
방송에서 언급되거나 화면에 표시된 **개별 종목**(지수/환율/원자재 제외)을 모두 추출하세요.

JSON 객체로만 답하세요:
{{"mentions": [{{"name": "<종목명>", "market": "US"|"KR", "ticker_guess": "<미국은 yfinance 티커, 한국은 6자리 코드 또는 정확한 한글 종목명>", "context": "<언급 맥락 한 줄: 호재/악재/실적/뉴스 등>"}}]}}

주의:
- 한국 기업의 미국 상장(ADR)은 언급된 시장 기준으로.
- OCR/전사 오류로 이름이 어색하면 문맥으로 올바른 종목명을 추정.
- 중복 제거, 최대 40개.

(A) 자료화면:
{_material_digest(vision_results)}

(B) 음성 전사:
{transcript[:40000]}"""
    try:
        data = _parse_json_obj(_call_llm(models, prompt, max_tokens=4000))
        mentions = data.get("mentions", [])
        log.info("언급 종목 추출: %d개", len(mentions))
        return mentions
    except Exception as e:
        log.warning("종목 추출 실패(리포트는 계속 진행): %s", e)
        return []


def generate_report(
    settings: dict,
    session: str,
    vision_results: list[dict],
    transcript: str,
    indices: list[dict],
    verified_mentions: list[dict],
    holdings_data: dict,
    holdings_quotes: list[dict],
    out_dir: Path,
    us_context_md: str = "",
) -> dict:
    """최종 리포트 생성.

    반환: {title_keyword, telegram_text, markdown_report, holdings_mentioned}
    """
    label = settings["sessions"][session]["label"]
    disclaimer = settings["report"]["disclaimer"]
    today = now_kst().strftime("%Y-%m-%d (%a)")

    holdings_desc = json.dumps(
        [
            {"name": h.get("name"), "aliases": h.get("aliases", []), "market": h.get("market")}
            for h in holdings_data["holdings"] + holdings_data["watchlist"]
        ],
        ensure_ascii=False,
    )

    session_goal = {
        "us": """이 방송은 전일 미국장 마감 리뷰입니다. 리포트 목표:
1) 🇺🇸 미국 전일 시황 핵심 요약 (지수 흐름, 섹터, 매크로 이슈)
2) 📈 언급된 미국 종목 표: 종목 | 종가 | 전일대비 (검증된 시세 우선 사용)
3) 🇰🇷 미국장 결과가 당일 한국장에 주는 시사점 — 언급 미국 종목과 연동되는 한국 관련주의 예상 방향(상승/하락 압력)과 근거""",
        "kr": """이 방송은 당일 한국장 개장 전 전망입니다. 리포트 목표:
1) 🇰🇷 한국 당일 시황 전망 핵심 요약 (수급, 섹터, 이벤트)
2) 언급된 한국 종목별: 최근 호재/악재(방송 자료 근거) + 당일 예상 방향
3) 미국 시황과의 연결 고리 (아래 '오늘 미국 세션 리포트' 참고)""",
    }[session]

    us_ctx = f"\n\n[오늘 미국 세션 리포트 (참고)]\n{us_context_md[:8000]}" if us_context_md else ""

    prompt = f"""당신은 한국 개인투자자를 위한 시황 애널리스트입니다.
삼프로TV {label} 방송({today})의 자료화면 추출물과 음성 전사, 그리고 API로 검증한 실제 시세를 바탕으로 데일리 리포트를 작성하세요.

{session_goal}

공통 요구사항:
- 💹 주요 지수/자산 변동 섹션 포함 (아래 검증 시세 사용)
- 💼 보유종목 언급 체크: 아래 보유/관심 종목이 방송에서 언급됐는지 확인. 언급됐으면 어떤 맥락인지, 안 됐으면 "언급 없음"으로.
- 가격·등락률은 반드시 [검증 시세]를 우선하고, 화면 숫자는 검증 실패 시에만 "(방송 화면 기준)"을 붙여 사용
- 예측은 근거(방송 발언/자료)와 함께, 과신 없이
- 마지막에 디스클레이머: {disclaimer}

JSON 객체로만 답하세요:
{{
  "title_keyword": "<오늘 방송 핵심 키워드 한 단어~구, 파일명용 (예: 반도체급등)>",
  "telegram_text": "<텔레그램용 요약. 이모지 섹션 구분, 3500자 이내, 마크다운 헤더(#) 금지, 굵게는 *텍스트* 형식>",
  "markdown_report": "<옵시디안용 상세 마크다운 리포트. ## 섹션 헤더, 표 사용 가능>",
  "holdings_mentioned": [{{"name": "<보유종목명>", "mentioned": true|false, "context": "<언급 맥락 또는 null>"}}]
}}

[주요 지수/자산 검증 시세]
{json.dumps(indices, ensure_ascii=False)}

[언급 종목 검증 시세] (quote가 null이면 검증 실패 → 화면 숫자 사용 시 표기)
{json.dumps(verified_mentions, ensure_ascii=False)}

[보유/관심 종목 목록]
{holdings_desc}

[보유/관심 종목 검증 시세]
{json.dumps(holdings_quotes, ensure_ascii=False)}

[자료화면 추출물 (광고 제외, 시간순)]
{_material_digest(vision_results)}

[음성 전사]
{transcript[:40000]}{us_ctx}"""

    # LLM이 완전히 불가능해도(할당량·빌링·모델 오류) 리포트는 반드시 발행한다.
    # 캡처·전사·시세는 이미 확보돼 있으므로 그날 작업을 통째로 버리지 않는다.
    try:
        data = _parse_json_obj(_call_llm(settings["models"], prompt, max_tokens=12000))
        for key in ("title_keyword", "telegram_text", "markdown_report"):
            if not data.get(key):
                raise RuntimeError(f"리포트 생성 결과에 {key} 누락")
    except Exception as e:
        log.error("LLM 리포트 생성 실패 → 원자료 기반 열화 리포트로 전환: %s", e)
        data = _fallback_report(
            settings, session, vision_results, transcript, indices,
            verified_mentions, holdings_data, holdings_quotes, reason=str(e)[:200],
        )

    out_file = out_dir / "report.json"
    out_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(data["markdown_report"], encoding="utf-8")
    log.info("리포트 생성 완료: 키워드=%s", data["title_keyword"])
    return data
