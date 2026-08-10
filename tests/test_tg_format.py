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


def test_obsidian_deeplink_becomes_clickable():
    """2026-07-28 실물 확인: obsidian:// 링크가 하이퍼링크로 안 바뀌고 [텍스트](URL)가
    그대로 노출됐다 — _LINK_RE가 http(s)만 매칭해서였다."""
    md = "🗂 [옵시디안에서 열기](obsidian://search?vault=vierzhen_home&query=3protv%EC%95%BC%EA%B0%84_20260728)"
    html = tg_format.to_telegram_html(md)
    assert '<a href="obsidian://search?vault=vierzhen_home&amp;query=' in html
    assert "옵시디안에서 열기</a>" in html
    assert "[옵시디안에서 열기](" not in html


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


class _FakeResp:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text


def test_send_telegram_fans_out_to_comma_separated_chat_ids(monkeypatch):
    """TELEGRAM_CHAT_ID에 콤마로 여러 id를 넣으면 각 방에 모두 보내야 한다
    (기존 대상 유지 + 새 단체방 추가 시나리오)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111, -100222")
    seen = []

    def fake_post(url, json=None, timeout=None):
        seen.append(json["chat_id"])
        return _FakeResp(200)

    monkeypatch.setattr(tg.requests, "post", fake_post)
    assert tg.send_telegram("hello") is True
    assert seen == ["111", "-100222"]


def test_send_telegram_single_chat_id_still_works(monkeypatch):
    """콤마 없는 기존 단일 chat id 설정은 그대로 동작해야 한다(회귀 방지)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    seen = []
    monkeypatch.setattr(
        tg.requests, "post",
        lambda url, json=None, timeout=None: seen.append(json["chat_id"]) or _FakeResp(200),
    )
    assert tg.send_telegram("hello") is True
    assert seen == ["111"]


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


def test_link_label_with_brackets_still_converts():
    """2026-08-01 실물: 기사 제목이 '[운용 & Now]'처럼 대괄호로 시작하면
    링크 변환이 실패해 마크다운 원문이 그대로 텔레그램에 노출됐다."""
    md = ("🔗 [[운용 & Now] 'TIGER 미국필라델피아반도체나스닥 ETF' 순자산 6조원 돌파..."
          "](https://www.ebn.co.kr/news/articleView.html?idxno=1718121)")
    html = tg_format.to_telegram_html(md)
    assert '<a href="https://www.ebn.co.kr/news/articleView.html?idxno=1718121">' in html
    assert "](http" not in html          # 마크다운 원문이 남으면 안 된다
    assert "운용 &amp; Now" in html      # 제목은 그대로 보존


def test_link_label_with_mid_brackets():
    html = tg_format.to_telegram_html("🔗 [속보 [단독] 반도체 수출 급증](https://a.b/2)")
    assert '<a href="https://a.b/2">속보 [단독] 반도체 수출 급증</a>' in html


def test_plain_brackets_are_not_treated_as_links():
    """링크가 아닌 대괄호 문구를 링크로 오인하면 안 된다."""
    html = tg_format.to_telegram_html("배열 arr[0] 과 [주의] 문구")
    assert "<a href" not in html


def test_to_plain_strips_markdown_bold():
    """2026-08-01 실물: 카카오 메시지에 '**📌 3protv오늘...**' 별표가 그대로 노출됐다."""
    out = tg_format.to_plain("**📌 3protv오늘_20260731_MS호실적반도체급등 [한국 시황]**")
    assert "**" not in out
    assert "📌 3protv오늘_20260731_MS호실적반도체급등 [한국 시황]" in out


def test_to_plain_strips_html_tags():
    """ETF 리뷰는 처음부터 <b>/<i> HTML로 만들어진다 — 카카오에 태그가 그대로 보였다."""
    md = "📦 <b>ETF 포트폴리오 리뷰</b>\n\n■ <b>TIME 미국나스닥100액티브</b> (426030)"
    out = tg_format.to_plain(md)
    assert "<b>" not in out and "</b>" not in out
    assert "ETF 포트폴리오 리뷰" in out and "TIME 미국나스닥100액티브" in out


def test_to_plain_converts_markdown_links():
    out = tg_format.to_plain("🔗 [삼성전자 신고가](https://a.b/1)")
    assert "[삼성전자 신고가](" not in out
    assert "삼성전자 신고가 (https://a.b/1)" in out


def test_to_plain_strips_headers():
    out = tg_format.to_plain("## 요약\n내용")
    assert "##" not in out and "요약" in out


def test_to_plain_fold_body_is_also_cleaned():
    """접기 안쪽에 마크다운/HTML이 있어도 재귀적으로 정리돼야 한다."""
    inner = "**굵게** <b>태그</b> [링크](https://a.b/1)"
    out = tg_format.to_plain(tg_format.fold("제목", inner))
    assert "**" not in out and "<b>" not in out and "](http" not in out
    assert "굵게" in out and "태그" in out and "링크 (https://a.b/1)" in out
