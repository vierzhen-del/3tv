"""generate_report의 LLM 실패 흡수(열화 리포트) + 보유종목 언급 매칭 테스트.

2026-07-23 실장애 재현: Gemini가 429(prepayment depleted)로 완전히 막혀
비전 분석 0장 + LLM 리포트 불가 상태에서도 리포트가 발행돼야 한다.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from threetv import report as report_mod
from threetv import tg_format

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
    assert "데일리 기사 정리" in md
    assert "[마이크론 투자 확대](https://news/mu)" in md
    assert "메모리 슈퍼사이클 기대" in md


def test_briefing_omitted_when_no_articles(tmp_path, llm_down):
    md = _generate(tmp_path, transcript="")["markdown_report"]
    assert "데일리 기사 정리" not in md


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
    """시황 본문이 비면 열화 경로로 — 빈 리포트 발행 방지."""
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: """===TITLE===
키워드
===SIHWANG===
===NEWS===
기사만 있고 시황이 없음
===END===""")
    data = _generate(tmp_path, transcript="전사 내용")
    assert data["title_keyword"] == "원자료시황"
    assert "AI 요약 없음" in data["markdown_report"]


def test_legacy_markdown_section_still_parsed(tmp_path, monkeypatch):
    """구형 ===MARKDOWN=== 출력도 시황 본문으로 인정 (모델이 옛 형식을 낼 때 대비)."""
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: """===TITLE===
키워드
===MARKDOWN===
본문
===END===""")
    data = _generate(tmp_path, transcript="전사 내용")
    assert data["title_keyword"] == "키워드"
    assert data["reports"]["sihwang"] == "본문"


def test_report_split_into_sihwang_and_news(tmp_path, monkeypatch):
    """세션당 2건 — 시황 / 종목기사검색으로 분리되고 '기타'는 텔레그램용에서만 접힌다.

    저장(옵시디안)용 news는 접지 않고 전부 남겨야 한다(2026-08-09 확정 —
    나중에 read_us_section_today() 등으로 재사용될 때 전종목이 검색돼야 함).
    """
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: """===TITLE===
반도체급등
===SIHWANG===
## 📌 요약
• 나스닥 강세
===NEWS===
• **엔비디아**: 급등
  🔗 [실적 서프라이즈](https://n.news/1)
---기타---
• **인텔**: 약세
===HOLDINGS===
삼성전자 | O | 언급됨
===END===""")
    data = _generate(tmp_path, transcript="")
    assert data["title_keyword"] == "반도체급등"
    assert "나스닥 강세" in data["reports"]["sihwang"]

    # 저장용(news) — 접지 않고 인텔까지 그대로 검색 가능해야 한다
    assert "엔비디아" in data["reports"]["news"]
    assert "인텔" in data["reports"]["news"]
    assert tg_format.FOLD_OPEN not in data["reports"]["news"]

    # 전송용(news_telegram) — '기타'는 접기 블록 안으로
    assert "엔비디아" in data["reports"]["news_telegram"]
    assert "---기타---" not in data["reports"]["news_telegram"]   # 접기 마커로 치환됨
    assert tg_format.FOLD_OPEN in data["reports"]["news_telegram"]
    assert "인텔" in data["reports"]["news_telegram"]

    # 통합본(옵시디안 단일 파일·하위호환)에는 둘 다 접지 않은 채로 들어간다
    assert "나스닥 강세" in data["markdown_report"]
    assert "엔비디아" in data["markdown_report"]
    assert "인텔" in data["markdown_report"]
    assert tg_format.FOLD_OPEN not in data["markdown_report"]
    assert data["holdings_mentioned"][0]["mentioned"] is True


def test_fold_top_n_keeps_head_folds_rest():
    """상위 n개 항목만 본문에 남고 나머지는 접기 블록으로."""
    items = "\n".join(
        f"• **이슈{i}** — 요약\n  🔗 [기사{i}](https://n/{i})" for i in range(1, 6)
    )
    out = report_mod._fold_top_n(items, 3, "그 외 이슈")
    assert "이슈1" in out and "이슈2" in out and "이슈3" in out
    assert out.index("이슈3") < out.index(tg_format.FOLD_OPEN)   # 상위 3개는 접기 밖
    assert tg_format.FOLD_OPEN in out
    assert "이슈4" in out and "이슈5" in out                     # 나머지는 접힌 채로 존재


def test_fold_top_n_noop_when_within_limit():
    """항목이 n개 이하면 접지 않는다."""
    items = "• **이슈1** — 요약\n• **이슈2** — 요약"
    out = report_mod._fold_top_n(items, 3, "그 외 이슈")
    assert out == items
    assert tg_format.FOLD_OPEN not in out


def test_cap_news_head_moves_overflow_before_existing_rest():
    """---기타--- 위 항목이 n개를 넘으면 코드가 강제로 자르고, 넘친 항목은
    기존 ---기타--- 내용 앞에 끼워 넣는다(2026-08-18 "한국기사는 2개외 접기")."""
    news = (
        "• **삼성전자**: 274,500원 — 상승\n"
        "  🔗 [기사1](https://n/1)\n"
        "• **SK하이닉스**: 1,645,000원 — 상승\n"
        "  🔗 [기사2](https://n/2)\n"
        "• **나스닥지수**: 26,644.91 — 하락\n"
        "  🔗 [기사3](https://n/3)\n"
        "---기타---\n"
        "• **인텔**: 약세\n"
    )
    out = report_mod._cap_news_head(news, 2)
    head, _, rest = out.partition("---기타---")
    assert "삼성전자" in head and "SK하이닉스" in head
    assert "나스닥지수" not in head           # 3번째부터는 head에서 빠짐
    assert "나스닥지수" in rest and "인텔" in rest   # 기존 기타 항목도 그대로 보존


def test_cap_news_head_noop_within_limit():
    news = "• **삼성전자**: 274,500원 — 상승\n• **SK하이닉스**: 1,645,000원 — 상승\n"
    assert report_mod._cap_news_head(news, 2) == news


def test_kr_news_capped_to_2(tmp_path, monkeypatch):
    """kr 세션은 종목기사 노출을 2개로 강제한다."""
    news_body = """===TITLE===
반도체급등
===SIHWANG===
1) 📌 요약
• 나스닥 강세
===NEWS===
• **삼성전자**: 274,500원 📈 ▲2.43%
  🔗 [기사1](https://n/1)
• **SK하이닉스**: 1,645,000원 📈 ▲3.26%
  🔗 [기사2](https://n/2)
• **나스닥지수**: 26,644.91 📉 ▼0.32%
  🔗 [기사3](https://n/3)
• **마이크론**: 1,011.75 📈 ▲4.13%
  🔗 [기사4](https://n/4)
===HOLDINGS===
===END==="""
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: news_body)

    kr_data = _generate(tmp_path, transcript="", session="kr")
    kr_telegram = kr_data["reports"]["news_telegram"]
    assert "삼성전자" in kr_telegram and "SK하이닉스" in kr_telegram
    assert kr_telegram.index("SK하이닉스") < kr_telegram.index(tg_format.FOLD_OPEN)
    assert "나스닥지수" in kr_telegram and "마이크론" in kr_telegram   # 접힌 채로 존재
    # 저장(옵시디안 아카이브)용은 접지 않고 4종목 전부 그대로 남아야 한다
    assert tg_format.FOLD_OPEN not in kr_data["reports"]["news"]
    assert "마이크론" in kr_data["reports"]["news"]


def test_us_news_capped_to_5(tmp_path, monkeypatch):
    """2026-08-24 요청: us 세션 종목기사도 상위 5개만 노출하고 나머지는 접는다
    (kr과 달리 중복 회피 목적이 아니라 리포트가 길어 가독성 때문)."""
    items = "\n".join(
        f"• **종목{i}**: {i}00원 📈 ▲{i}.00%\n  🔗 [기사{i}](https://n/{i})"
        for i in range(1, 7)   # 6개 — 5개 초과라 마지막 1개는 접혀야 함
    )
    news_body = f"""===TITLE===
반도체급등
===SIHWANG===
1) 📌 요약
• 나스닥 강세
===NEWS===
{items}
===HOLDINGS===
===END==="""
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: news_body)

    us_data = _generate(tmp_path, transcript="", session="us")
    us_telegram = us_data["reports"]["news_telegram"]
    assert tg_format.FOLD_OPEN in us_telegram
    for i in range(1, 6):
        assert f"종목{i}" in us_telegram
        assert us_telegram.index(f"종목{i}") < us_telegram.index(tg_format.FOLD_OPEN)
    assert "종목6" in us_telegram   # 접힌 채로 존재
    # 저장(옵시디안 아카이브)용은 접지 않고 6종목 전부 그대로 남아야 한다
    assert tg_format.FOLD_OPEN not in us_data["reports"]["news"]
    assert "종목6" in us_data["reports"]["news"]


def test_us_news_noop_within_5(tmp_path, monkeypatch):
    """us 세션도 5개 이하면 접지 않는다."""
    news_body = """===TITLE===
반도체급등
===SIHWANG===
1) 📌 요약
• 나스닥 강세
===NEWS===
• **삼성전자**: 274,500원 📈 ▲2.43%
  🔗 [기사1](https://n/1)
• **SK하이닉스**: 1,645,000원 📈 ▲3.26%
  🔗 [기사2](https://n/2)
===HOLDINGS===
===END==="""
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: news_body)

    us_data = _generate(tmp_path, transcript="", session="us")
    us_telegram = us_data["reports"]["news_telegram"]
    assert tg_format.FOLD_OPEN not in us_telegram
    assert "삼성전자" in us_telegram and "SK하이닉스" in us_telegram


def test_capture_rest_mark_folds_stocks_after_top_3(tmp_path, monkeypatch):
    """3) 캡처화면 정리에서 종목이 4개를 넘으면 ---시세상세--- 마커 아래로 접힌다
    (2026-08-11 실측: 국장 개별 종목 시세가 18종목까지 안 접힌 채 그대로 나갔다)."""
    stocks = "\n".join(
        f"**종목{i}**\n- 현재가 {i}00원, 전일대비 ▲{i}0원 (+{i}.00%) (방송 화면 기준)"
        for i in range(1, 6)
    )
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: f"""===TITLE===
반도체급등
===SIHWANG===
1) 📌 요약
• 나스닥 강세

3) 🖼 8시 전후 캡처화면 정리
**종목1**
- 현재가 100원, 전일대비 ▲10원 (+1.00%) (방송 화면 기준)
**종목2**
- 현재가 200원, 전일대비 ▲20원 (+2.00%) (방송 화면 기준)
**종목3**
- 현재가 300원, 전일대비 ▲30원 (+3.00%) (방송 화면 기준)
---시세상세---
**종목4**
- 현재가 400원, 전일대비 ▲40원 (+4.00%) (방송 화면 기준)
**종목5**
- 현재가 500원, 전일대비 ▲50원 (+5.00%) (방송 화면 기준)

4) 💼 관심종목 업데이트
- 삼성전자: 278,500원 📈 ▲1.64%
===NEWS===
===HOLDINGS===
===END===""")
    data = _generate(tmp_path, transcript="")
    telegram = data["reports"]["sihwang"]
    assert "종목1" in telegram and "종목2" in telegram and "종목3" in telegram
    assert telegram.index("종목3") < telegram.index(tg_format.FOLD_OPEN)
    assert "종목4" in telegram and "종목5" in telegram      # 접힌 채로 본문에 남아있음
    assert "---시세상세---" not in telegram                 # 마커 자체는 치환돼 사라짐
    assert "관심종목 업데이트" in telegram                   # 4번 섹션은 접기 밖에 그대로


def test_daily_section_folds_after_top_3(tmp_path, monkeypatch):
    """===DAILY=== 텔레그램 전송본은 상위 3개만 노출되고 나머지는 접힌다(2026-08-09 요청).

    저장(옵시디안)용은 접지 않는다 — 나중에 컨텍스트로 재사용할 때 전종목이
    검색돼야 하기 때문(같은 날 사용자 확정: "저장시는 풀고저장").
    """
    daily_issues = "\n".join(
        f"• **이슈{i}** — 요약\n  🔗 [기사{i}](https://n/{i})" for i in range(1, 6)
    )
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: f"""===TITLE===
반도체급등
===SIHWANG===
## 📌 요약
• 나스닥 강세
===NEWS===
• **엔비디아**: 급등
===DAILY===
{daily_issues}
===HOLDINGS===
===END===""")
    data = _generate(tmp_path, transcript="")

    # 텔레그램 전송본 — 상위 3개만 노출, 나머지는 접기 블록 안
    news_tg = data["reports"]["news_telegram"]
    assert "이슈1" in news_tg and "이슈3" in news_tg
    assert tg_format.FOLD_OPEN in news_tg
    assert "이슈5" in news_tg       # 접힌 채로 본문에는 남아있음(펼치면 보임)

    # 저장용 — 접지 않고 전부 그대로
    news_archive = data["reports"]["news"]
    assert "이슈1" in news_archive and "이슈3" in news_archive and "이슈5" in news_archive
    assert tg_format.FOLD_OPEN not in news_archive


def test_verbatim_caps_very_long_screen(tmp_path, llm_down):
    """시세 표처럼 줄이 많은 화면은 상위 일부만 — 다른 섹션을 밀어내면 안 된다.

    2026-07-26 실측: 07:59 종목 시세판이 50줄 넘게 그대로 실려 리포트를 잠식했다.
    """
    rows = "\n".join(f"종목{i}, {1000+i}, ▲ {i}, 0.5, {i*100}" for i in range(60))
    vision = [{"timestamp_sec": 14 * 60, "type": "자료화면", "text": rows}]  # 07:45+14m=07:59
    md = report_mod.generate_report(
        SETTINGS, "kr", vision, "", INDICES, [], HOLDINGS, HOLDINGS_QUOTES, tmp_path,
    )["markdown_report"]
    assert "화면 원문" in md
    assert "종목0," in md and "종목14," in md      # 상위 15줄은 보존
    assert "종목59," not in md                      # 뒤쪽은 생략
    assert "총 60줄 중 15줄 표시" in md


def test_verbatim_keeps_short_screen_intact(tmp_path, llm_down):
    """요약 슬라이드처럼 짧은 화면은 통째로 보존한다."""
    screen = ("다우 +0.27% / 나스닥 +1.30%\nWTI $71.8 / 달러인덱스 100.7\n"
              "국채 10년 4.54%, 2년 4.17%")
    vision = [{"timestamp_sec": 15 * 60, "type": "자료화면", "text": screen}]
    md = report_mod.generate_report(
        SETTINGS, "kr", vision, "", INDICES, [], HOLDINGS, HOLDINGS_QUOTES, tmp_path,
    )["markdown_report"]
    assert screen in md
    assert "줄 표시" not in md          # 생략 표기 없음


QUOTE_TABLE = (
    "종목명, 현재가, 전일대비, 등락률, 거래량\n"
    "삼성전자, 289000, ▲ 11000, 3.96, 2406255\n"
    "SK하이닉스, 2300000, ▲ 114000, 5.22, 669501\n"
    "삼성SDI, 1427000, ▲ 100000, 7.54, 91960\n"
    "현대차, 449500, ▲ 4000, 0.90, 77676\n"
    "기아, 147000, ▲ 2200, 1.52, 24922\n"
)


def test_detects_quote_table():
    assert report_mod._is_quote_table(QUOTE_TABLE, []) is True
    # 종목이 과도하게 많으면 헤더를 못 읽어도 시세판으로 본다
    many = [{"name": f"종목{i}"} for i in range(12)]
    assert report_mod._is_quote_table("무슨 화면", many) is True


def test_summary_slide_is_not_quote_table():
    slide = ("다우 +0.27% / 나스닥 +1.30% / S&P500 +0.81%\n"
             "WTI $71.8 / 달러인덱스 100.7 / 원화 1,507원")
    assert report_mod._is_quote_table(slide, [{"name": "엔비디아"}]) is False


# 2026-07-28 실장애: "전일, 해외/국내 시장 흐름 및 특징" 요약 슬라이드가 16~17개
# 종목을 한 줄씩 나열한다는 이유만으로 "종목 12개 이상 → 시세판" 규칙에 걸려
# kr 리포트에서 매일 통째로 빠졌다. 실제 방송 캡처 원문(사용자 스크린샷)으로 재현한다.
US_SUMMARY_SLIDE = (
    "전일, 해외 시장 흐름 및 특징\n"
    "다우 +0.51% / 나스닥 -0.18% / S&P500 +0.02% (7,413P)\n"
    "WTI $79.6 / 달러인덱스 101.3 / 원화 1,465원 / 위안화 6.76위안\n"
    "특징:\n"
    "SOX -2.23% / 엔비디아 -4.99%($196), 샌디스크 -10.9%, 마이크론 -1.25%($900)\n"
    "애플 +1.17%, 테슬라 -1.21%, MS +1.94%, 아마존 -0.31%, 메타 -0.22%, 알파벳 +2.13%\n"
    "오라클 +4.27%, SKHY -7.47%($143), SPCX -1.36%($113)\n"
)
US_SUMMARY_STOCKS = [
    {"name": n, "market": "US"} for n in
    ["다우", "나스닥", "S&P500", "SOX", "엔비디아", "샌디스크", "마이크론",
     "애플", "테슬라", "MS", "아마존", "메타", "알파벳", "오라클", "SKHY", "SPCX"]
]  # 16개

KR_SUMMARY_SLIDE = (
    "전일, 국내 시장 흐름 및 특징\n"
    "코스피 +0.97%(6,755P) / 코스닥 +2.22%(764P)\n"
    "코스피: 개인 +2.1조 / 외국인 -2.99조 / 기관 +8,631억 (금투 +5,082억, 투신 +4,256억)\n"
    "코스닥: 개인 -1,733억 / 외국인 -1,328억 / 기관 +2,830억 (금투 +2,193억, 투신 +412억)\n"
    "특징 :\n"
    "삼전 +1.80%, 하닉 +3.24%, NAVER +8.4%, 우리금융 +7.3%, 하이브 +6%\n"
    "레인보우로보 +6.5%, 주성엔지 +6.3%, 원익IPS +5.4%, 펩트론 +13.7%, 현대무벡스 +13%\n"
    "한화에어로 -8%, SK이노 -10%, S-OIL -9.7%, 현대로템 -16%, 한화시스템 -7.6%\n"
)
KR_SUMMARY_STOCKS = [
    {"name": n, "market": "KR"} for n in
    ["코스피", "코스닥", "삼전", "하닉", "NAVER", "우리금융", "하이브",
     "레인보우로보", "주성엔지", "원익IPS", "펩트론", "현대무벡스",
     "한화에어로", "SK이노", "S-OIL", "현대로템", "한화시스템"]
]  # 17개


def test_summary_slide_with_many_stocks_not_quote_table():
    """종목이 12개를 넘어도 요약 슬라이드 앵커가 있으면 시세판으로 오판하지 않는다."""
    assert report_mod._is_quote_table(US_SUMMARY_SLIDE, US_SUMMARY_STOCKS) is False
    assert report_mod._is_quote_table(KR_SUMMARY_SLIDE, KR_SUMMARY_STOCKS) is False
    assert len(US_SUMMARY_STOCKS) >= 12 and len(KR_SUMMARY_STOCKS) >= 12  # 회귀 조건 확인


def test_real_quote_table_still_detected_without_anchor():
    """앵커 화이트리스트를 넣어도 진짜 시세판(7/26 장애)은 여전히 걸러진다."""
    assert report_mod._is_quote_table(QUOTE_TABLE, []) is True
    many = [{"name": f"종목{i}"} for i in range(12)]
    assert report_mod._is_quote_table("무슨 화면", many) is True


def test_daily_summary_slides_survive_capture_blocks(tmp_path, llm_down):
    """08시 전후 매일 나오는 요약 슬라이드 3종이 캡처 정리에서 빠지지 않는다."""
    vision = [
        {"timestamp_sec": 19 * 60, "type": "자료화면",
         "text": US_SUMMARY_SLIDE, "stocks": US_SUMMARY_STOCKS},   # 08:04
        {"timestamp_sec": 19 * 60, "type": "자료화면",
         "text": KR_SUMMARY_SLIDE, "stocks": KR_SUMMARY_STOCKS},   # 08:04
    ]
    md = report_mod.generate_report(
        SETTINGS, "kr", vision, "", INDICES, [], HOLDINGS, HOLDINGS_QUOTES, tmp_path,
    )["markdown_report"]
    assert "전일, 해외 시장 흐름 및 특징" in md
    assert "전일, 국내 시장 흐름 및 특징" in md
    assert "SPCX -1.36%" in md            # 16번째 항목까지 안 잘림 (VERBATIM_MAX_LINES_SUMMARY)
    assert "한화시스템 -7.6%" in md        # 국내 슬라이드 마지막 줄도 보존
    assert "코스피: 개인 +2.1조" in md     # 수급 3행 원문 보존


def test_quote_table_excluded_from_8am_window(tmp_path, llm_down):
    """08시 전후는 흰 배경 슬라이드만 — 개별 종목 시세판은 제외한다."""
    vision = [
        {"timestamp_sec": 14 * 60, "type": "자료화면", "text": QUOTE_TABLE},   # 07:59 시세판
        {"timestamp_sec": 15 * 60, "type": "자료화면",
         "text": "다우 +0.27% / 나스닥 +1.30%"},                                # 08:00 슬라이드
    ]
    md = report_mod.generate_report(
        SETTINGS, "kr", vision, "", INDICES, [], HOLDINGS, HOLDINGS_QUOTES, tmp_path,
    )["markdown_report"]
    assert "다우 +0.27%" in md          # 슬라이드는 남고
    assert "2406255" not in md          # 시세판 숫자는 사라진다
    assert "SK하이닉스, 2300000" not in md


def test_us_stocks_in_captures_excludes_kr_and_tables():
    vision = [
        {"text": QUOTE_TABLE, "stocks": [{"name": "삼성전자", "market": "KR"}]},
        {"text": "엔비디아 신고가", "stocks": [
            {"name": "엔비디아", "market": "US"},
            {"name": "삼성전자", "market": "KR"},      # 국내는 제외
            {"name": "마이크론", "market": "US"},
        ]},
        {"text": "메타 관련", "stocks": [{"name": "엔비디아", "market": "US"}]},  # 중복
    ]
    got = report_mod.us_stocks_in_captures(vision)
    assert got == ["엔비디아", "마이크론"]


def test_us_stocks_in_captures_respects_limit():
    """한 화면에 종목이 몰리면 시세판으로 걸러지므로, 여러 화면에 나눠 담는다."""
    vision = [
        {"text": f"화면 {n}", "stocks": [
            {"name": f"US{n}_{i}", "market": "US"} for i in range(3)]}
        for n in range(5)
    ]
    assert len(report_mod.us_stocks_in_captures(vision, limit=5)) == 5


# ───────────── 검색 URL 퇴출 (2026-07-27/28 실장애: verified_mentions가 비면
# 캡처 화면 링크가 전부 search.naver.com 검색 페이지로 떨어졌다) ─────────────

def test_capture_blocks_uses_briefing_when_verified_mentions_empty():
    """verified_mentions가 비어도 news_briefing에서 실제 기사를 찾아 연결한다.

    2026-07-26/27 실장애 재현 조건: extract_mentions가 MAX_TOKENS로 실패하면
    verified=[] 상태로 리포트가 만들어졌다. 그래도 네이버 브리핑 수집(main.py가
    캡처 종목을 1순위로 검색)은 별도로 성공하므로, 그 기사를 캡처 링크에
    연결할 수 있어야 한다.
    """
    vision = [{"timestamp_sec": 0, "type": "자료화면", "text": "엔비디아 실적 프리뷰",
               "stocks": [{"name": "엔비디아", "market": "US"}]}]
    briefing = [{"title": "엔비디아 실적 서프라이즈", "url": "https://real.news/nvda-1",
                 "query": "엔비디아", "summary": ""}]
    out = report_mod._capture_blocks(vision, [], "05:55", None, news_briefing=briefing)
    assert "[엔비디아](https://real.news/nvda-1)" in out
    assert "search.naver.com" not in out


def test_capture_blocks_no_link_when_no_article_found():
    """기사를 못 찾은 종목은 검색 URL로 채우지 않고 링크 없이 종목명만 남긴다."""
    vision = [{"timestamp_sec": 0, "type": "자료화면", "text": "알 수 없는 종목 언급",
               "stocks": [{"name": "무명종목", "market": "US"}]}]
    out = report_mod._capture_blocks(vision, [], "05:55", None, news_briefing=[])
    assert "무명종목" in out            # head 줄에는 종목명이 남는다
    assert "🔗" not in out              # 기사 링크 줄 자체가 없다
    assert "search.naver.com" not in out


def test_capture_blocks_prefers_verified_mentions_over_briefing():
    vision = [{"timestamp_sec": 0, "type": "자료화면", "text": "삼성전자 소식",
               "stocks": [{"name": "삼성전자", "market": "KR"}]}]
    verified = [{"name": "삼성전자",
                 "news": [{"title": "검증경로 기사", "url": "https://verified/1"}]}]
    briefing = [{"title": "브리핑경로 기사", "url": "https://briefing/1", "query": "삼성전자"}]
    out = report_mod._capture_blocks(vision, verified, "05:55", None, news_briefing=briefing)
    assert "https://verified/1" in out
    assert "https://briefing/1" not in out


def test_strip_search_links_removes_search_urls_keeps_real_articles():
    md = (
        "• [S&P500](https://search.naver.com/search.naver?where=news&query=S%26P500)\n"
        "• [엔비디아 실적](https://real.news.co.kr/articles/12345)\n"
        "• [야후 검색](https://finance.yahoo.com/quote/NVDA/news)\n"
    )
    out = report_mod._strip_search_links(md)
    assert "search.naver.com" not in out
    assert "finance.yahoo.com/quote/NVDA/news" not in out
    assert "[S&P500]" not in out and "S&P500" in out           # 링크만 벗겨지고 텍스트는 남음
    assert "[엔비디아 실적](https://real.news.co.kr/articles/12345)" in out  # 실제 기사는 보존


def test_generate_report_strips_search_urls_from_llm_output(tmp_path, monkeypatch):
    """LLM이 프롬프트를 어기고 검색 URL을 직접 만들어도 최종 출력에선 사라진다."""
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: """===TITLE===
키워드
===SIHWANG===
## 요약
내용
===NEWS===
• **테슬라**: 200 📈 ▲1%
  🔗 [테슬라](https://search.naver.com/search.naver?where=news&query=테슬라)
===END===""")
    data = _generate(tmp_path, transcript="")
    assert "search.naver.com" not in data["reports"]["news"]
    assert "search.naver.com" not in data["markdown_report"]
    assert "테슬라" in data["reports"]["news"]                  # 텍스트 자체는 남는다


# ─────────────────── noon(12시에 만나요) / night(야간 미장) ───────────────────

KR_INTRADAY = [
    {"name": "KOSPI", "ticker": "^KS11", "market": "KR", "close": 3160.2,
     "change_pct": 0.31, "direction": "▲", "icon": "📈", "asof": "2026-07-28"},
    {"name": "KOSDAQ", "ticker": "^KQ11", "market": "KR", "close": 780.5,
     "change_pct": -0.12, "direction": "▼", "icon": "📉", "asof": "2026-07-28"},
]


def test_noon_report_llm_success(tmp_path, monkeypatch):
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: """===TITLE===
반도체강세
===BODY===
📌 시황 요약
• 오전장 반도체 강세
💹 장중 KR 지수
• KOSPI: 3,160.2 📈 ▲0.31%
※ 투자 참고용입니다.
===END===""")
    data = report_mod.generate_noon_report(
        SETTINGS, [], "오늘 오전 코스피 상승세입니다.", KR_INTRADAY, tmp_path,
    )
    assert data["title_keyword"] == "반도체강세"
    assert "KOSPI" in data["markdown_report"]
    assert (tmp_path / "report.md").exists()


def test_noon_report_falls_back_without_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("429 quota")))
    vision = [{"timestamp_sec": 0, "type": "자료화면", "text": "코스피 상승 출발"}]
    data = report_mod.generate_noon_report(
        SETTINGS, vision, "", KR_INTRADAY, tmp_path,
    )
    assert "AI 요약 없음" in data["markdown_report"]
    assert "KOSPI" in data["markdown_report"]
    assert "코스피 상승 출발" in data["markdown_report"]


def test_noon_report_no_news_section():
    """noon은 종목기사 섹션이 없다 (사용자 확정: 시황+장중지수만)."""
    import inspect
    sig = inspect.signature(report_mod.generate_noon_report)
    assert "news_briefing" not in sig.parameters


def _slot(hour: str, text: str) -> dict:
    return {"hour_label": hour, "vision_results": [
        {"timestamp_sec": 0, "type": "자료화면", "text": text}
    ]}


def test_night_digest_llm_success(tmp_path, monkeypatch):
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: """===TITLE===
나스닥반등
===BODY===
📊 지수 변동 궤적
• 나스닥: 상승 → 하락 → 보합
📌 주요 이벤트
• **엔비디아 실적** — 시간외 급등
※ 투자 참고용입니다.
===END===""")
    slots = [_slot("22", "나스닥 +0.5%"), _slot("23", "나스닥 -0.3%")]
    data = report_mod.generate_night_digest(SETTINGS, slots, tmp_path)
    assert data["title_keyword"] == "나스닥반등"
    assert "지수 변동 궤적" in data["markdown_report"]


def test_night_digest_falls_back_without_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("429 quota")))
    slots = [_slot("22", "나스닥 상승 출발"), _slot("03", "장중 변동성 확대")]
    data = report_mod.generate_night_digest(SETTINGS, slots, tmp_path)
    assert "AI 요약 없음" in data["markdown_report"]
    assert "나스닥 상승 출발" in data["markdown_report"]
    assert "장중 변동성 확대" in data["markdown_report"]


def test_night_digest_orders_slots_chronologically(tmp_path, monkeypatch):
    """슬롯 저장/전달 순서가 뒤섞여도 시간순으로 정렬해 프롬프트에 넣는다."""
    captured = {}

    def fake_call_llm(models, prompt, max_tokens=8000):
        captured["prompt"] = prompt
        raise RuntimeError("stop before real call")

    monkeypatch.setattr(report_mod, "_call_llm", fake_call_llm)
    slots = [_slot("23", "23시 내용"), _slot("00", "0시 내용"), _slot("22", "22시 내용")]
    report_mod.generate_night_digest(SETTINGS, slots, tmp_path)
    prompt = captured["prompt"]
    assert prompt.index("22시 내용") < prompt.index("23시 내용") < prompt.index("0시 내용")


def test_night_digest_handles_empty_slots(tmp_path, monkeypatch):
    """슬롯이 하나도 없어도(전부 실패) 리포트는 발행된다."""
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("429 quota")))
    data = report_mod.generate_night_digest(SETTINGS, [], tmp_path)
    for key in REQUIRED_KEYS:
        assert data[key]


def _slot_with_stocks(hour: str, texts: list[str], stocks: list[dict]) -> dict:
    return {"hour_label": hour, "vision_results": [
        {"timestamp_sec": i * 20, "type": "자료화면", "text": t, "stocks": stocks}
        for i, t in enumerate(texts)
    ]}


def test_night_digest_fallback_does_not_leak_internal_tags(tmp_path, monkeypatch):
    """2026-07-28 실장애: 열화 리포트에 <종목표시: ...> 내부 태그가 그대로 노출됨.

    _material_digest()는 LLM 프롬프트 전용 포맷이라 이 태그를 남기지만, 사람이
    보는 폴백 본문에는 절대 나오면 안 된다.
    """
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("429 quota")))
    slots = [_slot_with_stocks("02", ["국제 뉴스: 미국-이란 공습 이틀째 중단"],
                               [{"name": "나스닥100", "price": "28,693.50"}])]
    data = report_mod.generate_night_digest(SETTINGS, slots, tmp_path)
    assert "<종목표시" not in data["markdown_report"]
    assert "나스닥100 28,693.50" in data["markdown_report"]


def test_night_digest_fallback_dedupes_near_identical_frames(tmp_path, monkeypatch):
    """같은 슬롯 안에서 20초 간격 프레임이 거의 동일한 텍스트를 반복해도
    한 번만 남는다 (벽글씨 방지)."""
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("429 quota")))
    same_text = "국제 뉴스: 미국-이란 공습 이틀째 중단, 엔비디아 오픈AI에 2500억 금융보증"
    slots = [_slot_with_stocks("02", [same_text, same_text], [])]
    data = report_mod.generate_night_digest(SETTINGS, slots, tmp_path)
    assert data["markdown_report"].count(same_text) == 1


def test_night_digest_fallback_marks_unchanged_slot(tmp_path, monkeypatch):
    """슬롯 간(예: 01시→02시) 화면이 그대로면 원문을 반복하지 않고 '변동없음'만 남긴다."""
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("429 quota")))
    same_text = "나스닥100 28,693.50 변동 없음"
    slots = [_slot_with_stocks("01", [same_text], []),
             _slot_with_stocks("02", [same_text], [])]
    data = report_mod.generate_night_digest(SETTINGS, slots, tmp_path)
    assert data["markdown_report"].count(same_text) == 1
    assert "변동없음" in data["markdown_report"]


def test_noon_report_fallback_does_not_leak_internal_tags(tmp_path, monkeypatch):
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("429 quota")))
    vision = [{"timestamp_sec": 0, "type": "자료화면", "text": "코스피 상승 출발",
               "stocks": [{"name": "코스피", "price": "3,160.2"}]}]
    data = report_mod.generate_noon_report(SETTINGS, vision, "", KR_INTRADAY, tmp_path)
    assert "<종목표시" not in data["markdown_report"]
    assert "코스피 3,160.2" in data["markdown_report"]


# ── 2026-07-29 us 리포트 실물 지적: 영문 원문·중복·섹션 제목 ──

def test_capture_blocks_drops_duplicate_screens():
    """같은 화면이 여러 프레임에 잡히면 리포트에 같은 줄이 반복된다
    (us 실측: Russell 2000·Micron 헤드라인이 각각 2번씩 실렸다)."""
    dup = "Micron's stock sinks toward worst monthly drop in 11 years"
    vision = [
        {"timestamp_sec": 0, "type": "자료화면", "text": dup},
        {"timestamp_sec": 20, "type": "자료화면", "text": dup},
        {"timestamp_sec": 40, "type": "자료화면", "text": "  ".join(dup.split())},
        {"timestamp_sec": 60, "type": "자료화면", "text": "Nasdaq 100 heads for 10% drop"},
    ]
    out = report_mod._capture_blocks(vision, [], "05:55")
    assert out.count("Micron") == 1
    assert "Nasdaq 100" in out


def test_capture_blocks_keeps_distinct_screens():
    vision = [
        {"timestamp_sec": 0, "type": "자료화면", "text": "화면 A"},
        {"timestamp_sec": 20, "type": "자료화면", "text": "화면 B"},
    ]
    out = report_mod._capture_blocks(vision, [], "05:55")
    assert "화면 A" in out and "화면 B" in out


def test_us_prompt_asks_for_korean_translation(monkeypatch):
    """미장 캡처는 영문이라 번역 지시가 프롬프트에 있어야 한다."""
    captured = {}

    def fake(models, prompt, max_tokens=8000):
        captured["p"] = prompt
        raise RuntimeError("stop")

    monkeypatch.setattr(report_mod, "_call_llm", fake)
    _generate(Path(tempfile.mkdtemp()), session="us", transcript="")
    p = captured["p"]
    assert "영문 캡처는 반드시 한국어로" in p
    assert "미국장 캡처화면 정리" in p       # us엔 '8시 전후' 제목이 안 맞는다
    assert "8시 전후 캡처화면 정리" not in p


def test_kr_prompt_keeps_its_own_section_title(monkeypatch):
    captured = {}

    def fake(models, prompt, max_tokens=8000):
        captured["p"] = prompt
        raise RuntimeError("stop")

    monkeypatch.setattr(report_mod, "_call_llm", fake)
    _generate(Path(tempfile.mkdtemp()), session="kr", transcript="")
    assert "8시 전후 캡처화면 정리" in captured["p"]


def test_prompt_asks_for_kospi_kosdaq_quote_line(monkeypatch):
    """2026-08-18 실측: 종합 다이제스트가 국내 지수를 못 찾는 원인이 여기 있었다 —
    2) 주요 지표 지시문이 미국 지표만 나열해 KOSPI/KOSDAQ이 리포트에 quote 형식으로
    나온 적이 없었다(데이터는 있는데 출력 지시가 없었음)."""
    captured = {}

    def fake(models, prompt, max_tokens=8000):
        captured["p"] = prompt
        raise RuntimeError("stop")

    monkeypatch.setattr(report_mod, "_call_llm", fake)
    _generate(Path(tempfile.mkdtemp()), session="kr", transcript="")
    assert "국내 지수: KOSPI / KOSDAQ" in captured["p"]


def test_prompt_asks_for_30y_treasury(monkeypatch):
    """2026-08-21 요청 — 국채수익률에 30년물 추가."""
    captured = {}

    def fake(models, prompt, max_tokens=8000):
        captured["p"] = prompt
        raise RuntimeError("stop")

    monkeypatch.setattr(report_mod, "_call_llm", fake)
    _generate(Path(tempfile.mkdtemp()), session="kr", transcript="")
    assert "30년물" in captured["p"]


# ── 2026-08-01 가독성 개편: 접기 마커 + 링크 정규화 ──

def test_flow_detail_mark_gets_folded():
    """수급 상세는 텔레그램(reports.sihwang)에서만 접히고, 저장본(markdown_report/
    reports.sihwang_md)은 news와 같은 원칙(2026-08-14 확정)으로 펼쳐 남는다 —
    나중에 컨텍스트로 재사용될 때 전체가 검색돼야 하고, 사람이 볼트에서 직접
    열어봐도 바로 읽혀야 하기 때문."""
    text = """===TITLE===
키워드
===SIHWANG===
5) 🔎 수급 · 변동성
· 개인: +100
· 외국인: -50
---수급상세---
· 연기금: +10
· 순매수 top10 여기
===END==="""
    out = report_mod._parse_sections(text)

    tg = out["reports"]["sihwang"]
    assert "· 개인: +100" in tg
    assert report_mod.tg_format.FOLD_OPEN in tg
    assert "· 연기금: +10" not in tg.split(report_mod.tg_format.FOLD_OPEN)[0]

    for archive in (out["markdown_report"], out["reports"]["sihwang_md"]):
        assert "· 개인: +100" in archive
        assert "· 연기금: +10" in archive
        assert "· 순매수 top10 여기" in archive
        assert report_mod.tg_format.FOLD_OPEN not in archive
        assert "---수급상세---" not in archive


def test_flow_detail_callout_syntax_gets_normalized():
    """Gemini가 `---수급상세---` 마커 대신 옵시디안 콜아웃(`> [!note]-`)을 직접
    써버려도(2026-08-14 실측 — 텔레그램에 콜아웃 원문이 그대로 노출됨) 텔레그램은
    정상적으로 접히고, 저장본은 펼쳐진 평문으로 남아야 한다."""
    text = """===TITLE===
키워드
===SIHWANG===
5) 🔎 수급 · 변동성
· 개인: +100
· 외국인: -50

> [!note]- 수급 상세 (그외 주체 · TOP10 · ETF 등락)
> · 연기금: +10
> · 순매수 top10 여기
===END==="""
    out = report_mod._parse_sections(text)

    tg = out["reports"]["sihwang"]
    assert report_mod.tg_format.FOLD_OPEN in tg
    assert "[!note]" not in tg
    assert "· 연기금: +10" not in tg.split(report_mod.tg_format.FOLD_OPEN)[0]
    assert "· 연기금: +10" in tg   # 접기 블록 안에는 남아 있음

    archive = out["reports"]["sihwang_md"]
    assert "[!note]" not in archive
    assert "· 연기금: +10" in archive
    assert report_mod.tg_format.FOLD_OPEN not in archive


def test_us_ctx_mark_gets_folded_for_kr():
    text = """===TITLE===
키워드
===SIHWANG===
국내장 전망 내용입니다.
---미장참고---
미국장 정리 내용입니다.
===END==="""
    out = report_mod._parse_sections(text)
    md = out["markdown_report"]
    assert "국내장 전망 내용입니다." in md
    before_fold = md.split(report_mod.tg_format.FOLD_OPEN)[0]
    assert "미국장 정리 내용입니다." not in before_fold


def test_us_ctx_mark_mid_document_keeps_later_sections():
    """2026-08-01 로컬 재현 버그: `---미장참고---`가 1번 섹션 중간에 있으면
    "마커 뒤 전부 접기"로 구현했을 때 2)~5) 섹션이 통째로 접기 블록 안에
    빨려 들어가거나(오접기) 사라졌다(유실). 다음 번호 헤딩(`2)` 등) 앞에서
    접기를 끊고, 그 뒤 섹션은 원래 위치에 그대로 남아야 한다."""
    text = """===TITLE===
키워드
===SIHWANG===
1) 📌 3protv 요약
- 국내장 전망: 코스피 강보합
- 미국장: 나스닥 강세
---미장참고---
전일 다우 +0.4%, 나스닥 +0.9%

2) 💹 주요 지수
- 코스피 3120 (+0.4%)

3) 🖼 8시 전후 캡처화면 정리
- 07:55 창신메모리 이슈

5) 📊 수급·변동성
- 개인 -500억 / 외국인 +300억
===END==="""
    out = report_mod._parse_sections(text)
    md = out["markdown_report"]
    # 접기 블록 안: 마커 다음 내용만
    assert tg_format.FOLD_OPEN in md
    fold_body = md.split(tg_format.FOLD_OPEN, 1)[1].split(tg_format.FOLD_CLOSE, 1)[0]
    assert "다우 +0.4%" in fold_body
    # 2)~5) 섹션은 접기 블록 밖(뒤)에 그대로 남아 있어야 한다 — 유실도, 오접기도 안 됨
    after_fold = md.split(tg_format.FOLD_CLOSE, 1)[1]
    assert "2) 💹 주요 지수" in after_fold
    assert "3) 🖼 8시 전후 캡처화면 정리" in after_fold
    assert "5) 📊 수급·변동성" in after_fold
    assert "코스피 3120" in after_fold
    assert "창신메모리" in after_fold
    assert "개인 -500억" in after_fold


def test_capture_link_rows_are_collected_and_folded():
    text = """===TITLE===
키워드
===SIHWANG===
**05:55**
Bloom Energy 실적 발표
🔗 [Bloom Energy](https://a.b/1)
**05:59**
Micron 실적 우려
🔗 [Micron](https://a.b/2)
===END==="""
    out = report_mod._parse_sections(text)
    md = out["markdown_report"]
    before_fold = md.split(tg_format.FOLD_OPEN)[0]
    assert "🔗 " not in before_fold          # 본문에는 링크 줄이 안 남는다
    assert md.count("🔗 ") == 2              # 접기 블록 안에는 그대로 2개
    assert "Bloom Energy 실적 발표" in md
    assert "Micron 실적 우려" in md


def test_no_fold_markers_leaves_text_unchanged():
    """마커가 없는 정상 출력은 그대로 통과해야 한다(회귀 방지)."""
    text = """===TITLE===
키워드
===SIHWANG===
평범한 시황 내용
===END==="""
    out = report_mod._parse_sections(text)
    assert out["markdown_report"] == "평범한 시황 내용"


def test_mention_line_uses_mic_emoji_only():
    """언급 여부는 🎤 하나로만 — "(언급 없음)"은 줄만 늘려 걷어냈다(2026-08-02)."""
    hit = report_mod.mention_line(
        {"name": "마이크론", "mentioned": True, "context": "메타와 마진율 비교"})
    miss = report_mod.mention_line({"name": "삼성전자", "mentioned": False, "context": ""})
    assert hit == "• 🎤 마이크론"          # 맥락 문구는 붙지 않는다
    assert miss == "• 삼성전자"            # 미언급은 아무 표시도 없다
    assert "언급 없음" not in miss
    assert "📡" not in hit


def test_watchlist_prompt_forbids_no_mention_text():
    """프롬프트가 LLM에게 같은 규칙을 지시하는지 — 실제 리포트는 LLM이 쓴다."""
    src = Path(report_mod.__file__).read_text(encoding="utf-8")
    assert "`(언급 없음)`이라고 쓰면 안 됩니다" in src
    assert "줄 앞에 **🎤** 하나만 붙이고 **맥락은 쓰지 마세요.**" in src


def test_capture_prompt_merges_symbols_and_drops_time():
    """캡처 섹션: 같은 종목 합치기 + 시각 미표기 지시가 살아 있는지."""
    src = Path(report_mod.__file__).read_text(encoding="utf-8")
    assert "**같은 종목은 한 덩어리로 합치고, 시각(HH:MM)은 쓰지 마세요.**" in src
    assert "⚠️ **시각은 쓰지 마세요**" in src
