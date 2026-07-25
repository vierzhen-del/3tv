"""generate_report의 LLM 실패 흡수(열화 리포트) + 보유종목 언급 매칭 테스트.

2026-07-23 실장애 재현: Gemini가 429(prepayment depleted)로 완전히 막혀
비전 분석 0장 + LLM 리포트 불가 상태에서도 리포트가 발행돼야 한다.
"""
from __future__ import annotations

import json

import pytest

from threetv import report as report_mod

SETTINGS = {
    "sessions": {
        "us": {"label": "미국 시황", "start_kst": "05:55"},
        "kr": {"label": "한국 시황", "start_kst": "07:45",
               "verbatim_window": ["07:45", "08:10"]},
    },
    "report": {"disclaimer": "※ 투자 참고용입니다."},
    "telegram": {"max_message_len": 4000},
    "models": {"gemini": "g", "gemini_fallback": "gl", "claude_disabled": True},
}

HOLDINGS = {
    "holdings": [
        {"name": "삼성전자", "ticker": "005930", "market": "KR", "aliases": ["삼전"]},
        {"name": "마이크론", "ticker": "MU", "market": "US", "aliases": ["MU", "Micron"]},
        {"name": "테슬라", "ticker": "TSLA", "market": "US", "aliases": ["Tesla"]},
    ],
    "watchlist": [],
}

INDICES = [
    {"name": "나스닥", "ticker": "^IXIC", "market": "US", "close": 20123.45,
     "change_pct": 1.23, "direction": "▲", "icon": "📈", "asof": "2026-07-24"},
    {"name": "KOSPI", "ticker": "^KS11", "market": "KR", "close": 3150.5,
     "change_pct": -0.42, "direction": "▼", "icon": "📉", "asof": "2026-07-24"},
]

HOLDINGS_QUOTES = [
    {"name": "삼성전자", "ticker": "005930", "market": "KR", "close": 88000,
     "change_pct": 2.1, "direction": "▲", "icon": "📈", "asof": "2026-07-24"},
]

REQUIRED_KEYS = ("title_keyword", "telegram_text", "markdown_report")


def _generate(tmp_path, transcript="", vision_results=None, session="us"):
    return report_mod.generate_report(
        SETTINGS, session, vision_results or [], transcript,
        INDICES, [], HOLDINGS, HOLDINGS_QUOTES, tmp_path,
    )


@pytest.fixture
def llm_down(monkeypatch):
    """모든 LLM 호출이 7/23 실장애와 동일한 429로 죽는 상황."""
    def boom(*a, **k):
        raise RuntimeError(
            "429 RESOURCE_EXHAUSTED. Your prepayment credits are depleted."
        )
    monkeypatch.setattr(report_mod, "_call_llm", boom)


def test_llm_failure_still_returns_valid_report(tmp_path, llm_down):
    data = _generate(tmp_path, transcript="반도체 업종이 강세였습니다.")
    for key in REQUIRED_KEYS:
        assert data.get(key), f"{key} 누락"
    assert isinstance(data["holdings_mentioned"], list)


def test_llm_failure_writes_report_files(tmp_path, llm_down):
    """아티팩트·아카이브 일관성 — 열화 경로에서도 두 파일이 저장돼야 한다."""
    data = _generate(tmp_path, transcript="장중 특징주 정리")
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == data["markdown_report"]
    saved = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert saved["title_keyword"] == data["title_keyword"]


def test_fallback_includes_quotes_and_degraded_banner(tmp_path, llm_down):
    md = _generate(tmp_path, transcript="오늘 시장 요약")["markdown_report"]
    assert "AI 요약 없음" in md          # 요약본이 아님을 사용자가 즉시 알 수 있어야
    assert "prepayment credits" in md    # 실패 사유 명시
    assert "나스닥" in md and "20,123.45" in md and "▲1.23%" in md
    assert "KOSPI" in md and "▼0.42%" in md
    assert "삼성전자" in md              # 보유종목 시세


def test_fallback_preserves_full_transcript_in_markdown(tmp_path, llm_down):
    """전사는 LLM 없는 날의 유일한 방송 내용 — 마크다운에서 잘려선 안 된다."""
    transcript = "삼프로TV 방송 내용입니다. " * 2000  # 약 3만자 (실측 규모)
    md = _generate(tmp_path, transcript=transcript)["markdown_report"]
    assert transcript in md


def test_fallback_telegram_fits_single_message(tmp_path, llm_down):
    """텔레그램은 한 건 한도 안에서 발췌 — 3만자를 그대로 쏟아내면 안 된다."""
    transcript = "긴 방송 전사 내용. " * 3000
    tg = _generate(tmp_path, transcript=transcript)["telegram_text"]
    assert len(tg) <= SETTINGS["telegram"]["max_message_len"]
    assert "이하 생략" in tg


def test_fallback_matches_holdings_by_alias(tmp_path, llm_down):
    """전사에 alias만 있어도 언급으로 잡아야 한다 (LLM 판단 없이)."""
    data = _generate(tmp_path, transcript="오늘은 삼전이 강했고 Tesla도 반등했습니다.")
    by_name = {m["name"]: m for m in data["holdings_mentioned"]}
    assert by_name["삼성전자"]["mentioned"] is True
    assert by_name["테슬라"]["mentioned"] is True
    assert by_name["마이크론"]["mentioned"] is False   # 언급 없음
    assert "삼전" in by_name["삼성전자"]["context"]


def test_short_ascii_ticker_needs_word_boundary(tmp_path, llm_down):
    """'MU'가 'must'/'museum' 같은 단어에 잘못 걸리면 안 된다."""
    data = _generate(tmp_path, transcript="You must visit the museum. 무난한 장세.")
    by_name = {m["name"]: m for m in data["holdings_mentioned"]}
    assert by_name["마이크론"]["mentioned"] is False

    hit = _generate(tmp_path, transcript="MU 실적이 좋았습니다.")
    by_name2 = {m["name"]: m for m in hit["holdings_mentioned"]}
    assert by_name2["마이크론"]["mentioned"] is True


@pytest.mark.parametrize("transcript", [
    "Tesla도 반등했습니다",      # 영문명 + 조사 (한국어 전사의 기본 패턴)
    "TSLA는 상승 마감",          # 티커 + 조사
    "테슬라, MU 등이 강세",      # 쉼표 인접
    "(Tesla) 신고가",            # 괄호 인접
])
def test_ascii_alias_matches_with_korean_particle(tmp_path, llm_down, transcript):
    """한국어 전사는 영문 뒤에 조사가 바로 붙는다 — \\b로는 못 잡히는 실제 패턴."""
    data = _generate(tmp_path, transcript=transcript)
    by_name = {m["name"]: m for m in data["holdings_mentioned"]}
    assert by_name["테슬라"]["mentioned"] or by_name["마이크론"]["mentioned"]


def test_fallback_notes_empty_vision(tmp_path, llm_down):
    """Gemini가 막힌 날은 자료화면도 0장 — 그 사실이 리포트에 드러나야 한다."""
    md = _generate(tmp_path, transcript="전사만 있음", vision_results=[])["markdown_report"]
    assert "자료화면 추출물이 없습니다" in md


def test_fallback_includes_material_digest_when_available(tmp_path, llm_down):
    vision = [{"timestamp_sec": 65, "type": "자료화면", "text": "엔비디아 신고가"}]
    md = _generate(tmp_path, transcript="음성", vision_results=vision)["markdown_report"]
    assert "05:56" in md and "엔비디아 신고가" in md


SECTIONED = """===TITLE===
반도체급등
===TELEGRAM===
요약 텍스트
===MARKDOWN===
## 상세 리포트
===HOLDINGS===
삼성전자 | O | 실적 언급
테슬라 | X |
===END==="""


def test_successful_llm_path_unchanged(tmp_path, monkeypatch):
    """회귀 방지 — LLM이 정상이면 그 결과를 그대로 쓴다."""
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: SECTIONED)
    data = _generate(tmp_path, transcript="무엇이든")
    assert data["title_keyword"] == "반도체급등"
    assert data["markdown_report"] == "## 상세 리포트"
    assert "AI 요약 없음" not in data["markdown_report"]
    by = {h["name"]: h for h in data["holdings_mentioned"]}
    assert by["삼성전자"]["mentioned"] is True and by["테슬라"]["mentioned"] is False
    assert by["삼성전자"]["context"] == "실적 언급"


def test_sections_survive_quotes_and_newlines():
    """2026-07-25 3연속 실장애의 원인들을 한 번에 재현.

    JSON이었다면 ① 실제 개행 ② 미이스케이프 따옴표 로 모두 깨졌을 입력이다.
    """
    raw = '''===TITLE===
AI반도체수급
===TELEGRAM===
🇺🇸 미국 시황
메타, "유휴 컴퓨팅 없다" "임대 제의 검토일 뿐"
- 나스닥 📈 ▲1.30%
===MARKDOWN===
## 미국 시황

다우 📈 ▲0.27% / 나스닥 📈 ▲1.30%
마이크론, $2,000억 → $2,500억 "투자 확대" (UBS)

| 종목 | 종가 |
|---|---|
| NVDA | 202.0 |
===HOLDINGS===
삼성전자 | O | "미국 생산 확대" 촉구
===END==='''
    data = report_mod._parse_sections(raw)
    assert data["title_keyword"] == "AI반도체수급"
    assert '"유휴 컴퓨팅 없다"' in data["telegram_text"]
    assert "| NVDA | 202.0 |" in data["markdown_report"]
    assert data["holdings_mentioned"][0]["mentioned"] is True
    assert '"미국 생산 확대"' in data["holdings_mentioned"][0]["context"]


def test_quote_line_has_no_asof_suffix():
    """줄 끝 '[2026-07-24 종가]'는 제거 — 기준일은 섹션 제목에만."""
    q = {"name": "나스닥", "close": 20123.45, "change_pct": 1.23,
         "direction": "▲", "icon": "📈", "asof": "2026-07-24"}
    line = report_mod._quote_line(q)
    assert line == "• 나스닥: 20,123.45 📈 ▲1.23%"
    assert "종가]" not in line and "2026-07-24" not in line


def test_asof_label_uses_most_common_date():
    quotes = [{"asof": "2026-07-24"}, {"asof": "2026-07-24"}, {"asof": "2026-07-23"}]
    assert report_mod._asof_label(quotes) == "7/24 종가 기준"
    assert report_mod._asof_label([]) == ""


def test_fallback_omits_transcript_section_when_disabled(tmp_path, llm_down):
    """전사를 끄면 빈 '음성 전사' 섹션이 남지 않아야 한다."""
    data = _generate(tmp_path, transcript="")
    assert "음성 전사" not in data["markdown_report"]
    assert "음성 전사" not in data["telegram_text"]


def test_fallback_leads_with_screen_capture(tmp_path, llm_down):
    """구성 축: 화면 캡처 → 지표 → 종목·뉴스."""
    vision = [{"timestamp_sec": 65, "type": "자료화면", "text": "엔비디아 신고가"}]
    md = _generate(tmp_path, transcript="", vision_results=vision)["markdown_report"]
    cap, idx = md.find("방송 화면 캡처"), md.find("주요 지표")
    assert 0 < cap < idx                     # 화면 캡처가 지표보다 앞
    assert "엔비디아 신고가" in md
    assert "7/24 종가 기준" in md            # 기준일은 섹션 제목에만


def test_capture_uses_short_clock_time(tmp_path, llm_down):
    """캡처 시각은 방송 표시시각 'HH:MM'으로 짧게 (us 시작 05:55 + 8분)."""
    vision = [{"timestamp_sec": 8 * 60, "type": "자료화면", "text": "메타 관련 보도"}]
    md = _generate(tmp_path, transcript="", vision_results=vision)["markdown_report"]
    assert "06:03" in md
    assert "[08:00]" not in md and "480" not in md


def test_capture_two_lines_with_news_link(tmp_path, llm_down):
    """캡처당 2줄 — ① 시각·종목 ② 관련 기사 링크."""
    vision = [{"timestamp_sec": 8 * 60, "type": "자료화면", "text": "엔비디아 신고가 경신",
               "stocks": [{"name": "엔비디아", "market": "US"}]}]
    ver = [{"name": "엔비디아", "market": "US", "quote": None,
            "news": [{"title": "NVDA record", "url": "https://news/nvda", "publisher": "Reuters"}]}]
    md = report_mod.generate_report(
        SETTINGS, "us", vision, "", INDICES, ver, HOLDINGS, HOLDINGS_QUOTES, tmp_path,
    )["markdown_report"]
    assert "**06:03** · 엔비디아" in md
    assert "🔗 [엔비디아](https://news/nvda)" in md


def test_verbatim_window_preserves_screen_layout(tmp_path, llm_down):
    """07:45~08:10 주요지표 슬라이드는 압축하지 않고 원문 줄 구성을 보존."""
    screen = ("다우 +0.27% / 나스닥 +1.30% / S&P500 +0.81%\n"
              "WTI $71.8 / 달러인덱스 100.7 / 원화 1,507원\n"
              "국채수익률 10년 4.54%, 2년 4.17%, 3개월 3.78%")
    vision = [{"timestamp_sec": 15 * 60, "type": "자료화면", "text": screen}]   # 07:45+15m=08:00
    md = report_mod.generate_report(
        SETTINGS, "kr", vision, "", INDICES, [], HOLDINGS, HOLDINGS_QUOTES, tmp_path,
    )["markdown_report"]
    assert "08:00 · 화면 원문" in md
    assert screen in md            # 줄 구성 그대로


def test_outside_verbatim_window_is_compressed(tmp_path, llm_down):
    """구간 밖 화면은 한 줄로 압축된다."""
    screen = "첫 줄 내용\n둘째 줄 내용"
    vision = [{"timestamp_sec": 40 * 60, "type": "자료화면", "text": screen}]  # 07:45+40m=08:25
    md = report_mod.generate_report(
        SETTINGS, "kr", vision, "", INDICES, [], HOLDINGS, HOLDINGS_QUOTES, tmp_path,
    )["markdown_report"]
    assert "화면 원문" not in md
    assert "첫 줄 내용 둘째 줄 내용" in md    # 한 줄로 합쳐짐


def test_fallback_includes_news_briefing(tmp_path, llm_down):
    """LLM 요약이 불가해도 수집된 기사 목록은 브리핑으로 남아야 한다."""
    briefing = [
        {"title": "마이크론 투자 확대", "url": "https://news/mu",
         "summary": "메모리 슈퍼사이클 기대", "query": "마이크론"},
        {"title": "엔비디아 신고가", "url": "https://news/nvda",
         "summary": "AI 수요 지속", "query": "엔비디아"},
    ]
    data = report_mod.generate_report(
        SETTINGS, "us", [], "", INDICES, [], HOLDINGS, HOLDINGS_QUOTES, tmp_path,
        news_briefing=briefing,
    )
    md = data["markdown_report"]
    assert "뉴스 브리핑" in md
    assert "[마이크론 투자 확대](https://news/mu)" in md
    assert "메모리 슈퍼사이클 기대" in md


def test_briefing_omitted_when_no_articles(tmp_path, llm_down):
    md = _generate(tmp_path, transcript="")["markdown_report"]
    assert "뉴스 브리핑" not in md


def test_sections_reject_non_sectioned_text():
    with pytest.raises(ValueError):
        report_mod._parse_sections("그냥 평범한 텍스트 응답")


def test_sectioned_response_uses_llm_not_fallback(tmp_path, monkeypatch):
    """따옴표·개행이 섞인 응답이 열화 경로로 새지 않아야 한다."""
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: '''===TITLE===
AI반도체
===TELEGRAM===
메타, "유휴 컴퓨팅 없다"
===MARKDOWN===
## 미국 시황

나스닥 📈 ▲1.30%
===HOLDINGS===
===END===''')
    data = _generate(tmp_path, transcript="방송 전사")
    assert data["title_keyword"] == "AI반도체"
    assert "AI 요약 없음" not in data["markdown_report"]
    assert "나스닥 📈 ▲1.30%" in data["markdown_report"]


def test_truncated_response_reports_token_limit_cause(tmp_path, monkeypatch):
    """토큰 상한에 걸려 잘린 응답은 '잘렸다'는 원인이 로그에 드러나야 한다.

    7/25 실측: 잘린 JSON이 'JSON 객체를 찾지 못함'으로만 보여 원인 파악이 늦어졌다.
    """
    class FakeResp:
        text = '{"title_keyword": "AI", "telegram_text": "본문이 여기서 잘림'
        candidates = [type("C", (), {"finish_reason": "MAX_TOKENS"})()]

    monkeypatch.setattr(report_mod, "env_token", lambda *a, **k: "fake-key")
    monkeypatch.setattr(
        report_mod, "_call_gemini",
        lambda m, p, t=8000: (_ for _ in ()).throw(
            RuntimeError(f"Gemini 응답이 max_output_tokens({t})에 걸려 잘렸습니다")
        ),
    )
    data = _generate(tmp_path, transcript="전사")
    # 열화 경로로 안전하게 떨어지고, 사유에 '잘렸' 이 남는다
    assert data["title_keyword"] == "원자료시황"
    assert "잘렸" in data["markdown_report"]


def test_vision_parses_json_with_literal_newlines():
    """vision도 동일 — 슬라이드 표 텍스트는 여러 줄이라 같은 문제가 난다."""
    from threetv import vision as vision_mod
    raw = '[{"index": 0, "type": "자료화면", "text": "다우 +0.27%\n나스닥 +1.30%"}]'
    got = vision_mod._parse_json_array(raw)
    assert got[0]["type"] == "자료화면"
    assert "나스닥" in got[0]["text"]


def test_missing_required_section_falls_back(tmp_path, monkeypatch):
    """필수 섹션이 비면 열화 경로로 — 빈 리포트 발행 방지."""
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: """===TITLE===
키워드
===TELEGRAM===
===MARKDOWN===
본문
===END===""")
    data = _generate(tmp_path, transcript="전사 내용")
    assert data["title_keyword"] == "원자료시황"
    assert "AI 요약 없음" in data["markdown_report"]
