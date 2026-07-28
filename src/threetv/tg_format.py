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

_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(((?:https?|obsidian)://[^\s)]+)\)")
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
    """접기 마커만 걷어낸 평문 마크다운 (카카오 등 접기를 모르는 채널용)."""
    def _cb(m: re.Match) -> str:
        title = m.group(1).strip()
        return f"▼ {title}\n{m.group(2).strip()}" if title else m.group(2).strip()
    return _FOLD_RE.sub(_cb, md or "")


def to_obsidian(md: str) -> str:
    """접기 마커를 옵시디안 접이식 callout으로 변환."""
    def _cb(m: re.Match) -> str:
        title = m.group(1).strip()
        body = "\n".join(f"> {ln}" for ln in m.group(2).strip().splitlines())
        return f"> [!note]- {title}\n{body}"
    return _FOLD_RE.sub(_cb, md or "")
