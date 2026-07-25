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
    """LLM 응답에서 JSON 객체를 추출.

    strict=False가 핵심 — 리포트 본문(markdown_report)은 여러 줄짜리 마크다운이라
    LLM이 문자열 값 안에 `\\n` 이스케이프 대신 **실제 개행**을 그대로 넣는 일이 흔하다.
    기본(strict=True) json.loads는 이를 'Invalid control character'로 거부해
    리포트 전체가 버려진다(2026-07-25 실측: 45분 방송 리포트가 이 이유로 열화 처리됨).
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text.removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"JSON 객체를 찾지 못함: {text[:200]}")
    return json.loads(text[start : end + 1], strict=False)


def _call_claude(model: str, prompt: str, max_tokens: int = 8000) -> str:
    client = _client()
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def _call_gemini(model: str, prompt: str, max_tokens: int = 8000) -> str:
    """Gemini 호출.

    ⚠️ gemini-2.5 계열은 내부 '사고(thinking)' 토큰도 max_output_tokens 예산을
    함께 소모한다. 리포트처럼 긴 JSON을 요구하면 사고에 예산을 다 쓰고 본문이
    중간에서 잘려 JSON이 깨진다(2026-07-25 실측: 응답에 닫는 '}'가 없어 파싱 실패).
    → 사고 예산을 명시적으로 낮춰 출력 토큰을 확보하고, 잘렸으면 그 사실을
      명확한 예외로 올려 상위 로그에서 원인이 드러나게 한다.
    """
    from google import genai
    from google.genai import types

    api_key = env_token("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 미설정")
    client = genai.Client(api_key=api_key)

    cfg: dict = {"temperature": 0.2, "max_output_tokens": max_tokens}
    try:
        # 사고 예산을 소액으로 제한 (0=비활성). 모델이 미지원이면 아래 except로 폴백.
        cfg_obj = types.GenerateContentConfig(
            **cfg, thinking_config=types.ThinkingConfig(thinking_budget=512)
        )
        resp = client.models.generate_content(model=model, contents=prompt, config=cfg_obj)
    except Exception as e:
        if "thinking" not in str(e).lower():
            raise
        log.info("%s는 thinking_config 미지원 → 기본 설정으로 재호출", model)
        resp = client.models.generate_content(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(**cfg),
        )

    text = resp.text or ""
    # 토큰 상한에 걸려 잘린 응답은 JSON이 깨져 어차피 못 쓴다 — 원인을 명시해 올린다
    try:
        finish = str(resp.candidates[0].finish_reason or "")
    except Exception:
        finish = ""
    if "MAX_TOKENS" in finish.upper():
        raise RuntimeError(
            f"Gemini 응답이 max_output_tokens({max_tokens})에 걸려 잘렸습니다 "
            f"(finish_reason={finish}, 확보된 길이={len(text)}자). "
            f"max_tokens를 올리거나 프롬프트 요구 분량을 줄여야 합니다."
        )
    return text


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


def _parse_sections(text: str) -> dict:
    """구분선 기반 리포트 응답 파싱 (JSON 대신 쓰는 이유는 아래).

    LLM에게 긴 마크다운을 JSON 문자열로 감싸 달라고 하면 구조가 계속 깨진다 —
    2026-07-25 실측으로 3연속 실패했다:
      ① 문자열 안 실제 개행 → 'Invalid control character'
      ② 토큰 상한에 걸려 잘림 → 닫는 '}' 없음
      ③ 방송 인용문의 따옴표 미이스케이프 → "Expecting ',' delimiter"
    구분선 방식은 이스케이프가 아예 필요 없어 이 실패 유형이 원천적으로 사라진다.
    """
    marks = ["===TITLE===", "===TELEGRAM===", "===MARKDOWN===", "===HOLDINGS===", "===END==="]
    if "===TITLE===" not in text or "===MARKDOWN===" not in text:
        raise ValueError(f"구분선 형식이 아닙니다: {text[:200]}")

    pos = {m: text.find(m) for m in marks}

    def section(start_mark: str) -> str:
        i = pos[start_mark]
        if i < 0:
            return ""
        i += len(start_mark)
        # 다음으로 등장하는 구분선까지
        ends = [p for m, p in pos.items() if p > i]
        return text[i : min(ends)].strip() if ends else text[i:].strip()

    holdings: list[dict] = []
    for line in section("===HOLDINGS===").splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if not line or line.startswith("(") or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        name = parts[0]
        if not name:
            continue
        flag = parts[1].upper() if len(parts) > 1 else ""
        ctx = parts[2] if len(parts) > 2 else ""
        holdings.append({
            "name": name,
            "mentioned": flag.startswith("O") or flag == "TRUE",
            "context": ctx or None,
        })

    return {
        "title_keyword": section("===TITLE===").splitlines()[0].strip()
        if section("===TITLE===") else "",
        "telegram_text": section("===TELEGRAM==="),
        "markdown_report": section("===MARKDOWN==="),
        "holdings_mentioned": holdings,
    }


def _clock(base_kst: str, ts_sec: int) -> str:
    """녹화 시작 시각(KST) + 프레임 위치 → 방송 표시시각 'HH:MM' (짧게).

    라이브 녹화는 start_kst에 시작하므로 프레임 0초 = start_kst가 되어 정확하다.
    (VOD 트리밍 테스트에서는 트리밍 시작점만큼 오차가 생길 수 있다.)
    """
    try:
        h, m = (int(x) for x in str(base_kst).split(":")[:2])
    except (ValueError, AttributeError):
        h = m = 0
    total = h * 60 + m + int(ts_sec) // 60
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def _in_window(clock: str, window: list | None) -> bool:
    """'HH:MM'이 [시작,끝] 구간 안인지 (제로패딩이라 문자열 비교로 충분)."""
    if not window or len(window) != 2:
        return False
    return str(window[0]) <= clock <= str(window[1])


def _briefing_lines(news_briefing: list[dict] | None) -> str:
    """수집된 기사 목록을 '제목 · 링크 (검색어)' 줄로. LLM 요약 실패 시에도 쓰인다."""
    if not news_briefing:
        return ""
    lines = []
    for n in news_briefing:
        q = f" ({n['query']})" if n.get("query") else ""
        lines.append(f"• [{n['title']}]({n['url']}){q}")
        if n.get("summary"):
            lines.append(f"  {n['summary'][:160]}")
    return "\n".join(lines)


def _capture_blocks(
    vision_results: list[dict],
    verified_mentions: list[dict],
    base_kst: str,
    verbatim_window: list | None = None,
) -> str:
    """캡처 화면당 2줄 — ① 시각·종목·타이틀 ② 연관 기사 링크.

    verbatim_window(예: 07:45~08:10 주요지표 요약 슬라이드) 안의 화면은 압축하지 않고
    **원문 줄 구성을 그대로** 옮긴다. 그 화면은 배치 자체가 정보이기 때문이다.
    """
    from . import market

    news_map = {
        (v.get("name") or "").strip(): v.get("news") or []
        for v in verified_mentions if v.get("name")
    }
    blocks: list[str] = []
    for r in vision_results:
        clock = _clock(base_kst, r.get("timestamp_sec", 0))
        text = (r.get("text") or "").strip()
        stocks = [s for s in (r.get("stocks") or []) if s.get("name")]
        names = [str(s["name"]).strip() for s in stocks]

        if _in_window(clock, verbatim_window) and text:
            # 화면 그대로 — 줄 구성 보존
            blocks.append(f"**{clock} · 화면 원문**\n```\n{text}\n```")
        else:
            title = " ".join(text.split())
            if len(title) > 110:
                title = title[:110] + "…"
            head = f"**{clock}**"
            if names:
                head += f" · {', '.join(names[:6])}"
            blocks.append(f"{head}\n{title or '(텍스트 없음)'}")

        # 2번째 줄: 연관 기사 링크 (종목별 1건씩)
        links: list[str] = []
        for s in stocks[:4]:
            nm = str(s["name"]).strip()
            mkt = (s.get("market") or "").upper() or "KR"
            items = news_map.get(nm) or market.search_links(nm, mkt)
            if items:
                n = items[0]
                links.append(f"[{nm}]({n['url']})")
        if links:
            blocks.append(f"🔗 {' · '.join(links)}")
    return "\n".join(blocks)


def _quote_line(q: dict) -> str:
    """`• 나스닥: 20,123.45 📈 ▲1.23%` 형태.

    종가 기준일은 줄마다 반복하지 않고 섹션 제목에 한 번만 쓴다(_asof_label).
    """
    icon = q.get("icon") or ""
    sign = f"{q['direction']}{abs(q['change_pct'])}%"
    return f"• {q['name']}: {q['close']:,} {icon} {sign}".replace("  ", " ")


def _asof_label(quotes: list[dict]) -> str:
    """시세 목록의 대표 기준일을 'M/D 종가 기준'으로. 섹션 제목에 한 번만 표기."""
    dates = [q.get("asof") for q in quotes if q.get("asof")]
    if not dates:
        return ""
    common = max(set(dates), key=dates.count)
    try:
        y, m, d = common.split("-")
        return f"{int(m)}/{int(d)} 종가 기준"
    except ValueError:
        return f"{common} 종가 기준"


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
    news_briefing: list[dict] | None = None,
) -> dict:
    """LLM 없이 원자료만으로 리포트 생성 (LLM 완전 불가 시 열화 경로).

    구성은 정상 리포트와 같은 축 — **화면 캡처(자료화면) → 종목 시세 → 관련 뉴스링크**.
    시세와 뉴스링크는 API 키·LLM과 무관하게 확보되므로 AI 요약이 불가능해도
    쓸 만한 리포트가 된다. 반환 형태는 generate_report()와 동일해 호출 측
    (전송·아카이브)이 그대로 동작한다.
    """
    scfg = settings["sessions"][session]
    label = scfg["label"]
    today = now_kst().strftime("%Y-%m-%d (%a)")
    material = _material_digest(vision_results)
    holdings_mentioned = _match_holdings_mentions(
        holdings_data, f"{transcript}\n{material}"
    )
    captures = _capture_blocks(
        vision_results, verified_mentions,
        scfg.get("start_kst", "00:00"), scfg.get("verbatim_window"),
    )

    banner = (
        "⚠️ *AI 요약 없음 — 원자료 기반 자동 리포트*\n"
        f"LLM 호출이 모두 실패해(사유: {reason}) 방송 화면 캡처와 "
        "API 검증 시세만으로 자동 생성했습니다. 요약·전망 해석은 포함되지 않습니다."
    )

    idx_asof = _asof_label(indices)
    hold_asof = _asof_label(holdings_quotes)
    idx_block = "\n".join(_quote_line(q) for q in indices) or "• 조회 실패"
    hold_block = "\n".join(_quote_line(q) for q in holdings_quotes) or "• 조회 실패"
    mention_block = "\n".join(
        f"• {m['name']}: {'✅ ' + (m['context'] or '언급') if m['mentioned'] else '언급 없음'}"
        for m in holdings_mentioned
    ) or "• 대조할 보유종목 없음"
    # 언급 종목: 시세 + 관련 기사 링크를 함께 (LLM 없이도 링크는 제공 가능)
    mention_lines: list[str] = []
    for v in verified_mentions:
        nm = (v.get("name") or "").strip()
        if not nm:
            continue
        q = v.get("quote")
        head = f"• **{nm}**"
        if q:
            head += (f": {q['close']:,} {q.get('icon', '')} "
                     f"{q['direction']}{abs(q['change_pct'])}%")
        if v.get("context"):
            head += f" — {v['context']}"
        mention_lines.append(head)
        for n in (v.get("news") or [])[:3]:
            pub = f" ({n['publisher']})" if n.get("publisher") else ""
            mention_lines.append(f"  - [{n['title']}]({n['url']}){pub}")
    mentioned_stocks = "\n".join(mention_lines) or "추출 실패(LLM 필요)"

    # ── 옵시디안용: 화면 캡처 → 종목 → 뉴스링크 순서
    idx_title = f"💹 주요 지표 ({idx_asof})" if idx_asof else "💹 주요 지표"
    hold_title = f"💼 보유종목 시세 ({hold_asof})" if hold_asof else "💼 보유종목 시세"
    md_parts = [banner]
    if captures:
        md_parts.append(f"\n### 🖼 방송 화면 캡처 (시각 · 종목 · 관련 기사)\n{captures}")
    else:
        md_parts.append(
            "\n### 🖼 방송 화면 캡처\n"
            "비전 분석도 같은 사유로 실패해 자료화면 추출물이 없습니다."
        )
    md_parts += [
        f"\n### {idx_title}\n{idx_block}",
        f"\n### {hold_title}\n{hold_block}",
        f"\n### 💼 보유종목 언급 체크 (문자열 매칭)\n{mention_block}",
        f"\n### 📈 방송 언급 종목 · 관련 뉴스\n{mentioned_stocks}",
    ]
    # LLM 요약은 불가하니 기사 목록을 그대로 (중복 제거는 수집 단계에서 이미 완료)
    briefing_block = _briefing_lines(news_briefing)
    if briefing_block:
        md_parts.append(
            f"\n### 📰 뉴스 브리핑 (수집 원문 — AI 요약 없음)\n{briefing_block}"
        )
    if transcript:
        md_parts.append(f"\n### 🎙 음성 전사 (전문)\n```\n{transcript}\n```")
    md_parts.append(f"\n{settings['report']['disclaimer']}")
    markdown_report = "\n".join(md_parts)

    # ── 텔레그램용: 화면 캡처 → 지표 → 종목 → 뉴스링크
    telegram_text = "\n".join([
        banner,
        f"\n🖼 방송 화면 캡처\n{captures}" if captures else "",
        f"\n{idx_title}\n{idx_block}",
        f"\n{hold_title}\n{hold_block}",
        f"\n💼 보유종목 언급 체크\n{mention_block}",
        f"\n📈 방송 언급 종목 · 관련 뉴스\n{mentioned_stocks}",
    ])
    if transcript:
        room = settings["telegram"]["max_message_len"] - len(telegram_text) - 400
        budget = max(0, min(room, 1200))     # 발췌는 최대 1,200자
        excerpt = transcript[:budget]
        tail = "…(이하 생략 — 전문은 옵시디안 리포트·Actions 아티팩트 참조)" \
            if len(transcript) > len(excerpt) else ""
        telegram_text += f"\n\n🎙 음성 전사 발췌\n{excerpt}{tail}"

    log.warning("열화 리포트 생성 (LLM 없이 원자료 기반): %s / 화면 %d장, 지표 %d건, 전사 %d자",
                label, len(vision_results), len(indices), len(transcript))
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
    news_briefing: list[dict] | None = None,
) -> dict:
    """최종 리포트 생성.

    반환: {title_keyword, telegram_text, markdown_report, holdings_mentioned}
    """
    scfg = settings["sessions"][session]
    label = scfg["label"]
    disclaimer = settings["report"]["disclaimer"]
    today = now_kst().strftime("%Y-%m-%d (%a)")
    base_kst = scfg.get("start_kst", "00:00")
    vwin = scfg.get("verbatim_window")
    captures = _capture_blocks(vision_results, verified_mentions, base_kst, vwin)

    holdings_desc = json.dumps(
        [
            {"name": h.get("name"), "aliases": h.get("aliases", []), "market": h.get("market")}
            for h in holdings_data["holdings"] + holdings_data["watchlist"]
        ],
        ensure_ascii=False,
    )

    # 사용자 확정 리포트 순서 (2026-07-25) — us/kr 공통 골격, 세션별 강조점만 다르다.
    # 1 요약(미장·국장 전망) → 2 주요지표 → 3 언급종목 연관기사 → 4 8시 전후 캡처
    # → 5 관심종목 업데이트 → 6 기사 상세분석 + 수급/변동성
    common_order = """아래 **1~6 순서와 번호를 그대로** 지켜 작성하세요. 항목을 빼거나 순서를 바꾸지 마세요.

1) 📌 **3protv 요약** — 방송 핵심을 압축하고, 이어서 **미국장 정리**와 **오늘 국내장 전망**을
   각각 소제목으로 씁니다.
   - 미국장: 지수 흐름·주도 섹터·매크로 이슈
   - 국내장 전망: KOSPI·KOSDAQ 예상 방향과 근거, 반도체(삼성전자·SK하이닉스)에 미치는 영향,
     환율·외국인 수급 관점. **코스피 야간선물**은 방송 화면값 우선 → 없으면 EWY로 갈음(대용 명시)
     → 둘 다 없으면 "확인 불가"(추측 금지)
   - 방송에서 진행자가 국내장 전망을 언급했다면 그 내용을 우선 반영

2) 💹 **주요 지표 전일대비 현황** — 종가 기준일은 섹션 제목에 한 번만
   (예: `주요 지표 (7/24 종가 기준)`). 순서는 방송 슬라이드와 1:1 대조되도록:
   - 3대 지수: 다우존스 / 나스닥 / S&P500
   - 원자재·달러·환율: WTI / 달러인덱스 / 원-달러 / 위안-달러 / 엔-달러 / 금
   - 국채수익률: 10년물 / 2년물 / 3개월물 (커브 역전 여부 언급)
   - 반도체: SOX / SOXL / 엔비디아 / 마이크론 / 샌디스크
   - M7 + AI·반도체: 애플·MS·알파벳·아마존·엔비디아·메타·테슬라 / 인텔·AMD·브로드컴·오라클
   - 변동성: VIX

3) 📈 **언급종목 연관기사 검색 요약** — 방송에서 언급된 종목마다:
   - **항목당 정확히 2줄**: 1줄은 `• 종목명: 종가 아이콘 등락률 — 핵심 한 줄`,
     2줄은 `🔗 [기사제목](url) · [기사제목](url)`
   - 링크는 [언급 종목 검증 시세]의 `news` 배열과 [뉴스 브리핑 기사]에 주어진 url만 사용.
     **url을 절대 새로 만들지 마세요.**

4) 🖼 **8시 전후 캡처화면 정리** — [방송 화면 캡처] 블록을 그대로 활용해 캡처당 2줄
   (`**HH:MM** · 종목` / `🔗 링크`). 시각은 `08:00`처럼 짧게.
   - `화면 원문`으로 표시된 캡처(07:45~08:10 주요지표 요약 슬라이드)는 **요약하지 말고
     화면의 줄 구성을 그대로** 옮기세요. 항목 순서·구분자(/)·수치를 원문대로 유지.

5) 💼 **관심종목 업데이트** — 보유/관심 종목의 전일 종가·등락률 + 방송 언급 여부.
   언급됐으면 어떤 맥락인지, 안 됐으면 "언급 없음". 보유종목에 직접 영향이 있는 이슈를 짚어주세요.

6) 🔎 **기사 상세분석 · 수급/변동성** — 3번 기사들을 한 단계 더 파고든 요약 리포트:
   - 겹치는 기사는 묶어 사안별 3줄 이내 요약 + 대표 링크 1~2개
   - **전일 수급주체 수급동향**: [수급 데이터]의 investors(기관·외국인·개인 순매수, 억원)
   - **순매수 top10 / 순매도 top10**: [수급 데이터]의 top
   - **ETF 등락 상위/하위**: [수급 데이터]의 etf
   - **VIX (미장·국장)**: 미장은 검증 시세의 VIX, 국장은 VKOSPI(코스피 변동성지수)가
     검증 시세에 있으면 사용하고 없으면 "확인 불가"로 적으세요
   - 수급 데이터가 비어 있으면 그 항목만 "조회 실패"로 적고 넘어가세요 (숫자 창작 금지)"""

    session_focus = {
        "us": "이 방송은 전일 미국장 마감 리뷰입니다. 1번의 미국장 정리를 특히 두껍게 쓰세요.",
        "kr": "이 방송은 당일 한국장 개장 전 전망입니다. 1번의 국내장 전망과 4번(8시 전후 화면)을 "
              "특히 두껍게 쓰고, 미국 시황과의 연결 고리는 아래 '오늘 미국 세션 리포트'를 참고하세요.",
    }[session]
    session_goal = f"{session_focus}\n\n{common_order}"

    us_ctx = f"\n\n[오늘 미국 세션 리포트 (참고)]\n{us_context_md[:8000]}" if us_context_md else ""

    src_desc = "방송 화면 캡처 판독 결과" + ("와 음성 전사" if transcript else "")
    prompt = f"""당신은 한국 개인투자자를 위한 시황 애널리스트입니다.
삼프로TV {label} 방송({today})의 {src_desc}, 그리고 API로 검증한 실제 시세를 바탕으로 데일리 리포트를 작성하세요.
리포트의 핵심 축은 **① 방송 화면에서 읽은 내용 ② 종목 시세 ③ 관련 뉴스 링크** 입니다.

{session_goal}

공통 요구사항:
- 🖼 **방송 화면 캡처 섹션을 리포트 맨 앞에** 두고, **캡처 1장당 정확히 2줄**로 쓰세요:
    1줄: `**HH:MM** · 종목명들` 다음 줄에 그 화면의 핵심 내용 한 줄
    2줄: `🔗 [종목명](기사url) · [종목명](기사url)`
  시각은 `06:03`처럼 **짧게(HH:MM)** — 초 단위나 `[00:03:15]` 같은 표기는 쓰지 마세요.
  아래 [방송 화면 캡처] 블록에 이미 이 형식으로 정리돼 있으니 **그 내용과 링크를
  그대로 활용**하고, url을 새로 만들지 마세요.
- **`화면 원문`으로 표시된 캡처(07:45~08:10 주요지표 요약 슬라이드)는 요약하지 말고
  화면의 줄 구성을 그대로 옮기세요.** 그 화면은 배치 자체가 정보입니다 — 항목 순서,
  구분자(/), 수치를 원문대로 유지하고 빠뜨리지 마세요.
- 💹 주요 지수/자산 변동 섹션 포함 (아래 검증 시세 사용)
- 💼 보유종목 언급 체크: 아래 보유/관심 종목이 방송에서 언급됐는지 확인. 언급됐으면 어떤 맥락인지, 안 됐으면 "언급 없음"으로.
- 가격·등락률은 반드시 [검증 시세]를 우선하고, 화면 숫자는 검증 실패 시에만 "(방송 화면 기준)"을 붙여 사용
- **종가 기준일은 섹션 제목에 한 번만** 쓰세요 (예: `💹 주요 지표 (7/24 종가 기준)`).
  줄마다 `[2026-07-24 종가]`처럼 반복하면 지저분해집니다 — 절대 줄 끝에 붙이지 마세요.
  기준일은 검증 시세의 `asof` 필드를 쓰고, 항목별로 다르면 그 항목에만 예외 표기하세요.
- **등락 아이콘을 반드시 붙이세요** — 각 시세의 `icon` 필드(📈 상승 / 📉 하락 / ➖ 보합)를
  그대로 사용하고, 형식은 다음을 따르세요:
  `• 나스닥: 20,123.45 📈 ▲1.23%`
  숫자에는 천단위 쉼표를 넣고, 목록은 `•` 불릿으로 정렬해 한눈에 읽히게 하세요.
- 시세가 없는 항목(조회 실패·비상장 등)은 "조회 불가"로만 적고 **절대 숫자를 만들지 마세요**.
  특히 `nan`·`0` 같은 값을 시세처럼 쓰면 안 됩니다.
- 예측은 근거(방송 발언/자료)와 함께, 과신 없이
- 마지막에 디스클레이머: {disclaimer}

출력 형식 — 아래 구분선을 **그대로** 쓰고 각 구분선 뒤에 내용을 쓰세요.
JSON이 아닙니다. 따옴표 이스케이프도 필요 없고, 줄바꿈을 자유롭게 쓰세요.

===TITLE===
오늘 방송 핵심 키워드 한 단어~구 (파일명용, 예: 반도체급등)
===TELEGRAM===
텔레그램용 요약. 이모지로 섹션 구분, 3500자 이내, 마크다운 헤더(#) 금지, 굵게는 *텍스트*
※ 위 1~6번 섹션을 **모두** 담으세요. 특히 **화면 캡처 2줄 표기와 관련 뉴스 링크는
  텔레그램 본문에도 반드시 포함**하세요(마크다운 리포트에만 넣고 빠뜨리지 말 것).
  분량이 넘칠 것 같으면 각 섹션의 설명 문장을 줄이고, 링크와 수치는 남기세요.
===MARKDOWN===
옵시디안용 상세 리포트. ## 섹션 헤더와 표 사용 가능
===HOLDINGS===
보유종목마다 한 줄씩: 종목명 | O 또는 X | 언급 맥락
(예: 삼성전자 | O | 미 상무장관이 미국 생산 확대 촉구
     테슬라 | X | )
===END===

[주요 지수/자산 검증 시세]
{json.dumps(indices, ensure_ascii=False)}

[언급 종목 검증 시세] (quote가 null이면 검증 실패 → 화면 숫자 사용 시 표기)
{json.dumps(verified_mentions, ensure_ascii=False)}

[보유/관심 종목 목록]
{holdings_desc}

[보유/관심 종목 검증 시세]
{json.dumps(holdings_quotes, ensure_ascii=False)}

[방송 화면 캡처 — 캡처당 '시각 · 종목' + 관련 기사 링크 (그대로 활용)]
{captures}

[화면 캡처 원문 판독 (광고 제외, 시간순)]
{_material_digest(vision_results)}
{f"{chr(10)}[뉴스 브리핑 기사 — 겹치는 것 묶어 3줄 요약]{chr(10)}" + _briefing_lines(news_briefing) if news_briefing else ""}
{f"{chr(10)}[음성 전사]{chr(10)}" + transcript[:40000] if transcript else ""}{us_ctx}"""

    # LLM이 완전히 불가능해도(할당량·빌링·모델 오류) 리포트는 반드시 발행한다.
    # 캡처·전사·시세는 이미 확보돼 있으므로 그날 작업을 통째로 버리지 않는다.
    try:
        # 33개 지표 + M7 표 + 6개 섹션을 요구하므로 출력 분량이 크다.
        # 12000으로는 사고 토큰까지 겹쳐 응답이 잘렸다(7/25 실측) → 넉넉하게 확보.
        data = _parse_sections(_call_llm(settings["models"], prompt, max_tokens=40000))
        for key in ("title_keyword", "telegram_text", "markdown_report"):
            if not data.get(key):
                raise RuntimeError(f"리포트 생성 결과에 {key} 누락")
    except Exception as e:
        log.error("LLM 리포트 생성 실패 → 원자료 기반 열화 리포트로 전환: %s", e)
        data = _fallback_report(
            settings, session, vision_results, transcript, indices,
            verified_mentions, holdings_data, holdings_quotes, reason=str(e)[:200],
            news_briefing=news_briefing,
        )

    out_file = out_dir / "report.json"
    out_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(data["markdown_report"], encoding="utf-8")
    log.info("리포트 생성 완료: 키워드=%s", data["title_keyword"])
    return data
