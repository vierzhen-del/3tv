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

from . import tg_format
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


_MARK_RE = re.compile(r"===([A-Z]+)===")

# ===NEWS=== 안에서 '주요종목'과 '그 외'를 가르는 표식. 그 외는 접기 블록으로 내린다.
NEWS_REST_MARK = "---기타---"


def _split_marked(text: str) -> dict[str, str]:
    """`===NAME===` 구분선으로 나뉜 섹션들을 {NAME: 본문}으로."""
    out: dict[str, str] = {}
    marks = list(_MARK_RE.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out[m.group(1)] = text[m.end() : end].strip()
    return out


def _parse_holdings_lines(block: str) -> list[dict]:
    holdings: list[dict] = []
    for line in block.splitlines():
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
    return holdings


def _fold_news_rest(news_md: str) -> str:
    """기사 섹션의 `---기타---` 이후를 접기 블록으로.

    주요종목 기사는 본문에 그대로 노출하고, 나머지는 눌러야 펼쳐지게 한다
    (텔레그램=expandable blockquote, 옵시디안=접이식 callout).
    표식이 없으면 전체를 그대로 둔다 — LLM이 형식을 놓쳐도 내용은 안 잃는다.
    """
    if NEWS_REST_MARK not in news_md:
        return news_md
    head, rest = news_md.split(NEWS_REST_MARK, 1)
    rest = rest.strip()
    if not rest:
        return head.strip()
    n = sum(1 for ln in rest.splitlines() if ln.strip().startswith(("•", "-", "*")))
    title = f"그 외 종목 기사 ({n}건) — 눌러서 펼치기" if n else "그 외 종목 기사 — 눌러서 펼치기"
    return f"{head.strip()}\n\n{tg_format.fold(title, rest)}"


def _parse_sections(text: str) -> dict:
    """구분선 기반 리포트 응답 파싱 (JSON 대신 쓰는 이유는 아래).

    LLM에게 긴 마크다운을 JSON 문자열로 감싸 달라고 하면 구조가 계속 깨진다 —
    2026-07-25 실측으로 3연속 실패했다:
      ① 문자열 안 실제 개행 → 'Invalid control character'
      ② 토큰 상한에 걸려 잘림 → 닫는 '}' 없음
      ③ 방송 인용문의 따옴표 미이스케이프 → "Expecting ',' delimiter"
    구분선 방식은 이스케이프가 아예 필요 없어 이 실패 유형이 원천적으로 사라진다.

    2026-07-27부터 본문을 **SIHWANG(시황) / NEWS(종목기사검색) 2건**으로 받는다.
    이전의 TELEGRAM/MARKDOWN 2벌 출력은 같은 내용을 두 번 쓰게 해 출력 토큰을
    낭비하고 잘림 위험을 키웠다 — 이제 마크다운 한 벌을 받아 tg_format이
    텔레그램 HTML과 옵시디안 마크다운으로 각각 변환한다.
    """
    sec = _split_marked(text)
    sihwang = sec.get("SIHWANG") or sec.get("MARKDOWN") or ""
    if "TITLE" not in sec or not sihwang:
        raise ValueError(f"구분선 형식이 아닙니다: {text[:200]}")

    news_md = _fold_news_rest(sec.get("NEWS", ""))
    parts = [sihwang] + ([news_md] if news_md else [])
    return {
        "title_keyword": (sec.get("TITLE", "").splitlines() or [""])[0].strip(),
        # 하위호환 — 열화 경로·아카이브가 쓰는 통합 본문
        "telegram_text": sec.get("TELEGRAM") or sihwang,
        "markdown_report": "\n\n".join(parts),
        "holdings_mentioned": _parse_holdings_lines(sec.get("HOLDINGS", "")),
        # LLM 경로는 전사 전문을 본문에 싣지 않으므로 텔레그램용·옵시디안용이 같다
        "reports": {"sihwang": sihwang, "sihwang_md": sihwang, "news": news_md},
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


VERBATIM_MAX_LINES = 15   # '화면 원문' 보존 시 한 화면당 최대 줄 수

# 개별 종목 시세판을 가려내는 단서 — 이런 화면은 8시 전후 분석에서 제외한다
# (사용자 확정 2026-07-26: 08시 전후는 흰 배경 '그림' 슬라이드만 보고 개별 주가는 미적용)
_QUOTE_TABLE_HINTS = ("현재가", "거래량", "전일대비", "등락률", "체결")

# ETF 판별 — 삼프로TV(이 채널 한정)는 시황에서 ETF를 다루지 않으므로 전부 광고 취급.
# 화면에 뜨는 ETF는 예외 없이 협찬·상품 홍보였다 (사용자 확정 2026-07-27).
# 프롬프트만으로는 새는 경우가 있어 코드에서도 한 번 더 걸러낸다.
_ETF_BRANDS = (
    "KODEX", "TIGER", "ACE", "PLUS", "SOL", "KBSTAR", "ARIRANG", "HANARO",
    "KOSEF", "TIMEFOLIO", "RISE", "BNK", "히어로즈", "마이다스", "파워",
)
_ETF_WORDS = ("ETF", "ETN", "커버드콜", "인버스", "레버리지", "액티브", "TR)", "선물인버스")


def is_etf_name(name: str) -> bool:
    """ETF/ETN으로 보이는 종목명인지 (브랜드 접두어 또는 상품 키워드)."""
    n = (name or "").strip()
    if not n:
        return False
    upper = n.upper()
    if any(upper.startswith(b) or f" {b}" in f" {upper}" for b in _ETF_BRANDS):
        return True
    return any(w.upper() in upper for w in _ETF_WORDS)


def drop_etf_stocks(vision_results: list[dict]) -> int:
    """vision 결과의 stocks에서 ETF를 제거하고 제거 건수를 반환 (in-place)."""
    removed = 0
    for r in vision_results:
        stocks = r.get("stocks") or []
        kept = [s for s in stocks if not is_etf_name(str(s.get("name") or ""))]
        removed += len(stocks) - len(kept)
        if kept != stocks:
            r["stocks"] = kept
    return removed


def _is_quote_table(text: str, stocks: list | None) -> bool:
    """개별 종목 시세판(종목명·현재가·거래량 나열) 화면인지.

    2026-07-26 실측: 07:59 코스피/코스닥 시세판이 50줄 넘게 잡혀 리포트를 잠식했다.
    이런 화면은 '요약 슬라이드'가 아니라 단순 종목 나열이라 분석 가치가 낮다.
    """
    t = text or ""
    hits = sum(1 for h in _QUOTE_TABLE_HINTS if h in t)
    rows = [ln for ln in t.splitlines() if ln.count(",") >= 3]
    # 시세표 헤더 단어가 2개 이상 + 쉼표 구분 행이 여러 줄이면 시세판
    if hits >= 2 and len(rows) >= 5:
        return True
    # 헤더를 못 읽었더라도 한 화면에 종목이 과도하게 많으면 시세판으로 본다
    return len(stocks or []) >= 12


def _flow_lines(flows: dict | None) -> str:
    """수급 데이터를 사람이 읽는 줄로 (LLM 요약 실패 시에도 원자료가 남게)."""
    if not flows:
        return ""
    parts: list[str] = []
    inv = flows.get("investors") or []
    if inv:
        parts.append("• 전일 수급주체 순매수(억원): " + ", ".join(
            f"{r['investor']} {r['net']:+,.0f}" for r in inv[:8]))
    top = flows.get("top") or {}
    if top.get("buy"):
        parts.append(f"• {top.get('investor','')} 순매수 TOP: " + ", ".join(
            f"{r['name']}({r['net']:+,.0f})" for r in top["buy"][:10]))
    if top.get("sell"):
        parts.append(f"• {top.get('investor','')} 순매도 TOP: " + ", ".join(
            f"{r['name']}({r['net']:+,.0f})" for r in top["sell"][:10]))
    etf = flows.get("etf") or {}
    if etf.get("up"):
        parts.append("• ETF 상승 TOP: " + ", ".join(
            f"{r['name']}({r['pct']:+.2f}%)" for r in etf["up"][:10]))
    if etf.get("down"):
        parts.append("• ETF 하락 TOP: " + ", ".join(
            f"{r['name']}({r['pct']:+.2f}%)" for r in etf["down"][:10]))
    return "\n".join(parts)


def _capture_blocks(
    vision_results: list[dict],
    verified_mentions: list[dict],
    base_kst: str,
    verbatim_window: list | None = None,
) -> str:
    """캡처 화면당 2줄 — ① 시각·종목·타이틀 ② 연관 기사 링크.

    verbatim_window(예: 07:45~08:10 요약 슬라이드) 안의 화면은 압축하지 않고
    **원문 줄 구성을 그대로** 옮긴다. 그 화면은 배치 자체가 정보이기 때문이다.
    단 그 구간의 **개별 종목 시세판은 제외**한다 (사용자 확정: 08시 전후는 흰 배경
    그림 슬라이드만 보고 개별 주가는 반영하지 않는다).
    """
    from . import market

    news_map = {
        (v.get("name") or "").strip(): v.get("news") or []
        for v in verified_mentions if v.get("name")
    }
    blocks: list[str] = []
    skipped_tables = 0
    for r in vision_results:
        clock = _clock(base_kst, r.get("timestamp_sec", 0))
        text = (r.get("text") or "").strip()
        stocks = [s for s in (r.get("stocks") or []) if s.get("name")]
        names = [str(s["name"]).strip() for s in stocks]

        # 8시 전후 구간의 개별 종목 시세판은 분석 대상이 아니다
        if _in_window(clock, verbatim_window) and _is_quote_table(text, stocks):
            skipped_tables += 1
            continue

        if _in_window(clock, verbatim_window) and text:
            # 화면 그대로 — 줄 구성 보존. 단 시세 표처럼 줄이 매우 많은 화면은
            # 상위 일부만 (2026-07-26 실측: 50줄 넘는 종목 시세판이 리포트를 잠식했다)
            lines = text.splitlines()
            shown = lines[:VERBATIM_MAX_LINES]
            body = "\n".join(shown)
            if len(lines) > len(shown):
                body += f"\n…(총 {len(lines)}줄 중 {len(shown)}줄 표시)"
            blocks.append(f"**{clock} · 화면 원문**\n```\n{body}\n```")
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
    if skipped_tables:
        log.info("8시 전후 개별 종목 시세판 %d장 제외 (흰 배경 슬라이드만 분석)",
                 skipped_tables)
    return "\n".join(blocks)


def us_stocks_in_captures(vision_results: list[dict], limit: int = 12) -> list[str]:
    """캡처 화면에 등장한 **미국 종목명** (등장 순서, 중복 제거).

    언급종목 기사검색의 대상을 이 목록으로 잡는다 — 전사·LLM 추출 목록보다
    '방송 화면에 실제로 떴던 미장 종목'이 사용자가 원하는 기준이다.
    한글/영문 모두 그대로 두고(뉴스 검색은 한국어 매체가 대상), KR 종목은 제외한다.
    """
    out: list[str] = []
    for r in vision_results:
        stocks = r.get("stocks") or []
        if _is_quote_table(r.get("text") or "", stocks):
            continue                      # 시세판은 대상 아님
        for s in stocks:
            nm = str(s.get("name") or "").strip()
            mkt = (s.get("market") or "").upper()
            if not nm or mkt == "KR" or nm in out:
                continue
            out.append(nm)
            if len(out) >= limit:
                return out
    return out


MAJOR_MIN_APPEARANCES = 2   # 캡처에 이만큼 이상 뜬 종목은 '주요종목'


def major_stocks(holdings_data: dict, vision_results: list[dict]) -> list[str]:
    """본문에 그대로 노출할 **주요종목** 이름 목록.

    = 보유·관심 종목 ∪ 캡처 화면에 2회 이상 등장한 종목.
    내 포지션을 절대 놓치지 않으면서, 방송이 반복 강조한 종목도 함께 올린다.
    나머지 종목의 기사는 접기 블록으로 내린다(눌러야 펼쳐짐).
    """
    out: list[str] = []
    for h in holdings_data.get("holdings", []) + holdings_data.get("watchlist", []):
        name = (h.get("name") or "").strip()
        if name and name not in out:
            out.append(name)

    counts: dict[str, int] = {}
    for r in vision_results:
        stocks = r.get("stocks") or []
        if _is_quote_table(r.get("text") or "", stocks):
            continue                       # 시세판 나열은 '강조'가 아니다
        for nm in {str(s.get("name") or "").strip() for s in stocks}:
            if nm:
                counts[nm] = counts.get(nm, 0) + 1
    for nm, c in counts.items():
        if c >= MAJOR_MIN_APPEARANCES and nm not in out:
            out.append(nm)
    return out


def _quote_line(q: dict) -> str:
    """`• 나스닥: 20,123.45 📈 ▲1.23%` 형태.

    종가 기준일은 줄마다 반복하지 않고 섹션 제목에 한 번만 쓴다(_asof_label).
    """
    icon = q.get("icon") or ""
    sign = f"{q['direction']}{abs(q['change_pct'])}%"
    # 대표 기준일보다 오래된 항목만 자기 날짜를 병기 (섹션 제목과 다르다는 표시)
    tail = f" ⚠️({q['asof']} 기준)" if q.get("stale") and q.get("asof") else ""
    return f"• {q['name']}: {q['close']:,} {icon} {sign}{tail}".replace("  ", " ")


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
    flows: dict | None = None,
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
        f"• {m['name']}: {'📡 ' + (m['context'] or '방송 언급') if m['mentioned'] else '언급 없음'}"
        for m in holdings_mentioned
    ) or "• 대조할 보유종목 없음"

    # ── 1건: 시황 (요약 자리엔 원자료 배너 + 지표 + 캡처)
    idx_title = f"💹 주요 지표 ({idx_asof})" if idx_asof else "💹 주요 지표"
    hold_title = f"💼 보유종목 시세 ({hold_asof})" if hold_asof else "💼 보유종목 시세"
    sihwang_parts = [banner, f"\n### {idx_title}\n{idx_block}"]
    if captures:
        sihwang_parts.append(f"\n### 🖼 방송 화면 캡처 (시각 · 종목 · 관련 기사)\n{captures}")
    else:
        sihwang_parts.append(
            "\n### 🖼 방송 화면 캡처\n"
            "비전 분석도 같은 사유로 실패해 자료화면 추출물이 없습니다."
        )
    sihwang_parts += [
        f"\n### {hold_title}\n{hold_block}",
        f"\n### 📡 보유종목 방송 언급 체크 (문자열 매칭)\n{mention_block}",
    ]
    flow_block = _flow_lines(flows)
    if flow_block:
        sihwang_parts.append(f"\n### 🔎 수급·변동성 (원자료)\n{flow_block}")
    core = "\n".join(sihwang_parts)

    # 전사는 LLM 없는 날의 유일한 '방송 내용'이라 옵시디안엔 전문을 남기고,
    # 텔레그램엔 한 건 한도 안에서 발췌만 싣는다 (3만자를 그대로 쏟아내지 않도록).
    tail = f"\n\n{settings['report']['disclaimer']}"
    sihwang_full = core + (
        f"\n\n### 🎙 음성 전사 (전문)\n```\n{transcript}\n```" if transcript else ""
    ) + tail
    sihwang_md = core
    if transcript:
        room = settings["telegram"]["max_message_len"] - len(core) - len(tail) - 400
        excerpt = transcript[: max(0, min(room, 1200))]
        cut = "…(이하 생략 — 전문은 옵시디안 리포트·Actions 아티팩트 참조)" \
            if len(transcript) > len(excerpt) else ""
        sihwang_md += f"\n\n### 🎙 음성 전사 발췌\n{excerpt}{cut}"
    sihwang_md += tail

    # ── 2건: 종목 기사검색 (주요종목은 노출, 그 외는 접기)
    majors = major_stocks(holdings_data, vision_results)
    head_lines: list[str] = []
    rest_lines: list[str] = []
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
        links = " · ".join(
            f"[{n['title'][:40]}]({n['url']})" for n in (v.get("news") or [])[:3]
        )
        bucket = head_lines if nm in majors else rest_lines
        bucket.append(head)
        if links:
            bucket.append(f"  🔗 {links}")
    news_body = "\n".join(head_lines) or "추출 실패(LLM 필요)"
    if rest_lines:
        news_body += f"\n\n{NEWS_REST_MARK}\n" + "\n".join(rest_lines)
    news_md = f"### 📈 방송 언급 종목 · 관련 기사\n{_fold_news_rest(news_body)}"
    # LLM 요약은 불가하니 기사 목록을 그대로 (중복 제거는 수집 단계에서 이미 완료)
    briefing_block = _briefing_lines(news_briefing)
    if briefing_block:
        news_md += "\n\n### 📰 뉴스 브리핑 (수집 원문 — AI 요약 없음)\n" + tg_format.fold(
            "기사 목록 — 눌러서 펼치기", briefing_block
        )

    log.warning("열화 리포트 생성 (LLM 없이 원자료 기반): %s / 화면 %d장, 지표 %d건, 전사 %d자",
                label, len(vision_results), len(indices), len(transcript))
    return {
        "title_keyword": "원자료시황",
        "telegram_text": sihwang_md,
        "markdown_report": f"{sihwang_full}\n\n{news_md}",
        "holdings_mentioned": holdings_mentioned,
        # sihwang=텔레그램용(발췌) / sihwang_md=옵시디안용(전사 전문)
        "reports": {"sihwang": sihwang_md, "sihwang_md": sihwang_full, "news": news_md},
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
    flows: dict | None = None,
) -> dict:
    """최종 리포트 생성.

    반환: {title_keyword, telegram_text, markdown_report, holdings_mentioned,
           reports: {"sihwang": md, "news": md}}
    - `reports`가 세션당 2건 전송의 원본이고, `markdown_report`는 둘을 합친
      통합본(옵시디안 단일 파일·하위호환용)이다.
    """
    scfg = settings["sessions"][session]
    label = scfg["label"]
    disclaimer = settings["report"]["disclaimer"]
    today = now_kst().strftime("%Y-%m-%d (%a)")
    base_kst = scfg.get("start_kst", "00:00")
    vwin = scfg.get("verbatim_window")
    captures = _capture_blocks(vision_results, verified_mentions, base_kst, vwin)
    # ===NEWS===(종목 기사검색)의 대상 — 캡처 화면에 실제로 뜬 미장 종목
    capture_us = us_stocks_in_captures(vision_results)
    # 본문 노출 종목(나머지는 접기 블록으로)
    majors = major_stocks(holdings_data, vision_results)

    holdings_desc = json.dumps(
        [
            {"name": h.get("name"), "aliases": h.get("aliases", []), "market": h.get("market")}
            for h in holdings_data["holdings"] + holdings_data["watchlist"]
        ],
        ensure_ascii=False,
    )

    # 사용자 확정 구조 (2026-07-27 갱신) — 리포트를 **2건**으로 나눈다.
    #   ① 시황(===SIHWANG===): 1 요약 → 2 주요지표 → 3 캡처화면 → 4 관심종목 → 5 수급/변동성
    #   ② 종목 기사검색(===NEWS===): 주요종목은 노출, 그 외는 접기 블록
    # 한 건에 다 담으면 기사 목록이 본문을 잠식해 읽히지 않았다(실측 스크린샷).
    common_order = """### 리포트 ① 시황 (===SIHWANG===) — 아래 1~5 순서와 번호를 그대로

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
   - 지수선물: 나스닥100 선물 / S&P500 선물 / 다우 선물
   - 원자재·달러·환율: WTI / 달러인덱스 / 원-달러 / 위안-달러 / 엔-달러 / 금
   - 국채수익률: 10년물 / 2년물 / 3개월물 (커브 역전 여부 언급)
   - 반도체: SOX / SOXL / 엔비디아 / 마이크론 / 샌디스크
   - M7 + AI·반도체: 애플·MS·알파벳·아마존·엔비디아·메타·테슬라 / 인텔·AMD·브로드컴·오라클
   - 변동성: VIX

3) 🖼 **8시 전후 캡처화면 정리** — [방송 화면 캡처] 블록을 그대로 활용해 캡처당 2줄
   (`**HH:MM** · 종목` / `🔗 링크`). 시각은 `08:00`처럼 짧게.
   - `화면 원문`으로 표시된 캡처(07:45~08:10 흰 배경 요약 슬라이드)는 **요약하지 말고
     화면의 줄 구성을 그대로** 옮기세요. 항목 순서·구분자(/)·수치를 원문대로 유지.
   - ⚠️ 이 구간은 **흰 배경 그림·슬라이드만** 대상입니다. 개별 종목 시세판
     (종목명·현재가·거래량 나열)은 이미 제외돼 있으니 **개별 주가를 끌어와 쓰지 마세요.**

4) 💼 **관심종목 업데이트** — 보유/관심 종목의 전일 종가·등락률 + 방송 언급 여부.
   언급된 종목은 줄 앞에 **📡** 를 붙이고 맥락을 한 줄로, 안 된 종목은 "언급 없음".
   (✅·체크표시는 쓰지 마세요 — 방송 언급 표시는 📡 하나로 통일합니다.)

5) 🔎 **수급 · 변동성**:
   - **전일 수급주체 수급동향**: [수급 데이터]의 investors(기관·외국인·개인 순매수, 억원)
   - **순매수 top10 / 순매도 top10**: [수급 데이터]의 top
   - **ETF 등락 상위/하위**: [수급 데이터]의 etf
   - **VIX (미장·국장)**: 미장은 검증 시세의 VIX, 국장은 VKOSPI(코스피 변동성지수)가
     검증 시세에 있으면 사용하고 없으면 "확인 불가"로 적으세요
   - 수급 데이터가 비어 있으면 그 항목만 "조회 실패"로 적고 넘어가세요 (숫자 창작 금지)

### 리포트 ② 종목 기사검색 (===NEWS===)

대상은 **방송 캡처 화면에 등장한 종목**입니다([캡처 화면의 미장 종목] 목록 참고).
전사나 추측으로 종목을 늘리지 말고 그 목록과 [언급 종목 검증 시세]를 기준으로 쓰세요.

- **항목당 정확히 2줄**:
    1줄 `• **종목명**: 종가 아이콘 등락률 — 핵심 한 줄` (방송에서 언급됐으면 종목명 앞에 📡)
    2줄 `  🔗 [기사제목](url) · [기사제목](url)`
- 링크는 [언급 종목 검증 시세]의 `news` 배열과 [뉴스 브리핑 기사]에 주어진 url만 사용.
  **url을 절대 새로 만들지 마세요.** 마크다운 링크 형식 `[제목](url)`을 반드시 지키세요
  (텔레그램에서 제목만 보이는 하이퍼링크로 변환됩니다).
- ⚠️ **[주요종목] 목록에 있는 종목을 먼저** 쓰고, 다 쓴 뒤 `---기타---` 를 한 줄로 넣고
  그 아래에 나머지 종목을 쓰세요. `---기타---` 아래는 접기(펼치기) 블록이 되어
  눌러야 보입니다. 표식을 빠뜨리지 마세요.
- 겹치는 기사는 묶어 사안별 3줄 이내로 요약하세요."""

    session_focus = {
        "us": "이 방송은 전일 미국장 마감 리뷰입니다. 1번의 미국장 정리를 특히 두껍게 쓰세요.",
        "kr": "이 방송은 당일 한국장 개장 전 전망입니다. 1번의 국내장 전망과 3번(8시 전후 화면)을 "
              "특히 두껍게 쓰고, 미국 시황과의 연결 고리는 아래 '오늘 미국 세션 리포트'를 참고하세요.",
    }[session]
    session_goal = f"{session_focus}\n\n{common_order}"

    us_ctx = f"\n\n[오늘 미국 세션 리포트 (참고)]\n{us_context_md[:8000]}" if us_context_md else ""

    # 구간 전사(kr 07:50~08:05)는 '시황전망'만 뽑기 위한 것 — 수치 근거로 쓰면 안 된다
    twin = scfg.get("transcribe_window")
    tr_note = (
        f" — {twin[0]}~{twin[1]} 구간만 전사. ⚠️ **1번 시황전망 요약에만** 사용하세요. "
        "종목 시세·수치는 화면 캡처와 검증 시세가 우선이고, 전사 전문을 리포트에 옮기지 마세요"
    ) if (twin and transcript) else ""

    src_desc = "방송 화면 캡처 판독 결과" + ("와 음성 전사" if transcript else "")
    prompt = f"""당신은 한국 개인투자자를 위한 시황 애널리스트입니다.
삼프로TV {label} 방송({today})의 {src_desc}, 그리고 API로 검증한 실제 시세를 바탕으로 데일리 리포트를 작성하세요.
리포트의 핵심 축은 **① 방송 화면에서 읽은 내용 ② 종목 시세 ③ 관련 뉴스 링크** 입니다.

{session_goal}

가독성 규칙 (⚠️ 실물 스크린샷에서 가장 크게 지적된 부분):
- **지수·종목 나열은 반드시 줄바꿈된 `•` 불릿 한 줄에 하나씩.** 여러 종목의 등락률을
  한 문단에 이어쓰지 마세요 — "미국장 정리"·"국내장 전망"이 통짜 문단으로 붙어 나와
  읽을 수 없다는 지적을 받았습니다.
- 서술 문장은 **2~3줄마다 문단을 끊고**, 종목별 수치는 문장이 아니라 불릿 목록으로.
- **마크다운 표(`| ... |`)를 쓰지 마세요.** 텔레그램이 렌더링하지 못해 깨져 보입니다 → 불릿으로.
- 소제목은 `**미국장 정리**`처럼 굵게 한 줄로 두고 그 아래에 불릿을 붙이세요.

화법 규칙 (⚠️ 실제 수신 리포트 원문 대조로 지적된 "사족" 패턴 — 위 가독성 규칙이
구조를 고쳤다면 이건 문장 자체의 장황함을 없애는 규칙입니다):
- **서술형 도입 문단 금지**: "지난 주말 미국 증시는 다우존스와 S&P500이 소폭
  상승했으나..." 처럼 표(2번 섹션)에 이미 있는 수치를 문장으로 다시 풀지 마세요.
  코멘트는 원인·해석 한 줄로 압축: "인텔 호실적에도 파운드리 고객 확보 의구심 +
  차익실현으로 반도체 약세".
- **완곡 표현 금지**: "~것으로 예상됩니다/분석됩니다/보일 가능성이 높습니다" 같은
  표현을 반복하지 말고 단정형 명사구로 끝맺으세요 ("하방 압력이 예상됩니다" →
  "하방 압력").
- **진행자 인용은 풀어쓰지 말고 압축**: "진행자는 ~라고 언급했습니다" 대신
  `종목/이슈: "핵심 인용" — 결론` 한 줄로 쓰세요.
  예: `코스닥: "저점매수 어려움" — 활성화 대책 9월 연기, 안정화 대책 시급`.
- **차트 설명은 데이터 포인트만**: "이동평균선이 하락 추세를 지지" 같은 해설 문장 없이
  고점·현재가 등 숫자 2개만 남기세요.
- **불확실성 유보 문장은 괄호로 압축**: "구체적 기업명이 공개되지 않아 영향이
  제한적입니다" → "메타 공급계약(기업명 미공개, 영향 제한적)".
- **대립되는 해석은 나열하지 말고 결론형으로**: 인용을 늘어놓지 말고 한 줄 대비로.
  예: "원화 강세, 배경 해석 엇갈림(한은: 수급 vs 진행자: 정치적 가능성)".

공통 요구사항:
- ⚠️ **섹션 순서는 위 1~5를 절대 지키세요.** 화면 캡처는 **3번 자리**입니다 —
  리포트 맨 앞에 두지 마세요. 1번(요약)부터 순서대로 시작해야 합니다.
- 화면 캡처(3번)는 **캡처 1장당 2줄**:
    1줄: `**HH:MM** · 종목명들` 다음 줄에 그 화면의 핵심 내용 한 줄
    2줄: `🔗 [종목명](기사url) · [종목명](기사url)`
  시각은 `06:03`처럼 **짧게(HH:MM)** — 초 단위나 `[00:03:15]` 같은 표기는 쓰지 마세요.
  아래 [방송 화면 캡처] 블록에 이미 이 형식으로 정리돼 있으니 **그 내용과 링크를
  그대로 활용**하고, url을 새로 만들지 마세요.
- **`화면 원문`으로 표시된 캡처(07:45~08:10 요약 슬라이드)는 요약하지 말고
  화면의 줄 구성을 그대로** 옮기세요 — 항목 순서·구분자·수치를 원문대로 유지.
  단 **시세 표처럼 줄이 매우 많은 화면은 상위 15줄까지만** 옮기고 `…(이하 생략)`으로
  줄이세요. 원문 보존이 다른 섹션(1·2·4·5)을 밀어내면 안 됩니다.
- 💹 주요 지수/자산 변동 섹션 포함 (아래 검증 시세 사용)
- 📡 보유종목 언급 체크: 아래 보유/관심 종목이 방송에서 언급됐는지 확인. 언급됐으면 어떤 맥락인지, 안 됐으면 "언급 없음"으로.
- ⚠️ **ETF는 리포트에 넣지 마세요.** 삼프로TV는 시황에서 ETF를 다루지 않고 화면의
  ETF(KODEX·TIGER·ACE·PLUS·SOL 등, 커버드콜·인버스·레버리지 포함)는 전부 협찬 광고입니다.
  단 5번의 'ETF 등락 상위/하위'는 [수급 데이터]에서 온 시장 통계이므로 예외입니다.
- 가격·등락률은 반드시 [검증 시세]를 우선하고, 화면 숫자는 검증 실패 시에만 "(방송 화면 기준)"을 붙여 사용
- **종가 기준일은 섹션 제목에 한 번만** 쓰세요 (예: `💹 주요 지표 (7/24 종가 기준)`).
  줄마다 `[2026-07-24 종가]`처럼 반복하면 지저분해집니다 — 절대 줄 끝에 붙이지 마세요.
  ⚠️ **줄 끝 날짜 표기는 `stale: true`인 항목에만** 붙이세요 — asof가 대표 기준일과
  하루이틀 다른 건 정상(지표마다 발표 시각이 다를 뿐)이라 예외 표기 대상이 아닙니다.
  `stale: true`인 항목만 `⚠️(7/16 기준)`처럼 자기 날짜를 병기하고 최신 값이 아님을 밝히세요.
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
본문은 **마크다운 한 벌만** 쓰면 됩니다 — 텔레그램용/옵시디안용을 따로 쓰지 마세요
(시스템이 각각의 형식으로 자동 변환합니다).

===TITLE===
오늘 방송 핵심 키워드 한 단어~구 (파일명용, 예: 반도체급등)
===SIHWANG===
리포트 ① 시황 — 위 1~5번 섹션. `##` 섹션 헤더 사용 가능, 표는 금지.
분량은 3,500자 안팎(길면 설명 문장을 줄이고 수치·링크를 남기세요).
마지막 줄에 디스클레이머를 넣으세요.
===NEWS===
리포트 ② 종목 기사검색 — 주요종목 먼저, 그다음 `---기타---` 한 줄, 그 아래 나머지 종목.
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

[캡처 화면의 미장 종목 — ===NEWS=== 종목 기사검색의 대상]
{", ".join(capture_us) if capture_us else "(캡처에서 미국 종목을 찾지 못함 — ===NEWS===는 '해당 없음'으로)"}

[주요종목 — ===NEWS===에서 `---기타---` **위쪽**에 쓸 종목 (보유·관심 ∪ 캡처 2회 이상)]
{", ".join(majors) if majors else "(없음 — 전부 ---기타--- 아래로)"}

[화면 캡처 원문 판독 (광고 제외, 시간순)]
{_material_digest(vision_results)}
{f"{chr(10)}[뉴스 브리핑 기사 — 겹치는 것 묶어 3줄 요약]{chr(10)}" + _briefing_lines(news_briefing) if news_briefing else ""}
{f"{chr(10)}[수급 데이터 (단위 억원, 없는 항목은 '조회 실패'로만 표기)]{chr(10)}" + json.dumps(flows, ensure_ascii=False) if flows else ""}
{f"{chr(10)}[음성 전사{tr_note}]{chr(10)}" + transcript[:40000] if transcript else ""}{us_ctx}"""

    # LLM이 완전히 불가능해도(할당량·빌링·모델 오류) 리포트는 반드시 발행한다.
    # 캡처·전사·시세는 이미 확보돼 있으므로 그날 작업을 통째로 버리지 않는다.
    try:
        # 33개 지표 + M7 표 + 6개 섹션을 요구하므로 출력 분량이 크다.
        # 12000으로는 사고 토큰까지 겹쳐 응답이 잘렸다(7/25 실측) → 넉넉하게 확보.
        data = _parse_sections(_call_llm(settings["models"], prompt, max_tokens=40000))
        for key in ("title_keyword", "markdown_report"):
            if not data.get(key):
                raise RuntimeError(f"리포트 생성 결과에 {key} 누락")
        if not data["reports"]["sihwang"].strip():
            raise RuntimeError("리포트 생성 결과에 시황 본문(===SIHWANG===) 누락")
    except Exception as e:
        log.error("LLM 리포트 생성 실패 → 원자료 기반 열화 리포트로 전환: %s", e)
        data = _fallback_report(
            settings, session, vision_results, transcript, indices,
            verified_mentions, holdings_data, holdings_quotes, reason=str(e)[:200],
            news_briefing=news_briefing, flows=flows,
        )

    out_file = out_dir / "report.json"
    out_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(data["markdown_report"], encoding="utf-8")
    log.info("리포트 생성 완료: 키워드=%s", data["title_keyword"])
    return data
