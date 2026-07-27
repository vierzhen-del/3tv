"""마크다운 → 텔레그램 HTML 변환 + 태그 안전 분할 테스트.

2026-07-27 실물 스크린샷에서 확인된 문제를 회귀 방지로 고정한다:
긴 네이버 검색 URL이 본문에 그대로 노출됐고(`[ACE 고배당주Plus](https://search...)`),
`**06:14**`의 별표도 렌더링되지 않은 채 보였다 — parse_mode 미설정이 원인.
"""
from __future__ import annotations

from threetv import notify_telegram as tg
from threetv import tg_format
from threetv.common import window_offsets


def test_link_shows_title_only():
    """긴 URL은 숨고 제목만 — 스크린샷의 최대 불만."""
    md = "• [ACE 고배당주](https://search.naver.com/search.naver?where=news&query=x%2Fy)"
    html = tg_format.to_telegram_html(md)
    # href 안의 &는 &amp;로 이스케이프돼야 한다 (텔레그램이 파싱 시 되돌린다)
    assert '<a href="https://search.naver.com/search.naver?where=news&amp;query=x%2Fy">' in html
    assert "ACE 고배당주</a>" in html
    assert "https://search.naver.com" not in html.split("<a href=", 1)[1].split(">", 1)[1]


def test_bold_and_header():
    html = tg_format.to_telegram_html("## 요약\n**06:14** 나스닥 강세")
    assert "<b>요약</b>" in html
    assert "<b>06:14</b>" in html
    assert "**" not in html and "##" not in html


def test_fold_becomes_expandable_blockquote():
    md = tg_format.fold("그 외 종목 기사 (3건)", "• [제목](https://a.b/1)\n• 인텔 약세")
    html = tg_format.to_telegram_html(md)
    assert "<blockquote expandable>" in html
    assert "<b>그 외 종목 기사 (3건)</b>" in html
    assert '<a href="https://a.b/1">제목</a>' in html      # 접기 안쪽도 링크 변환
    assert tg_format.FOLD_OPEN not in html


def test_angle_brackets_escaped_outside_tags():
    """본문의 부등호는 이스케이프돼야 파싱이 깨지지 않는다."""
    html = tg_format.to_telegram_html("전일대비 <2% 상승 & 거래량 증가")
    assert "&lt;2%" in html and "&amp;" in html


def test_to_plain_drops_fold_markers():
    """카카오처럼 접기를 모르는 채널엔 마커가 노출되면 안 된다."""
    out = tg_format.to_plain("본문\n" + tg_format.fold("그 외 기사", "• 항목1"))
    assert tg_format.FOLD_OPEN not in out and tg_format.FOLD_CLOSE not in out
    assert "▼ 그 외 기사" in out and "• 항목1" in out


def test_to_obsidian_uses_collapsible_callout():
    md = "본문\n" + tg_format.fold("그 외 기사", "• 항목1\n• 항목2")
    out = tg_format.to_obsidian(md)
    assert "> [!note]- 그 외 기사" in out
    assert "> • 항목1" in out
    assert tg_format.FOLD_CLOSE not in out


# ── 분할: 태그·접기 블록을 가르지 않아야 한다 ──────────────────────────────

def test_split_keeps_blockquote_whole():
    """4000자 경계가 접기 블록 한가운데 걸려도 블록은 쪼개지지 않는다."""
    filler = "\n".join(f"• 종목{i} 소식입니다" for i in range(250))
    quote = f"<blockquote expandable><b>그 외</b>\n{filler}</blockquote>"
    text = "머리말\n" * 300 + quote
    chunks = tg._split(text, 4000, html=True)
    assert len(chunks) > 1
    hit = [c for c in chunks if "blockquote" in c]
    assert len(hit) == 1
    assert hit[0].count("<blockquote") == hit[0].count("</blockquote>")


def test_split_never_cuts_inside_anchor():
    line = " ".join(f'<a href="https://ex.com/{i}">기사제목 {i}</a>' for i in range(300))
    for chunk in tg._split(line, 1000, html=True):
        assert chunk.count("<a ") == chunk.count("</a>")
        assert chunk.count("<") == chunk.count(">")


def test_split_breaks_oversized_block_into_valid_pieces():
    """블록 하나가 한도를 넘으면 조각마다 여닫는 태그를 다시 붙인다."""
    body = "\n".join(f"• 아주 긴 항목 {i}" for i in range(500))
    chunks = tg._split(f"<blockquote expandable>{body}</blockquote>", 2000, html=True)
    assert len(chunks) > 1
    for c in chunks:
        c = c.strip()
        assert c.startswith("<blockquote expandable>") and c.endswith("</blockquote>")
        assert len(c) <= 2000


def test_split_plaintext_unchanged():
    """html=False면 기존 줄 단위 분할 그대로 (실패 알림 등)."""
    text = "\n".join(f"줄 {i}" for i in range(2000))
    chunks = tg._split(text, 4000)
    assert all(len(c) <= 4000 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")


def test_strip_tags_keeps_url_visible():
    """HTML 파싱 실패 시 평문 폴백 — 링크는 'text (url)'로 살아남는다."""
    out = tg._strip_tags('<b>제목</b> <a href="https://a.b/1">기사</a> &amp; 끝')
    assert out == "제목 기사 (https://a.b/1) & 끝"


# ── 구간 전사 오프셋 ─────────────────────────────────────────────────────

def test_window_offsets_kr_session():
    """07:45 녹화 시작 · 07:50~08:05 구간 → 300초부터 900초."""
    assert window_offsets("07:45", ["07:50", "08:05"]) == (300, 900)


def test_window_offsets_clamps_before_recording_start():
    """구간이 녹화 시작보다 앞서면 0초부터, 길이도 겹치는 만큼만."""
    assert window_offsets("07:45", ["07:30", "08:00"]) == (0, 900)


def test_window_offsets_invalid():
    assert window_offsets("07:45", None) is None
    assert window_offsets("07:45", ["08:05", "07:50"]) is None   # 뒤집힘
    assert window_offsets("07:45", ["아침"]) is None
