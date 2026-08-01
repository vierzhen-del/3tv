"""마크다운 리포트 → 텔레그램 HTML 변환.

텔레그램은 parse_mode 없이 보내면 마크다운을 렌더링하지 않는다. 그래서
`[제목](url)`의 긴 URL과 `**06:14**`의 별표가 본문에 그대로 노출됐다
(2026-07-27 실측 스크린샷). parse_mode=HTML로 보내고 여기서 변환한다.

접기(숨기기) 탭은 텔레그램의 `<blockquote expandable>`을 쓴다 — 눌러야 펼쳐지므로
기사 목록처럼 긴 블록을 접어둘 수 있다.
"""
from __future__ import annotations

import re

# 리포트 본문에서 접을 구간을 표시하는 마커 (LLM/코드가 삽입)
FOLD_OPEN = "<<<FOLD:"      # 예: <<<FOLD:미장 종목 기사 (12건)>>>
FOLD_CLOSE = "<<<END>>>"

# 링크 표시 텍스트는 **대괄호를 한 겹 품을 수 있다** — 언론사 제목이
# "[운용 & Now] 'TIGER 미국필라델피아반도체나스닥 ETF' 순자산 6조원 돌파"처럼
# 대괄호로 시작하는 경우가 흔하다. 예전 `[^\]\n]+`는 제목 안의 첫 `]`에서 끊겨
# 매칭에 실패했고, 그러면 변환이 통째로 안 돼 **마크다운 원문이 그대로 텔레그램에
# 노출**됐다(2026-08-01 실물 확인: `🔗 [[운용 & Now] ...](https://www.ebn.co.kr/...)`).
# LLM도 링크를 직접 만들기 때문에 생성부만 손봐선 막히지 않아 정규식에서 처리한다.
_LINK_RE = re.compile(
    r"\[((?:[^\[\]\n]|\[[^\[\]\n]*\])+)\]\(((?:https?|obsidian)://[^\s)]+)\)"
)
_BOLD2_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_BOLD1_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_FENCE_RE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.S)
_HEAD_RE = re.compile(r"^#{1,6}\s*(.+)$", re.M)
_FOLD_RE = re.compile(
    re.escape(FOLD_OPEN) + r"(.*?)>>>\n?(.*?)" + re.escape(FOLD_CLOSE), re.S
)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_telegram_html(md: str) -> str:
    """리포트 마크다운을 텔레그램이 렌더링하는 HTML로.

    지원: 링크(URL 숨김) · 굵게 · 헤더(굵게로) · 코드펜스 · 접기 블록.
    표(| ... |)는 텔레그램이 렌더링하지 못하므로 그대로 남긴다(코드펜스로 감싸면
    가독성이 더 나쁘다) — 프롬프트에서 표 대신 불릿을 쓰게 유도한다.
    """
    text = md or ""

    # 1) 접기 블록·코드펜스를 먼저 자리표시자로 빼둔다 (내부를 이스케이프만 하고 변환 제외)
    holds: list[str] = []

    def _hold(html: str) -> str:
        holds.append(html)
        return f"\x00{len(holds) - 1}\x00"

    def _fence(m: re.Match) -> str:
        return _hold(f"<pre>{_esc(m.group(1).rstrip())}</pre>")

    text = _FENCE_RE.sub(_fence, text)

    def _fold(m: re.Match) -> str:
        title = _esc(m.group(1).strip())
        body = to_telegram_html(m.group(2).strip())   # 내부도 링크·굵게 변환
        head = f"<b>{title}</b>\n" if title else ""
        return _hold(f"<blockquote expandable>{head}{body}</blockquote>")

    text = _FOLD_RE.sub(_fold, text)

    # 2) 본문 이스케이프 후 인라인 변환
    text = _esc(text)
    # 링크: 표시 텍스트만 보이고 URL은 숨는다 (긴 검색 URL 노출 문제 해결)
    text = _LINK_RE.sub(
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = _HEAD_RE.sub(lambda m: f"<b>{m.group(1).strip()}</b>", text)
    text = _BOLD2_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _BOLD1_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)

    # 3) 자리표시자 복원
    for i, html in enumerate(holds):
        text = text.replace(f"\x00{i}\x00", html)
    return text


def fold(title: str, body: str) -> str:
    """접기 블록 마커로 감싼다 (텔레그램에선 expandable quote, 옵시디안에선 callout)."""
    return f"{FOLD_OPEN}{title}>>>\n{body}\n{FOLD_CLOSE}"


def to_plain(md: str) -> str:
    """접기 마커·마크다운·HTML 서식을 전부 벗긴 평문 (카카오 등 서식을 모르는 채널용).

    카카오 나에게 보내기는 마크다운도 HTML도 렌더링하지 않는다 — `**제목**`이나
    `<b>제목</b>`이 글자 그대로 노출된다(2026-08-01 실물 확인: us/kr 리포트의
    `**📌 3protv오늘...**`과 ETF 리뷰의 `<b>ETF 포트폴리오 리뷰</b>` 둘 다 카카오
    메시지에 기호가 그대로 보였다). 대부분의 리포트는 마크다운이지만 ETF 리뷰처럼
    처음부터 HTML로 만들어지는 것도 있어(`generate_etf_review`) 두 서식 다 벗긴다.
    """
    def _cb(m: re.Match) -> str:
        title = m.group(1).strip()
        body = to_plain(m.group(2).strip())    # 접기 안쪽도 재귀적으로 정리
        return f"▼ {title}\n{body}" if title else body
    out = _FOLD_RE.sub(_cb, md or "")
    out = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", out)   # [글자](url) → 글자 (url)
    out = _BOLD2_RE.sub(lambda m: m.group(1), out)     # **굵게**
    out = _BOLD1_RE.sub(lambda m: m.group(1), out)     # *기울임*
    out = _HEAD_RE.sub(lambda m: m.group(1).strip(), out)   # ## 제목
    out = re.sub(r"<a href=\"([^\"]+)\">(.*?)</a>", r"\2 (\1)", out, flags=re.S)
    out = re.sub(r"<[^>]+>", "", out)
    return (out.replace("&lt;", "<").replace("&gt;", ">")
               .replace("&quot;", '"').replace("&amp;", "&"))


def to_obsidian(md: str) -> str:
    """접기 마커를 옵시디안 접이식 callout으로 변환."""
    def _cb(m: re.Match) -> str:
        title = m.group(1).strip()
        body = "\n".join(f"> {ln}" for ln in m.group(2).strip().splitlines())
        return f"> [!note]- {title}\n{body}"
    return _FOLD_RE.sub(_cb, md or "")
