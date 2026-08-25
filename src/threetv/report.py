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


def _clean_digest_fallback(vision_results: list[dict], limit_chars: int = 4000) -> str:
    """LLM 없이 **사람에게 그대로 보여줄** 자료화면 요약.

    `_material_digest()`는 LLM 프롬프트용이라 `<종목표시: ...>` 같은 내부 태그를
    그대로 남긴다 — LLM이 이해하고 자연어로 풀어써 주는 걸 전제하기 때문이다.
    그런데 리포트 생성이 통째로 실패하는 열화 경로에서는 이 원문이 **치환 없이
    그대로 텔레그램에 노출**되는 버그가 있었다(2026-07-28 실측, night-digest
    슬롯 데이터 부족으로 열화 전환된 실제 발송 리포트에서 확인). 그래서
    ① 태그를 벗겨 읽을 수 있는 불릿으로, ② 프레임마다 반복되는 동일 텍스트는
    한 번만, ③ 지표는 슬롯의 마지막(최신) 프레임 값만 한 줄로 압축한다.
    """
    if not vision_results:
        return "(자료화면 없음)"
    seen_text: set[str] = set()
    lines: list[str] = []
    last_stocks: list[str] = []
    for r in vision_results:
        ts = r.get("timestamp_sec", 0)
        mm, ss = divmod(int(ts), 60)
        t = (r.get("text") or "").strip()
        if t and t not in seen_text:
            seen_text.add(t)
            lines.append(f"[{mm:02d}:{ss:02d}] {t}")
        stocks = [
            f"{s.get('name')} {s.get('price') or ''} {s.get('change') or ''}".strip()
            for s in (r.get("stocks") or []) if s.get("name")
        ]
        if stocks:
            last_stocks = stocks
    if last_stocks:
        lines.append("· " + " · ".join(last_stocks))
    return ("\n".join(lines) or "(자료화면 없음)")[:limit_chars]


_MARK_RE = re.compile(r"===([A-Z]+)===")

# ===NEWS=== 안에서 '주요종목'과 '그 외'를 가르는 표식. 그 외는 접기 블록으로 내린다.
NEWS_REST_MARK = "---기타---"

# 시황 본문에서 "여기부터는 접어라"를 표시하는 마커들. LLM이 마커만 찍고 코드가
# 접는 구조 — LLM에게 `<<<FOLD:...>>>` 문법을 직접 시키면 형식이 자주 깨진다.
FLOW_DETAIL_MARK = "---수급상세---"     # 개인·외국인·기관 요약 아래의 세부 주체/TOP10
US_CTX_MARK = "---미장참고---"          # kr 리포트에 딸려오는 미국장 내용
CAPTURE_REST_MARK = "---시세상세---"    # 캡처화면 정리(3번)에서 상위 3종목 외 나머지


# 공용 프롬프트의 번호 섹션 헤딩("1) 📌 ...", "2) 💹 ..." 등) — `_fold_after`가
# 다음 섹션 시작 전에서 멈추는 경계로 쓴다.
_SECTION_HEAD_RE = re.compile(r"^\s*(?:#{1,6}\s*)?\d+\)\s", re.M)


def _fold_after(md: str, mark: str, title: str) -> str:
    """`mark` 다음 줄부터 **다음 번호 섹션 시작 전까지**를 접기 블록으로 내린다.

    ⚠️ 마커 뒤 전부를 무조건 접으면 안 된다 — `---미장참고---`는 1번 섹션
    중간(국내장 전망 다음)에 오는데, 그 뒤에 2)~5) 섹션이 더 있다. "전부 접기"로
    구현했더니 3)·5) 섹션 전체가 미국장 접기 블록 안으로 빨려 들어갔다
    (2026-08-01 로컬 재현: 캡처 정리·수급 섹션이 통째로 사라짐). 다음 번호 헤딩을
    만나면 그 앞에서 접기를 끊고, 헤딩부터는 원래 자리에 그대로 남긴다.
    마커가 파일 끝부분(예: 5번 섹션의 `---수급상세---`)에 있으면 다음 헤딩이
    없으니 기존처럼 끝까지 접는다.
    """
    if mark not in md:
        return md
    head, rest = md.split(mark, 1)
    m = _SECTION_HEAD_RE.search(rest)
    tail = ""
    if m:
        tail, rest = rest[m.start():].strip(), rest[:m.start()]
    rest = rest.strip()
    if not rest:
        out = head.strip()
    else:
        out = f"{head.strip()}\n\n{tg_format.fold(title, rest)}"
    if tail:
        out = f"{out}\n\n{tail}"
    return out


_LINK_ROW_RE = re.compile(r"^\s*🔗 .*$", re.M)


def _fold_link_rows(md: str, title: str = "캡처 화면 관련 기사 링크") -> str:
    """본문에 흩어진 `🔗 A · B · C` 링크 나열 줄을 걷어 **맨 뒤 접기 블록 하나**로 모은다.

    캡처 정리 섹션은 화면 내용(주요 내용) 사이사이에 링크 줄이 끼어 있어 읽는 흐름이
    끊긴다(2026-08-01 실물 지적). 링크는 버리지 않고 한곳에 모아 접는다.
    LLM 출력·열화 출력 어디에나 적용되도록 **후처리**로 구현했다 — 프롬프트 지시에
    기대면 지키지 않는 날 그대로 나간다.
    """
    rows = [m.group(0).strip() for m in _LINK_ROW_RE.finditer(md)]
    if len(rows) < 2:          # 1줄뿐이면 접는 게 오히려 번거롭다
        return md
    body = _LINK_ROW_RE.sub("", md)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return f"{body}\n\n{tg_format.fold(f'{title} ({len(rows)}건)', chr(10).join(rows))}"


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


_ITEM_START_RE = re.compile(r"^•\s")


def _fold_top_n(md: str, n: int, title: str) -> str:
    """상위 `n`개 항목(`•`로 시작하는 줄 + 뒤따르는 비-`•` 줄)만 본문에 남기고
    나머지는 접기 블록으로 내린다.

    데일리 정리(===DAILY===)처럼 LLM이 별도 마커를 찍지 않는 섹션용 — 항목 순서
    자체가 이미 중요도순(프롬프트가 "상위 6~8개, 중요도 순"으로 요청)이라, 개수만
    세어 자르면 되고 LLM의 마커 준수 여부에 기대지 않아도 된다(2026-08-09,
    "top3 제외 접기" 요청).
    """
    lines = md.splitlines()
    items: list[list[str]] = []
    for line in lines:
        if _ITEM_START_RE.match(line):
            items.append([line])
        elif items:
            items[-1].append(line)
        # 첫 항목 전의 잡음(빈 줄 등)은 버린다 — DAILY/NEWS는 항상 불릿으로 시작
    if len(items) <= n:
        return md
    head = "\n".join(chr(10).join(item) for item in items[:n])
    rest = "\n".join(chr(10).join(item) for item in items[n:])
    return f"{head}\n\n{tg_format.fold(f'{title} ({len(items) - n}건 더) — 눌러서 펼치기', rest)}"


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


def _cap_news_head(news_md: str, n: int) -> str:
    """`---기타---` 위(접기 밖, 노출) 항목을 최대 `n`개로 코드가 강제로 자른다.

    us/kr 세션 종목기사(===NEWS===) 전용, 세션별로 `n`이 다르다(us=5, kr=2 —
    호출부인 `_parse_sections` 참고). kr은 미국 지수·종목이 별도 미국 세션
    리포트와 중복 노출되지 않게 [주요종목] 자리를 2개로 제한하라고 프롬프트로
    지시했지만, `_fold_top_n`과 같은 이유로 LLM의 준수 여부에 기대지 않는다
    (2026-08-18 요청: "한국기사는 2개외 접기"). us는 원본 출처라 중복 회피
    목적은 없고 단순히 리포트가 길어 가독성 때문에 5개로 제한한다(2026-08-24
    요청). 넘친 항목은 버리지 않고 `---기타---` 아래(기존 '기타' 항목 앞)로
    옮긴다.
    """
    head, _, rest = news_md.partition(NEWS_REST_MARK)
    items: list[list[str]] = []
    for line in head.splitlines():
        if _ITEM_START_RE.match(line):
            items.append([line])
        elif items:
            items[-1].append(line)
        # 첫 항목 전의 잡음(빈 줄 등)은 버린다 — _fold_top_n과 동일한 관례
    if len(items) <= n:
        return news_md
    kept = "\n".join(chr(10).join(item) for item in items[:n])
    overflow = "\n".join(chr(10).join(item) for item in items[n:])
    rest = rest.strip()
    merged_rest = f"{overflow}\n\n{rest}" if rest else overflow
    return f"{kept}\n\n{NEWS_REST_MARK}\n{merged_rest}"


# LLM이 프롬프트 지시를 무시하고 검색 결과 페이지 URL을 "기사"로 만들어낼 때의
# 최후 안전장치 — 실제 기사가 아닌 검색 페이지 도메인/경로 패턴만 잡는다.
_SEARCH_URL_RE = re.compile(
    r"\[([^\]\n]+)\]\("
    r"(?:https?://)?(?:www\.)?"
    r"(?:search\.naver\.com/search\.naver"
    r"|finance\.yahoo\.com/quote/[^)/]+/news/?"
    r"|(?:www\.)?google\.com/search)"
    r"[^)\n]*\)"
)


def _strip_search_links(md: str) -> str:
    """`[제목](검색결과URL)` → `제목` — 링크만 벗기고 텍스트는 남긴다.

    검색 결과 페이지는 기사가 아니다. `market.fetch_news()`가 더 이상
    `search_links()`를 자동으로 붙이지 않지만, LLM이 프롬프트를 무시하고 직접
    검색 URL을 만들어낼 가능성은 남아있어 출력 직전에 한 번 더 걸러낸다.
    """
    if not md:
        return md
    return _SEARCH_URL_RE.sub(lambda m: m.group(1), md)


def _parse_sections(text: str, session: str = "") -> dict:
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

    # 가독성 후처리 (2026-08-01 요청) — 마커 기반 접기는 LLM이 마커를 안 찍으면
    # 원문 그대로 통과하고, 링크 줄 접기는 마커 없이 후처리라 항상 적용된다.
    sihwang = _fold_after(sihwang, FLOW_DETAIL_MARK,
                          "수급 상세 (그외 주체 · TOP10 · ETF 등락)")
    sihwang = _fold_after(sihwang, US_CTX_MARK, "미국장 참고")
    sihwang = _fold_after(sihwang, CAPTURE_REST_MARK, "그 외 종목 시세")
    sihwang = _fold_link_rows(sihwang)

    news_raw = sec.get("NEWS", "")
    daily_raw = sec.get("DAILY", "").strip()

    # 저장(옵시디안 아카이브)용 — 접지 않고 전부 남긴다. 이 파일은 나중에 kr 세션이
    # read_us_section_today()로 다시 읽어 컨텍스트로 쓰는데, 접힌 채로 저장하면
    # (텔레그램 expandable/옵시디안 callout 문법) 그 재사용 경로에서 종목이 누락된
    # 것처럼 취급될 위험이 있다(2026-08-09 사용자 확정 — "저장시는 풀고저장해서
    # 나중요약 학습시에는 전종목 검색되게").
    news_archive = news_raw
    if daily_raw:
        news_archive += f"\n\n### 📰 데일리 주요 종목기사 정리\n{daily_raw}"

    # 텔레그램용 — 상위 항목만 보이고 나머지는 눌러서 펼치는 접기 블록으로.
    news_for_telegram = news_raw
    if session == "kr":
        # 미국 지수·종목이 별도 미국 세션 리포트와 중복 노출되지 않도록 노출
        # 자리를 2개로 강제(archive는 건드리지 않음 — 위 news_archive 참고).
        news_for_telegram = _cap_news_head(news_for_telegram, 2)
    elif session == "us":
        # 2026-08-24 사용자 요청: us 세션 종목기사도 너무 길어 상위 5개만
        # 노출하고 나머지는 접기로. kr(2개)보다 넉넉한 건 kr과 달리 여기가
        # 그 종목들의 원본 출처라 중복 회피 목적이 아니라 단순 가독성용이기
        # 때문(archive는 건드리지 않음 — 위 news_archive 참고).
        news_for_telegram = _cap_news_head(news_for_telegram, 5)
    news_telegram = _fold_news_rest(news_for_telegram)
    if daily_raw:
        news_telegram += (f"\n\n### 📰 데일리 주요 종목기사 정리\n"
                          f"{_fold_top_n(daily_raw, 3, '그 외 이슈')}")

    parts = [sihwang] + ([news_archive] if news_archive else [])
    return {
        "title_keyword": (sec.get("TITLE", "").splitlines() or [""])[0].strip(),
        # 하위호환 — 열화 경로·아카이브가 쓰는 통합 본문
        "telegram_text": sec.get("TELEGRAM") or sihwang,
        "markdown_report": "\n\n".join(parts),
        "holdings_mentioned": _parse_holdings_lines(sec.get("HOLDINGS", "")),
        # news: 저장용(접지 않음) / news_telegram: 전송용(접힘). 아카이브는 news를
        # 쓰고, 텔레그램 전송은 news_telegram이 있으면 그걸 우선한다.
        "reports": {"sihwang": sihwang, "sihwang_md": sihwang,
                   "news": news_archive, "news_telegram": news_telegram},
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


def _briefing_grouped(news_briefing: list[dict] | None) -> str:
    """수집 기사를 종목(query)별로 묶어 표시 — 열화(LLM 없음) 경로의 데일리 정리용.

    LLM이 없으면 이슈 단위 요약(===DAILY===)을 만들 수 없으니 그 대신 종목별로
    실제 기사 원문 목록을 보여준다. `news.py`가 이미 최신순으로 정렬해 넘긴다.
    """
    if not news_briefing:
        return ""
    by_query: dict[str, list[dict]] = {}
    for n in news_briefing:
        by_query.setdefault(n.get("query") or "기타", []).append(n)
    blocks = []
    for q, items in by_query.items():
        lines = [f"**{q}**"]
        for n in items:
            when = f" ({n['published_kst']})" if n.get("published_kst") else ""
            lines.append(f"• [{n['title']}]({n['url']}){when}")
            if n.get("summary"):
                lines.append(f"  {n['summary'][:160]}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


VERBATIM_MAX_LINES = 15         # '화면 원문' 보존 시 한 화면당 최대 줄 수
VERBATIM_MAX_LINES_SUMMARY = 30  # 매일 나오는 요약 슬라이드(_SUMMARY_ANCHORS)는 더 길게 허용

# 개별 종목 시세판을 가려내는 단서 — 이런 화면은 8시 전후 분석에서 제외한다
# (사용자 확정 2026-07-26: 08시 전후는 흰 배경 '그림' 슬라이드만 보고 개별 주가는 미적용)
_QUOTE_TABLE_HINTS = ("현재가", "거래량", "전일대비", "등락률", "체결")

# 매일 나오는 3종 요약 슬라이드의 제목 — 이 앵커가 있으면 종목이 몇 개 나열됐든
# 시세판이 아니라 방송 본 자료다. 2026-07-28 실측: "전일, 해외/국내 시장 흐름 및
# 특징" 슬라이드가 종목을 16~17개 언급한다는 이유만으로 _is_quote_table()의
# "종목 12개 이상" 규칙에 걸려 매일 통째로 캡처 정리에서 빠지고 있었다.
_SUMMARY_ANCHORS = ("흐름 및 특징", "특징:", "특징 :", "시황 전망", "투자 대응", "시장 흐름")


def _is_summary_slide(text: str) -> bool:
    t = text or ""
    return any(a in t for a in _SUMMARY_ANCHORS)

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

    ⚠️ 2026-07-28 실측: "전일, 해외/국내 시장 흐름 및 특징" 요약 슬라이드가
    16~17개 종목을 한 줄씩 나열한다는 이유만으로 아래 종목-개수 규칙에 걸려
    매일 통째로 빠졌다. `_is_summary_slide()`(제목 앵커)에 걸리면 종목 수와
    무관하게 시세판이 아니라고 먼저 확정한다 — 진짜 시세판(헤더 단어 2개↑ +
    쉼표 구분 행 5줄↑)은 이 앵커가 없으므로 그 판정은 그대로 유지된다.
    """
    if _is_summary_slide(text):
        return False
    t = text or ""
    hits = sum(1 for h in _QUOTE_TABLE_HINTS if h in t)
    rows = [ln for ln in t.splitlines() if ln.count(",") >= 3]
    # 시세표 헤더 단어가 2개 이상 + 쉼표 구분 행이 여러 줄이면 시세판
    if hits >= 2 and len(rows) >= 5:
        return True
    # 헤더를 못 읽었더라도 한 화면에 종목이 과도하게 많으면 시세판으로 본다
    return len(stocks or []) >= 12


def _delta_str(d: float | None) -> str:
    """전일대비 증감 — 없으면 빈 문자열."""
    if d is None:
        return ""
    return f" (전일比 {'+' if d > 0 else '−'}{abs(d):,.0f})"


def _flow_lines(flows: dict | None) -> str:
    """수급 데이터를 사람이 읽는 줄로 (LLM 요약 실패 시에도 원자료가 남게).

    종목이 여러 개인 목록은 **한 줄에 하나씩** 쓴다 — 쉼표로 이어붙이면 텔레그램에서
    통째로 한 문단이 돼 읽을 수가 없다(2026-07-29 실물 스크린샷 지적).
    """
    if not flows:
        return ""
    parts: list[str] = []      # 항상 펼쳐 보이는 개요
    detail: list[str] = []     # 접어 두는 상세

    # ① 개인·외국인·기관 요약 (전일대비 증감 포함) — 가장 먼저 본다.
    #    나머지 주체(연기금·투신·사모…)와 TOP10은 상세로 내린다(2026-08-01 요청).
    summary = flows.get("summary") or {}
    if summary.get("main"):
        d = summary.get("date", "")
        head = f"• 수급 요약 ({d[4:6]}/{d[6:8]} 순매수, 억원)" if len(d) == 8 \
            else "• 수급 요약 (순매수, 억원)"
        parts.append(head)
        for r in summary["main"]:
            parts.append(f"  · {r['investor']}: {r['net']:+,.0f}{_delta_str(r.get('delta'))}")
        others = summary.get("others") or []
        if others:
            detail.append("• 그외 수급주체 (억원)")
            detail += [f"  · {r['investor']}: {r['net']:+,.0f}{_delta_str(r.get('delta'))}"
                       for r in others]
    else:
        inv = flows.get("investors") or []
        if inv:
            parts.append("• 수급주체 순매수(억원)")
            for r in inv[:8]:
                parts.append(f"  · {r['investor']}: {r['net']:+,.0f}")

    top = flows.get("top") or {}
    for key, label in (("buy", "순매수"), ("sell", "순매도")):
        if top.get(key):
            detail.append(f"• {top.get('investor','')} {label} TOP (억원)")
            detail += [f"  · {r['name']}: {r['net']:+,.0f}" for r in top[key][:10]]

    etf = flows.get("etf") or {}
    for key, label in (("up", "상승"), ("down", "하락")):
        if etf.get(key):
            detail.append(f"• ETF {label} TOP")
            detail += [f"  · {r['name']}: {r['pct']:+.2f}%" for r in etf[key][:10]]

    out = "\n".join(parts)
    if detail:
        out += "\n\n" + tg_format.fold("수급 상세 (그외 주체 · TOP10 · ETF 등락)",
                                       "\n".join(detail))
    return out


def _capture_blocks(
    vision_results: list[dict],
    verified_mentions: list[dict],
    base_kst: str,
    verbatim_window: list | None = None,
    news_briefing: list[dict] | None = None,
) -> str:
    """캡처 화면당 2줄 — ① 시각·종목·타이틀 ② 연관 기사 링크.

    verbatim_window(예: 07:45~08:10 요약 슬라이드) 안의 화면은 압축하지 않고
    **원문 줄 구성을 그대로** 옮긴다. 그 화면은 배치 자체가 정보이기 때문이다.
    단 그 구간의 **개별 종목 시세판은 제외**한다 (사용자 확정: 08시 전후는 흰 배경
    그림 슬라이드만 보고 개별 주가는 반영하지 않는다).

    기사 링크는 `verified_mentions[].news`(검증 시세와 함께 조회한 실제 기사) →
    `news_briefing`(종목명으로 미리 검색해둔 실제 기사, `query` 필드로 매칭) 순으로
    찾는다. 둘 다 없으면 **링크 없이 종목명만** — 검색 결과 페이지 URL로 대체하지
    않는다(2026-07-27 실측: `verified_mentions`가 비면 캡처 링크가 전부
    `search.naver.com` 검색 URL로 떨어졌다. `us_stocks_in_captures()`가
    `news_briefing`의 1순위 검색 대상이라(main.py) 캡처 종목의 실제 기사는
    이미 브리핑에 들어 있다 — 여기서 연결만 하면 된다).
    """
    news_map = {
        (v.get("name") or "").strip(): v.get("news") or []
        for v in verified_mentions if v.get("name")
    }
    briefing_map: dict[str, list[dict]] = {}
    for n in news_briefing or []:
        q = (n.get("query") or "").strip()
        if q:
            briefing_map.setdefault(q, []).append(n)
    blocks: list[str] = []
    skipped_tables = 0
    seen_text: set[str] = set()
    skipped_dupes = 0
    for r in vision_results:
        clock = _clock(base_kst, r.get("timestamp_sec", 0))
        text = (r.get("text") or "").strip()
        stocks = [s for s in (r.get("stocks") or []) if s.get("name")]
        names = [str(s["name"]).strip() for s in stocks]

        # 같은 화면이 여러 프레임에 걸쳐 잡히면 리포트에 똑같은 줄이 반복된다
        # (2026-07-29 us 실측: Russell 2000·Micron 헤드라인이 각각 2번씩 실렸다).
        # 공백만 다른 동일 텍스트는 처음 것만 남긴다.
        key = " ".join(text.split())
        if key and key in seen_text:
            skipped_dupes += 1
            continue
        if key:
            seen_text.add(key)

        # 8시 전후 구간의 개별 종목 시세판은 분석 대상이 아니다
        if _in_window(clock, verbatim_window) and _is_quote_table(text, stocks):
            skipped_tables += 1
            continue

        if _in_window(clock, verbatim_window) and text:
            # 화면 그대로 — 줄 구성 보존. 단 시세 표처럼 줄이 매우 많은 화면은
            # 상위 일부만 (2026-07-26 실측: 50줄 넘는 종목 시세판이 리포트를 잠식했다)
            # 매일 나오는 요약 슬라이드(_is_summary_slide)는 20줄짜리도 있어 더 길게 허용
            max_lines = VERBATIM_MAX_LINES_SUMMARY if _is_summary_slide(text) else VERBATIM_MAX_LINES
            lines = text.splitlines()
            shown = lines[:max_lines]
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

        # 2번째 줄: 연관 기사 링크 (종목별 1건씩). 실제 기사가 없으면 그 종목은
        # 링크 줄에서 빠진다 — 종목명 자체는 이미 위쪽 head 줄에 있다.
        links: list[str] = []
        for s in stocks[:4]:
            nm = str(s["name"]).strip()
            items = news_map.get(nm) or briefing_map.get(nm)
            if items:
                n = items[0]
                links.append(f"[{nm}]({n['url']})")
        if links:
            blocks.append(f"🔗 {' · '.join(links)}")
    if skipped_tables:
        log.info("8시 전후 개별 종목 시세판 %d장 제외 (흰 배경 슬라이드만 분석)",
                 skipped_tables)
    if skipped_dupes:
        log.info("동일 화면 캡처 %d장 중복 제거", skipped_dupes)
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


def mention_line(m: dict) -> str:
    """관심종목 한 줄 — 방송 언급 여부를 이모지 하나로만 표시한다.

    종전에는 언급되면 `📡 + 맥락`, 아니면 `언급 없음`을 적었다. 그런데 대부분의
    종목은 언급되지 않아 "(언급 없음)"이 줄마다 반복되며 화면에서 줄바꿈만
    늘렸다(2026-08-02 실측: 13줄 중 11줄). 이제 언급된 종목만 🎤를 달고,
    언급되지 않은 종목에는 아무 표시도 붙이지 않는다.
    """
    return f"• {'🎤 ' if m.get('mentioned') else ''}{m['name']}"


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
        news_briefing=news_briefing,
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
        mention_line(m) for m in holdings_mentioned
    ) or "• 대조할 보유종목 없음"

    # ── 1건: 시황 (요약 자리엔 원자료 배너 + 지표 + 캡처)
    idx_title = f"💹 주요 지표 ({idx_asof})" if idx_asof else "💹 주요 지표"
    hold_title = f"💼 보유종목 시세 ({hold_asof})" if hold_asof else "💼 보유종목 시세"
    sihwang_parts = [banner, f"\n### {idx_title}\n{idx_block}"]
    if captures:
        # 링크 나열 줄은 화면 내용 사이에 끼면 읽는 흐름을 끊는다 — 뒤로 모아 접는다
        sihwang_parts.append(
            f"\n### 🖼 방송 화면 캡처 (종목 · 관련 기사)\n{_fold_link_rows(captures)}")
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
    # 저장(옵시디안 아카이브)용 — 접지 않고 head/rest를 그대로 이어붙인다(2026-08-09
    # 확정 — 나중에 read_us_section_today() 등이 재사용할 때 전종목이 검색돼야 함).
    news_archive_body = "\n".join(head_lines) or "추출 실패(LLM 필요)"
    if rest_lines:
        news_archive_body += "\n" + "\n".join(rest_lines)
    news_archive = f"### 📈 방송 언급 종목 · 관련 기사\n{news_archive_body}"

    # 텔레그램용 — 주요종목은 노출, 그 외는 접기
    news_telegram_body = "\n".join(head_lines) or "추출 실패(LLM 필요)"
    if rest_lines:
        news_telegram_body += f"\n\n{NEWS_REST_MARK}\n" + "\n".join(rest_lines)
    news_telegram = f"### 📈 방송 언급 종목 · 관련 기사\n{_fold_news_rest(news_telegram_body)}"

    # LLM이 없어 이슈 단위 요약(===DAILY===)은 만들 수 없다 — 대신 종목별로
    # 실제 기사 원문을 묶어 보여준다(중복 제거·최신순 정렬은 news.py가 이미 처리)
    briefing_grouped = _briefing_grouped(news_briefing)
    if briefing_grouped:
        briefing_title = "### 📰 데일리 기사 정리 (AI 요약 없음 — 종목별 원문 목록)\n"
        news_archive += f"\n\n{briefing_title}{briefing_grouped}"
        news_telegram += f"\n\n{briefing_title}" + tg_format.fold(
            f"오늘 수집 기사 {len(news_briefing or [])}건 — 눌러서 펼치기",
            briefing_grouped,
        )

    log.warning("열화 리포트 생성 (LLM 없이 원자료 기반): %s / 화면 %d장, 지표 %d건, 전사 %d자",
                label, len(vision_results), len(indices), len(transcript))
    return {
        "title_keyword": "원자료시황",
        "telegram_text": sihwang_md,
        "markdown_report": f"{sihwang_full}\n\n{news_archive}",
        "holdings_mentioned": holdings_mentioned,
        # sihwang=텔레그램용(발췌) / sihwang_md=옵시디안용(전사 전문)
        # news=저장용(접지 않음) / news_telegram=전송용(접힘)
        "reports": {"sihwang": sihwang_md, "sihwang_md": sihwang_full,
                   "news": news_archive, "news_telegram": news_telegram},
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
        # 4000으로는 gemini-2.5의 사고 토큰과 겹쳐 최대 40개 종목 JSON이 잘렸다
        # (2026-07-26/27 실측: 확보 7,626자/8,922자에서 MAX_TOKENS로 두 모델 다 실패
        #  → verified_mentions=[] → 캡처 링크가 전부 검색 URL로 떨어짐)
        data = _parse_json_obj(_call_llm(models, prompt, max_tokens=12000))
        mentions = data.get("mentions", [])
        log.info("언급 종목 추출: %d개", len(mentions))
        return mentions
    except Exception as e:
        log.warning("종목 추출 실패(리포트는 계속 진행): %s", e)
        return []


def _simple_report(
    settings: dict,
    prompt: str,
    fallback_title: str,
    fallback_body: str,
    out_dir: Path,
) -> dict:
    """noon/night처럼 SIHWANG/NEWS 2분할이 필요 없는 단일 섹션 리포트 공통 골격.

    출력 형식은 `===TITLE===`/`===BODY===`/`===END===` — us/kr의 6구획 형식보다
    훨씬 가볍다. LLM 실패 시 `fallback_title`/`fallback_body`(호출부가 미리 만든
    원자료 기반 문구)로 대체해, us/kr과 마찬가지로 LLM이 완전히 막혀도 리포트가
    반드시 발행된다.
    """
    title, body = fallback_title, fallback_body
    try:
        text = _call_llm(settings["models"], prompt, max_tokens=6000)
        sec = _split_marked(text)
        t = (sec.get("TITLE", "").splitlines() or [""])[0].strip()
        b = sec.get("BODY", "").strip()
        if not t or not b:
            raise ValueError(f"구분선 형식이 아닙니다: {text[:200]}")
        title, body = t, b
    except Exception as e:
        log.error("리포트 생성 실패 → 원자료 기반 열화 리포트로 전환: %s", e)

    body = _strip_search_links(body)
    data = {"title_keyword": title, "telegram_text": body, "markdown_report": body,
            "reports": {"sihwang": body, "sihwang_md": body, "news": ""}}
    (out_dir / "report.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(body, encoding="utf-8")
    log.info("리포트 생성 완료: 키워드=%s", title)
    return data


def generate_noon_report(
    settings: dict,
    vision_results: list[dict],
    transcript: str,
    kr_indices: list[dict],
    out_dir: Path,
) -> dict:
    """12시 국내 시황(겸손은힘들다 "12시에 만나요") — 요약 + 장중 KR 지수만.

    us/kr과 달리 종목기사 검색·보유종목 체크가 없다(사용자 확정: 12:00~12:20
    주요 시황·화면캡처·전사 요약만, 장중 KR 지수 현황). 그래서 `generate_report()`
    전체를 타지 않고 `_simple_report()`로 가볍게 만든다.

    2026-07-28 사용자 확정(실제 방송 스크린샷): 오프닝 이후 8~12분대에 "정오의
    Money 뉴스" 고정 코너(코스피·코스닥·환율 수치 + 헤드라인 4줄)가 매일 나온다 —
    이게 이 리포트의 핵심 소스라 프롬프트에서 명시적으로 우선순위를 준다.
    """
    disclaimer = settings["report"]["disclaimer"]
    today = now_kst().strftime("%Y-%m-%d (%a)")
    material = _material_digest(vision_results)
    idx_asof = _asof_label(kr_indices)
    idx_title = f"장중 KR 지수 ({idx_asof})" if idx_asof else "장중 KR 지수"
    idx_block = "\n".join(_quote_line(q) for q in kr_indices) or "• 조회 실패"

    prompt = f"""당신은 한국 개인투자자를 위한 시황 애널리스트입니다.
겸손은힘들다 "12시에 만나요"({today}) 12:00~12:20 방송의 화면 캡처와 음성 전사를
바탕으로 짧은 장중 리포트를 쓰세요.

- ⚠️ **"정오의 Money 뉴스" 화면을 최우선으로 반영하세요** — 매일 나오는 고정
  코너로, 코스피·코스닥·환율 수치(예: "2026.07.28 12시00분 기준")와 헤드라인
  4줄(예: "中 DUV 노광장비 자체 생산설에 ASML 8%대 급락")로 구성됩니다.
  [방송 화면 캡처 원문]에 이 화면이 있으면 수치는 정확히, 헤드라인은 전부
  살려서 반영하세요 — 빠뜨리지 마세요.
- 📌 시황 요약: 오전장 흐름과 진행자 코멘트를 3~5줄로. 완곡 표현("~것으로 보입니다")
  대신 단정형 명사구로, 서술 문단이 아니라 불릿 위주로.
- 📰 정오의 Money 뉴스 헤드라인: 화면에 나온 헤드라인을 불릿으로 그대로.
- 💹 {idx_title}: 아래 검증 시세를 불릿으로 그대로 옮기세요(숫자를 새로 만들지 마세요).
- 표(`| |`)는 쓰지 마세요. 검색 결과 페이지 URL은 쓰지 마세요.
- 마지막 줄에 디스클레이머: {disclaimer}

출력 형식:
===TITLE===
오늘 12시 방송 핵심 키워드 (파일명용)
===BODY===
📌 시황 요약
...
📰 정오의 Money 뉴스 헤드라인
...
💹 {idx_title}
{idx_block}
...(디스클레이머로 끝)
===END===

[장중 KR 지수 검증 시세]
{json.dumps(kr_indices, ensure_ascii=False)}

[방송 화면 캡처 원문 — "정오의 Money 뉴스" 화면을 최우선으로 찾아 반영]
{material}

[음성 전사 (20분 전체)]
{transcript[:20000]}"""

    banner = "⚠️ *AI 요약 없음 — 원자료 기반 자동 리포트*\n"
    fallback_body = (
        f"{banner}📌 12시 국내 시황 ({today})\n{_clean_digest_fallback(vision_results)}\n\n"
        f"💹 {idx_title}\n{idx_block}\n\n{disclaimer}"
    )
    return _simple_report(settings, prompt, "12시시황", fallback_body, out_dir)


def generate_night_digest(
    settings: dict,
    slots: list[dict],
    out_dir: Path,
) -> dict:
    """22:00~06:00 야간 미장 라이브(오선의 미국 증시 라이브) 8슬롯 종합.

    슬롯 하나하나는 리포트를 만들지 않는다(캡처 5분·저장만) — 06시에 이 함수가
    `obsidian_archive.read_night_slots()`가 모아온 슬롯을 한 번에 종합해
    ① 지수 변동 궤적(장초반→장중→마감) ② 주요 이벤트 2~3건 결과를 만든다.
    """
    disclaimer = settings["report"]["disclaimer"]
    today = now_kst().strftime("%Y-%m-%d (%a)")

    def _slot_order(s: dict) -> int:
        # 22:00~05:59 구간이라 자정을 넘어간다 — 00~05시는 22~23시 다음으로
        # 취급해야 시간순이 된다 (문자열/숫자 그대로 정렬하면 00이 22보다 앞에 옴)
        try:
            h = int(s.get("hour_label", "0"))
        except ValueError:
            h = 0
        return h + 24 if h < 12 else h

    ordered_slots = sorted(slots, key=_slot_order)

    slot_texts = []
    for s in ordered_slots:
        material = _material_digest(s.get("vision_results") or [])
        if material:
            slot_texts.append(f"### {s.get('hour_label', '?')}시\n{material}")
    slots_block = "\n\n".join(slot_texts) or "(수집된 슬롯 없음)"

    fallback_slot_texts = []
    prev_clean = None
    for s in ordered_slots:
        clean = _clean_digest_fallback(s.get("vision_results") or [])
        if clean == "(자료화면 없음)":
            continue
        hour = s.get("hour_label", "?")
        if clean == prev_clean:
            # 화면이 안 바뀐 슬롯을 그대로 반복 표시하지 않는다(2026-07-28 실장애:
            # 인접 프레임이 거의 동일한 텍스트를 계속 반복해 열화 리포트가 벽글씨가 됨)
            fallback_slot_texts.append(f"### {hour}시\n(직전 슬롯과 동일 — 변동없음)")
        else:
            fallback_slot_texts.append(f"### {hour}시\n{clean}")
        prev_clean = clean
    slots_fallback_block = "\n\n".join(fallback_slot_texts) or "(수집된 슬롯 없음)"

    prompt = f"""당신은 한국 개인투자자를 위한 시황 애널리스트입니다.
"오선의 미국 증시 라이브"({today} 밤~오늘 새벽, 22:00~06:00 KST) 방송을 매시 정각
5분씩 캡처한 8개 슬롯 결과를 종합해 아침 리포트를 쓰세요. 슬롯은 시간순입니다.

- 📊 지수 변동 궤적: 화면에 나온 주요 지수(다우/나스닥/S&P500)가 장초반→장중→마감
  구간에서 어떻게 움직였는지 방향(상승/하락/보합) 흐름으로 요약하세요.
  예: "나스닥: 상승 → 하락 → 보합". 슬롯에 없는 구간은 "확인 불가"로.
- 📌 주요 이벤트: 슬롯 화면에 나온 미국 일일 주요 이벤트 2~3개를 골라
  각각 `• **이벤트명** — 결과 한 줄`로. 완곡 표현 금지, 단정형 명사구로.
- 표는 쓰지 마세요. 검색 결과 페이지 URL은 쓰지 마세요.
- 마지막 줄에 디스클레이머: {disclaimer}

출력 형식:
===TITLE===
오늘 야간 미장 핵심 키워드 (파일명용)
===BODY===
📊 지수 변동 궤적
...
📌 주요 이벤트
...
(디스클레이머로 끝)
===END===

[시간대별 슬롯 캡처 원문]
{slots_block}"""

    banner = "⚠️ *AI 요약 없음 — 원자료 기반 자동 리포트*\n"
    fallback_body = f"{banner}📊 야간 미장 슬롯 원문 ({today})\n{slots_fallback_block}\n\n{disclaimer}"
    return _simple_report(settings, prompt, "야간미장", fallback_body, out_dir)


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
    captures = _capture_blocks(vision_results, verified_mentions, base_kst, vwin,
                               news_briefing=news_briefing)
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

1) 📌 **3protv 요약** — 방송 핵심을 압축하고, 이어서 **오늘 국내장 전망**과 **미국장 정리**를
   각각 소제목으로 씁니다(이 순서로 — 국내장 먼저, 미국장은 참고자료라 뒤).
   - 국내장 전망: KOSPI·KOSDAQ 예상 방향과 근거, 반도체(삼성전자·SK하이닉스)에 미치는 영향,
     환율·외국인 수급 관점. **코스피 야간선물**은 방송 화면값 우선 → 없으면 EWY로 갈음(대용 명시)
     → 둘 다 없으면 "확인 불가"(추측 금지)
   - 미국장: 지수 흐름·주도 섹터·매크로 이슈. **kr 세션이면 이 소제목 바로 앞 줄에
     `---미장참고---` 한 줄만 단독으로 넣으세요**(다른 글자 없이) — 국내장 전망이
     먼저 읽히도록 미국장 부분을 접습니다. us 세션은 이 마커를 넣지 마세요
     (미국장이 본문 주제입니다).
   - 방송에서 진행자가 국내장 전망을 언급했다면 그 내용을 우선 반영

2) 💹 **주요 지표 전일대비 현황** — 종가 기준일은 섹션 제목에 한 번만
   (예: `주요 지표 (7/24 종가 기준)`). 순서는 방송 슬라이드와 1:1 대조되도록:
   - 3대 지수: 다우존스 / 나스닥 / S&P500
   - **국내 지수: KOSPI / KOSDAQ** — [주요 지수/자산 검증 시세]에 있으면 반드시
     이 줄에 포함하세요(다른 지수와 같은 형식: `KOSPI: 3,150.50 📉 ▼0.42%`).
     빠뜨리면 안 됩니다 — 2번 섹션 밖(1번 국내장 전망 서술문)에만 언급하고
     끝내지 마세요.
   - 지수선물: 나스닥100 선물 / S&P500 선물 / 다우 선물
   - 원자재·달러·환율: WTI / 달러인덱스 / 원-달러 / 위안-달러 / 엔-달러 / 금
   - 국채수익률: 10년물 / 2년물 / 3개월물 / 30년물 (10년물-2년물 커브 역전 여부 언급)
   - 반도체: SOX / SOXL / 엔비디아 / 마이크론 / 샌디스크
   - M7 + AI·반도체: 애플·MS·알파벳·아마존·엔비디아·메타·테슬라 / 인텔·AMD·브로드컴·오라클
   - 변동성: VIX

3) 🖼 **8시 전후 캡처화면 정리** — [방송 화면 캡처] 블록을 그대로 활용하되
   **같은 종목은 한 덩어리로 합치고, 시각(HH:MM)은 쓰지 마세요.**
   - 종목명을 굵게 한 줄(`**아마존닷컴**`) 쓰고, 그 종목의 캡처 내용을 그 아래
     `- ` 불릿으로 모으세요. 같은 종목이 6번 나와도 제목은 **한 번만** 씁니다
     (2026-08-02 실측: 아마존닷컴 6줄·Apple 4줄이 제목까지 그대로 반복됐습니다).
   - 내용이 겹치면 한 줄로 합치고, 종목 순서는 방송에 나온 순서를 유지하세요.
   - 링크 줄은 그 종목 덩어리 **맨 아래**에 `🔗 `로 시작하는 **한 줄**로 모으세요
     (코드가 이 줄들을 모아 접습니다).
   - 기사 제목을 그대로 붙이지 말고 **키워드로 줄이세요** — `[운용 & Now] 'TIGER
     미국필라델피아반도체나스닥 ETF' 순자산 6조원 돌파...` → `[TIGER 미국반도체 ETF
     순자산 6조 돌파](url)`. 20자 안팎이면 충분합니다.
   - ⚠️ **영문 캡처는 반드시 한국어로 옮겨 쓰세요.** 미장 화면은 블룸버그·
     트레이딩뷰 등 영문 헤드라인이라 원문 그대로 실으면 읽히지 않습니다
     (2026-07-29 실측: "Micron's stock sinks toward worst monthly drop in 11
     years as China fears escalate"가 영문 그대로 나갔습니다).
     · **수치·티커·기업명은 원문 그대로** 두고 서술만 한국어로:
       `Micron 166.84 −11.34% · 중국 리스크 확산에 11년래 최악의 월간 낙폭`
     · 잘려 있는 문장(`…`)은 **추측해서 채우지 마세요** — 확인된 부분까지만.
     · 한국어로 이미 적힌 캡처는 그대로 둡니다.
   - `화면 원문`으로 표시된 캡처(07:45~08:10 흰 배경 요약 슬라이드)는 **요약하지 말고
     화면의 줄 구성을 그대로** 옮기세요. 항목 순서·구분자(/)·수치를 원문대로 유지.
   - ⚠️ 이 구간은 **흰 배경 그림·슬라이드만** 대상입니다. 개별 종목 시세판
     (종목명·현재가·거래량 나열)은 이미 제외돼 있으니 **개별 주가를 끌어와 쓰지 마세요.**
   - ⚠️ **매일 나오는 요약 슬라이드 3종은 절대 누락하지 마세요** — 화면 캡처 블록에
     있으면 반드시 전부 옮기세요:
     ① `전일, 해외 시장 흐름 및 특징` ② `전일, 국내 시장 흐름 및 특징`
     ③ `오늘 시황 전망 및 투자 대응`.
     특히 ②의 **수급 3행**(코스피/코스닥 개인·외국인·기관 순매수, 금투·투신 세부)은
     숫자 배치 자체가 정보이니 요약하지 말고 원문 그대로 옮기세요.
   - **종목 블록(`**종목명**` 단위)이 4개를 넘으면** 상위 3개만 그대로 두고, 그
     다음 줄에 `---시세상세---`를 단독으로 넣은 뒤 나머지 종목 블록을 이어서
     쓰세요(코드가 이 마커 아래를 접습니다). 3개 이하면 마커 없이 그대로 두세요.

4) 💼 **관심종목 업데이트** — 보유/관심 종목의 전일 종가·등락률 + 방송 언급 여부.
   ⚠️ **언급 여부는 이모지 하나로만 표시합니다 — 설명 문구를 쓰지 마세요.**
   - 방송에서 언급된 종목: 줄 앞에 **🎤** 하나만 붙이고 **맥락은 쓰지 마세요.**
   - 언급되지 않은 종목: **아무 표시도 붙이지 마세요.** `(언급 없음)`이라고 쓰면 안 됩니다
     (2026-08-02 실측: 13줄 중 11줄이 "(언급 없음)"이라 줄바꿈만 늘렸습니다).
   - 예) `- 🎤 마이크론: 823.03 📉 ▼5.90%` / `- 삼성전자: 262,500원 📈 ▲26.81%`
   (✅·체크표시·📡는 쓰지 마세요 — 방송 언급 표시는 🎤 하나로 통일합니다.)

5) 🔎 **수급 · 변동성**:
   - **수급주체 요약**: [수급 데이터]의 summary를 **개인 / 외국인 / 기관합계 3줄로
     먼저** 쓰고, 각 줄에 delta가 있으면 `(전일比 ±N)`을 붙이세요. summary.date가
     그 수급의 기준일입니다 — **날짜를 임의로 '전일'이라고 바꿔 쓰지 마세요.**
   - 위 3줄 바로 다음 줄에 **`---수급상세---`를 단독으로** 넣고, 그 아래에
     ① 연기금·투신·사모 등 "그외" 주체 ② 순매수 top10 / 순매도 top10
     ③ ETF 등락 상위/하위를 이어 쓰세요 — 코드가 이 마커 아래 전부를 접습니다.
     핵심 3주체만 바로 보이고 나머지는 눌러야 펼쳐지게 하려는 목적이니
     마커 위치를 반드시 지키세요.
   - **ETF 등락 상위/하위**: [수급 데이터]의 etf. **name(종목명)을 그대로 쓰고
     ticker는 쓰지 마세요** — 코드만 나열하면 사람이 못 읽습니다. 단 name이 코드
     형태(예: "0197X0")로 와 있으면 **그대로 두세요 — 이름을 추측해 채우지 마세요.**
   - 위 목록들은 **한 줄에 한 종목**씩 쓰세요 (쉼표로 이어붙이지 말 것)
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
  ⚠️ **`search.naver.com`·`google.com/search`처럼 검색 결과 페이지 URL을 링크로
  쓰지 마세요 — 그건 기사가 아닙니다.** 해당 종목의 실제 기사 url이 위 두 곳에
  없으면 링크 없이 종목명만 쓰고 다음 종목으로 넘어가세요.
- ⚠️ **[주요종목] 목록에 있는 종목을 먼저** 쓰고, 다 쓴 뒤 `---기타---` 를 한 줄로 넣고
  그 아래에 나머지 종목을 쓰세요. `---기타---` 아래는 접기(펼치기) 블록이 되어
  눌러야 보입니다. 표식을 빠뜨리지 마세요.
<<KR_NEWS_EXTRA>>
- 겹치는 기사는 묶어 사안별 3줄 이내로 요약하세요.

### 리포트 ③ 데일리 주요 종목기사 정리 (===DAILY===)

[뉴스 브리핑 기사]에 오늘 수집된 기사 전체가 있습니다. 이걸 **종목별이 아니라
이슈(사안) 단위로 묶어** 정리하세요 — ===NEWS===는 종목 기준, 이건 오늘 하루 전체를
관통하는 사안 기준이라 관점이 다릅니다.
- 같은 사안을 다루는 기사는 묶어 하나의 이슈로. 상위 6~8개 이슈만, 중요도 순.
- 이슈당 정확히 2줄: 1줄 `• **이슈 제목** — 핵심 내용 한 줄`,
  2줄 `  🔗 [기사제목](url) · [기사제목](url)` (url은 [뉴스 브리핑 기사]에 있는 것만)
- [보유/관심 종목 목록]이 걸린 이슈는 앞쪽에 배치하세요.
- 위 화법 규칙(완곡 표현 금지·단정형 명사구·인용 압축)을 여기도 그대로 적용하세요.
- [뉴스 브리핑 기사]가 비어 있으면 "오늘 수집된 기사가 없습니다"라고만 쓰세요."""

    session_focus = {
        "us": "이 방송은 전일 미국장 마감 리뷰입니다. 1번의 미국장 정리를 특히 두껍게 쓰세요.",
        "kr": "이 방송은 당일 한국장 개장 전 전망입니다. 1번의 국내장 전망과 3번(8시 전후 화면)을 "
              "특히 두껍게 쓰고, 미국 시황과의 연결 고리는 아래 '오늘 미국 세션 리포트'를 참고하세요.",
    }[session]
    # 3번 섹션 제목은 kr 기준("8시 전후")으로 적혀 있어 us(05:55~06:40)엔 안 맞는다
    # — 05:55 캡처에 "8시 전후"라는 제목이 붙어 나갔다(2026-07-29 실측).
    if session == "us":
        common_order = common_order.replace(
            "8시 전후 캡처화면 정리", "미국장 캡처화면 정리")
    # kr 세션의 ===NEWS===엔 미국 지수·종목이 섞여 나와 별도 미국 세션 리포트와
    # 중복됐다(2026-08-18 실측: 나스닥지수·S&P500·마이크론·샌디스크가 삼성전자·
    # SK하이닉스와 나란히 접기 없이 노출). 미국 항목은 ---기타---로 보내고,
    # 노출(주요종목) 자리는 한국 종목 상위 2개로 제한한다. LLM이 이 개수 지시를
    # 놓쳐도 _cap_news_head()가 코드로 다시 한번 2개로 강제한다.
    kr_news_extra = (
        "- ⚠️ **국장(kr) 전용**: 나스닥·S&P500 등 **미국 지수·미국 상장 종목은 이미 "
        "별도 미국 세션 리포트에서 다뤘으므로 여기서는 [주요종목] 여부와 무관하게 "
        "전부 `---기타---` 아래(접기)로 보내세요.** [주요종목] 자리(접기 밖)에는 "
        "**한국 상장 종목만, 최대 2개**까지만 쓰세요."
    ) if session == "kr" else ""
    common_order = common_order.replace("<<KR_NEWS_EXTRA>>", kr_news_extra)
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
- 화면 캡처(3번)는 **종목 단위로 묶어** 쓰세요 — 캡처 1장당 한 덩어리가 아닙니다:
    `**종목명**` 한 줄 → 그 아래 그 종목 캡처 내용들을 `- ` 불릿으로
    → 맨 아래 `🔗 [종목명](기사url) · [종목명](기사url)` 한 줄
  ⚠️ **시각은 쓰지 마세요** — `06:03`·`[00:03:15]` 어떤 형태도 넣지 않습니다.
  같은 종목이 여러 캡처에 나오면 제목을 반복하지 말고 한 덩어리로 합치세요.
  아래 [방송 화면 캡처] 블록에 이미 이 형식으로 정리돼 있으니 **그 내용과 링크를
  그대로 활용**하고, url을 새로 만들지 마세요. 그 블록에 링크가 없는 종목은
  **검색 URL을 만들어 채우지 말고 링크 없이 종목명만** 남기세요.
- **`화면 원문`으로 표시된 캡처(07:45~08:10 요약 슬라이드)는 요약하지 말고
  화면의 줄 구성을 그대로** 옮기세요 — 항목 순서·구분자·수치를 원문대로 유지.
  단 **시세 표처럼 줄이 매우 많은 화면은 상위 15줄까지만** 옮기고 `…(이하 생략)`으로
  줄이세요. 원문 보존이 다른 섹션(1·2·4·5)을 밀어내면 안 됩니다.
- 💹 주요 지수/자산 변동 섹션 포함 (아래 검증 시세 사용)
- 🎤 보유종목 언급 체크: 아래 보유/관심 종목이 방송에서 언급됐는지 확인. **언급된 종목만**
  줄 앞에 🎤를 붙이고, 언급 안 된 종목은 아무 표시도 붙이지 마세요("언급 없음" 금지).
- ⚠️ **ETF는 리포트에 넣지 마세요.** 삼프로TV는 시황에서 ETF를 다루지 않고 화면의
  ETF(KODEX·TIGER·ACE·PLUS·SOL 등, 커버드콜·인버스·레버리지 포함)는 전부 협찬 광고입니다.
  단 5번의 'ETF 등락 상위/하위'는 [수급 데이터]에서 온 시장 통계이므로 예외입니다.
- 가격·등락률은 반드시 [검증 시세]를 우선하고, 화면 숫자는 검증 실패 시에만 "(방송 화면 기준)"을 붙여 사용
- **종가 기준일은 섹션 제목에 한 번만** 쓰세요 (예: `💹 주요 지표 (7/24 종가 기준)`).
  줄마다 `[2026-07-24 종가]`처럼 반복하면 지저분해집니다 — 절대 줄 끝에 붙이지 마세요.
  ⚠️ **줄 끝 날짜 표기는 `stale: true`인 항목에만** 붙이세요 — asof가 대표 기준일과
  하루이틀 다른 건 정상(지표마다 발표 시각이 다를 뿐)이라 예외 표기 대상이 아닙니다.
  `stale: true`인 항목만 `⚠️(7/16 기준)`처럼 자기 날짜를 병기하고 최신 값이 아님을 밝히세요.
  **`stale` 필드가 아예 없거나 `false`인 항목(KOSPI·KOSDAQ 포함)에는 절대 이 경고를
  붙이지 마세요** — asof 날짜만 보고 스스로 판단해서 붙이면 안 됩니다.
- **등락 아이콘을 반드시 붙이세요** — 각 시세의 `icon` 필드(📈 상승 / 📉 하락 / ➖ 보합)를
  그대로 사용하고, 형식은 다음을 따르세요:
  `• 나스닥: 20,123.45 📈 ▲1.23%`
  숫자에는 천단위 쉼표를 넣고, 목록은 `•` 불릿으로 정렬해 한눈에 읽히게 하세요.
  ⚠️ **`icon`이 `➖`(보합)면 ▲/▼ 화살표를 붙이지 말고 `➖ 0.00%`로만 쓰세요**
  (`icon`이 📈/📉일 때만 그 뒤에 ▲/▼를 붙입니다). `➖ ▼0.00%`처럼 아이콘과
  화살표를 섞어 쓰면 안 됩니다(2026-08-20 실측: 2년물 국채수익률이 반올림하면
  0.00%인데 `➖ ▼0.00%`로 나가 아이콘과 화살표가 서로 모순됐습니다).
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
===DAILY===
리포트 ③ 데일리 주요 종목기사 정리 — 이슈 단위 6~8개, 항목당 2줄.
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
        data = _parse_sections(_call_llm(settings["models"], prompt, max_tokens=40000),
                               session=session)
        for key in ("title_keyword", "markdown_report"):
            if not data.get(key):
                raise RuntimeError(f"리포트 생성 결과에 {key} 누락")
        if not data["reports"]["sihwang"].strip():
            raise RuntimeError("리포트 생성 결과에 시황 본문(===SIHWANG===) 누락")
        # LLM의 ===DAILY=== 요약 아래에 오늘 수집된 기사 원문 전체를 접어서 붙인다 —
        # 요약이 놓친 기사도 원하면 펼쳐서 확인할 수 있게 (검증·투명성 목적)
        raw_articles = _briefing_lines(news_briefing)
        if raw_articles:
            fold_block = tg_format.fold(
                f"오늘 수집 기사 원문 {len(news_briefing or [])}건 — 눌러서 펼치기",
                raw_articles,
            )
            data["reports"]["news"] = f"{data['reports']['news']}\n\n{fold_block}"
            data["markdown_report"] = f"{data['markdown_report']}\n\n{fold_block}"
    except Exception as e:
        log.error("LLM 리포트 생성 실패 → 원자료 기반 열화 리포트로 전환: %s", e)
        data = _fallback_report(
            settings, session, vision_results, transcript, indices,
            verified_mentions, holdings_data, holdings_quotes, reason=str(e)[:200],
            news_briefing=news_briefing, flows=flows,
        )

    # 최후 안전장치 — LLM이 프롬프트를 무시하고 검색 결과 페이지 URL을 직접
    # 만들어냈을 경우에 대비해 링크만 벗긴다 (실제 기사 URL은 건드리지 않음)
    for key in ("telegram_text", "markdown_report"):
        data[key] = _strip_search_links(data.get(key, ""))
    data["reports"] = {k: _strip_search_links(v) for k, v in data["reports"].items()}

    out_file = out_dir / "report.json"
    out_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(data["markdown_report"], encoding="utf-8")
    log.info("리포트 생성 완료: 키워드=%s", data["title_keyword"])
    return data


# ─────────────────── ETF 포트폴리오 리뷰 (KRX PDF 기반) ───────────────────


def _fmt_qty(v: float) -> str:
    """수량 증감을 부호 포함해 읽기 쉽게 (소수 계약수도 있어 정수면 정수로)."""
    sign = "+" if v > 0 else "−"
    a = abs(v)
    return f"{sign}{a:,.0f}" if a >= 1 or a == 0 else f"{sign}{a:,.2f}"


def _fmt_pp(v: float | None) -> str:
    """비중 변화(%p). None이면 빈 문자열."""
    if v is None:
        return ""
    return f"{'+' if v > 0 else '−'}{abs(v):.2f}%p"


def _move_tail(m: dict) -> str:
    """수량 변동 한 줄의 꼬리 — 현재 비중과 비중 변화(참고).

    비중은 주가 등락이 섞인 값이라 매매 근거가 아니라 '이 종목이 포트에서 얼마나
    큰가'를 보여주는 맥락으로만 붙인다.
    """
    if m.get("weight") is None:
        return ""
    tail = f" · 비중 {m['weight']:.2f}%"
    dw = _fmt_pp(m.get("dw"))
    return f"{tail} ({dw})" if dw else tail


def _etf_block(name: str, ticker: str, diff: dict, prev_date: str) -> str:
    """ETF 1종의 전일 대비 구성 변화 블록.

    수량(실매매)을 본문에, 비중 변화는 괄호로 덧붙인다 — 비중만 크게 움직이고
    수량이 그대로면 그건 매매가 아니라 주가효과라 오해를 부른다.
    """
    lines = [f"■ **{name}** ({ticker})"]
    head = f"구성 {diff['count_today']}종목"
    if prev_date:
        head += f" · {prev_date[4:6]}/{prev_date[6:8]} 대비"
    if diff.get("has_qty"):
        head += f" · 매수 {diff['n_buys']} / 매도 {diff['n_sells']}"
    lines.append(head)
    # 바스켓(설정단위) 자체가 움직였으면 개별 매매와 헷갈리지 않게 따로 알린다
    shift = diff.get("basket_shift") or 0
    if abs(shift) >= 1:
        lines.append(f"⚙️ 설정단위 전체 {_fmt_pp(shift).replace('%p', '%')} 조정"
                     " *(개별 매매 아님)*")

    if diff["added"]:
        items = ", ".join(
            f"{r['name']}" + (f" ({r['weight']:.2f}%)" if r.get("weight") is not None else "")
            for r in diff["added"])
        lines.append(f"🆕 신규편입: {items}")
    if diff["removed"]:
        items = ", ".join(r["name"] for r in diff["removed"])
        lines.append(f"🚪 편출: {items}")

    if diff.get("has_qty"):
        if diff["buys"]:
            lines.append("📈 매수(수량↑)")
            for m in diff["buys"]:
                lines.append(f"· {m['name']} {_fmt_qty(m['dq'])}주{_move_tail(m)}")
        if diff["sells"]:
            lines.append("📉 매도(수량↓)")
            for m in diff["sells"]:
                lines.append(f"· {m['name']} {_fmt_qty(m['dq'])}주{_move_tail(m)}")
        if not diff["buys"] and not diff["sells"] and not diff["added"] and not diff["removed"]:
            lines.append("· 전일 대비 수량 변동 없음")
    else:
        # 수량 컬럼이 없으면 비중으로 대체하되, 주가효과가 섞였음을 명시한다
        if diff["weight_up"]:
            items = ", ".join(f"{m['name']} {_fmt_pp(m['dw'])}" for m in diff["weight_up"])
            lines.append(f"📈 비중 확대: {items}")
        if diff["weight_down"]:
            items = ", ".join(f"{m['name']} {_fmt_pp(m['dw'])}" for m in diff["weight_down"])
            lines.append(f"📉 비중 축소: {items}")
        if diff["weight_up"] or diff["weight_down"]:
            lines.append("  *(수량 미공시 — 주가 등락에 의한 변동이 섞여 있습니다)*")

    return "\n".join(lines)


def generate_etf_review(settings: dict, results: list[dict]) -> dict:
    """ETF 포트폴리오 리뷰 — KRX 공시 PDF의 전일 대비 구성 변화.

    LLM을 쓰지 않는다. 수량·비중 차이는 순수 계산이라 요약이 필요 없고, Gemini
    무료 할당량(하루 20건)을 여기에 쓰면 정작 방송 리포트가 밀린다.

    results: [{"name", "ticker", "diff", "prev_date"}] — 조회 실패분은 호출부가 뺀다.
    """
    today = now_kst().strftime("%Y-%m-%d (%a)")
    blocks = [_etf_block(r["name"], r["ticker"], r["diff"], r.get("prev_date", ""))
              for r in results]
    body = "\n\n".join(blocks) or "(조회된 ETF가 없습니다)"
    note = ("ℹ️ KRX 공시 납입자산구성내역(PDF) 기준. 비중은 주가 등락으로도 변하므로 "
            "실제 운용 매매는 **수량 변화**로 판정합니다.")
    md = f"📦 **ETF 포트폴리오 리뷰** ({today})\n\n{body}\n\n{note}\n\n{settings['report']['disclaimer']}"
    return {"title_keyword": "ETF구성변화", "telegram_text": md, "markdown_report": md}
