"""generate_report의 LLM 실패 흡수(열화 리포트) + 보유종목 언급 매칭 테스트.

2026-07-23 실장애 재현: Gemini가 429(prepayment depleted)로 완전히 막혀
비전 분석 0장 + LLM 리포트 불가 상태에서도 리포트가 발행돼야 한다.
"""
from __future__ import annotations

import json

import pytest

from threetv import report as report_mod

SETTINGS = {
    "sessions": {"us": {"label": "미국 시황"}, "kr": {"label": "한국 시황"}},
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
    {"name": "나스닥", "ticker": "^IXIC", "market": "US",
     "close": 20123.45, "change_pct": 1.23, "direction": "▲"},
    {"name": "KOSPI", "ticker": "^KS11", "market": "KR",
     "close": 3150.5, "change_pct": -0.42, "direction": "▼"},
]

HOLDINGS_QUOTES = [
    {"name": "삼성전자", "ticker": "005930", "market": "KR",
     "close": 88000, "change_pct": 2.1, "direction": "▲"},
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
    assert "[01:05]" in md and "엔비디아 신고가" in md


def test_successful_llm_path_unchanged(tmp_path, monkeypatch):
    """회귀 방지 — LLM이 정상이면 그 결과를 그대로 쓴다."""
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: json.dumps({
        "title_keyword": "반도체급등",
        "telegram_text": "요약 텍스트",
        "markdown_report": "## 상세 리포트",
        "holdings_mentioned": [{"name": "삼성전자", "mentioned": True, "context": "실적"}],
    }, ensure_ascii=False))
    data = _generate(tmp_path, transcript="무엇이든")
    assert data["title_keyword"] == "반도체급등"
    assert data["markdown_report"] == "## 상세 리포트"
    assert "AI 요약 없음" not in data["markdown_report"]


def test_parses_json_with_literal_newlines_in_strings():
    """2026-07-25 실장애 재현: LLM이 마크다운 본문에 실제 개행을 넣어 보낸 응답.

    strict=True면 'Invalid control character'로 죽어 리포트 전체가 버려졌다.
    """
    raw = (
        '{"title_keyword": "반도체강세",\n'
        ' "telegram_text": "첫 줄\n둘째 줄\n셋째 줄",\n'
        ' "markdown_report": "## 제목\n\n- 항목1\n- 항목2\n\n표\t들여쓰기",\n'
        ' "holdings_mentioned": []}'
    )
    data = report_mod._parse_json_obj(raw)
    assert data["title_keyword"] == "반도체강세"
    assert "둘째 줄" in data["telegram_text"]
    assert "항목1" in data["markdown_report"]


def test_multiline_llm_response_uses_llm_not_fallback(tmp_path, monkeypatch):
    """개행 포함 응답이 열화 경로로 새지 않고 정상 LLM 리포트로 채택돼야 한다."""
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: (
        '{"title_keyword": "AI반도체",\n'
        ' "telegram_text": "🇺🇸 미국 시황\n- 나스닥 상승\n- SOX 강세",\n'
        ' "markdown_report": "## 미국 시황\n\n다우 ▲0.27%\n나스닥 ▲1.30%",\n'
        ' "holdings_mentioned": [{"name": "삼성전자", "mentioned": true, "context": "언급"}]}'
    ))
    data = _generate(tmp_path, transcript="방송 전사")
    assert data["title_keyword"] == "AI반도체"
    assert "AI 요약 없음" not in data["markdown_report"]   # 열화 경로 아님
    assert "나스닥 ▲1.30%" in data["markdown_report"]


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


def test_missing_required_key_falls_back(tmp_path, monkeypatch):
    """LLM이 응답했으나 필수 키가 비면 열화 경로로 — 빈 리포트 발행 방지."""
    monkeypatch.setattr(report_mod, "_call_llm", lambda *a, **k: json.dumps(
        {"title_keyword": "키워드", "telegram_text": "", "markdown_report": "본문"}
    ))
    data = _generate(tmp_path, transcript="전사 내용")
    assert data["title_keyword"] == "원자료시황"
    assert "AI 요약 없음" in data["markdown_report"]
