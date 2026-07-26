"""시세 조회 NaN 방어 + 배치 조회 파싱 + 관련 기사 링크 테스트.

2026-07-25 실장애 재현: 텔레그램 리포트의 미국 지표가 전부 'nan (-nan%)'으로 나갔다.
yfinance가 스로틀링·결측 시 예외 대신 NaN이 담긴 행을 돌려주는데 그대로 통과했던 것.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from threetv import market


def _idx(n):
    return pd.date_range("2026-07-20", periods=n, freq="D")


# ───────────────────────── NaN 방어 ─────────────────────────

def test_fmt_quote_rejects_nan():
    assert market._fmt_quote("나스닥", "^IXIC", "US", float("nan"), 1.2) is None
    assert market._fmt_quote("나스닥", "^IXIC", "US", 100.0, float("nan")) is None
    assert market._fmt_quote("나스닥", "^IXIC", "US", float("inf"), 1.2) is None


def test_fmt_quote_accepts_valid():
    q = market._fmt_quote("나스닥", "^IXIC", "US", 20123.456, 1.234, "2026-07-24", 19878.0)
    assert q["close"] == 20123.46 and q["change_pct"] == 1.23
    assert q["direction"] == "▲" and q["asof"] == "2026-07-24"


def test_pair_from_closes_drops_trailing_nan():
    """마지막 행이 NaN이면 그 값을 종가로 쓰면 안 된다 (실장애의 직접 원인)."""
    s = pd.Series([100.0, 110.0, np.nan], index=_idx(3))
    close, prev, asof = market._pair_from_closes(s)
    assert close == 110.0 and prev == 100.0
    assert asof == "2026-07-21"      # NaN 행 날짜가 아니라 유효한 마지막 날짜


def test_pair_from_closes_all_nan_returns_none():
    """스로틀링으로 전부 NaN이면 조회 실패로 처리 (nan 리포트 방지)."""
    s = pd.Series([np.nan, np.nan, np.nan], index=_idx(3))
    assert market._pair_from_closes(s) is None


def test_pair_from_closes_needs_two_points():
    assert market._pair_from_closes(pd.Series([100.0], index=_idx(1))) is None


def test_pair_from_closes_rejects_large_date_gap():
    """2026-07-25 실측: 결측이 많은 시계열의 두 점이 멀면 등락률이 터무니없어진다.

    같은 날 KOSPI ▲4.4% / KOSPI200 ▼7.18% 로 찍힌 원인.
    """
    idx = pd.to_datetime(["2026-06-01", "2026-07-24"])   # 53일 간격
    s = pd.Series([1000.0, 1080.0], index=idx)
    assert market._pair_from_closes(s) is None


def test_pair_from_closes_allows_weekend_gap():
    """금→월(3일)은 정상 전일대비로 인정해야 한다."""
    idx = pd.to_datetime(["2026-07-24", "2026-07-27"])
    s = pd.Series([100.0, 101.0], index=idx)
    got = market._pair_from_closes(s)
    assert got is not None and got[0] == 101.0


def test_pair_from_closes_rejects_zero_prev():
    """0으로 나누면 inf/nan이 된다."""
    s = pd.Series([0.0, 110.0], index=_idx(2))
    assert market._pair_from_closes(s) is None


# ───────────────────── 배치 조회 파싱 ─────────────────────

def _multi_df(data: dict[str, list]):
    """yf.download(group_by='ticker') 형태의 MultiIndex 프레임."""
    frames = {}
    for sym, closes in data.items():
        frames[(sym, "Close")] = closes
        frames[(sym, "Open")] = closes
    df = pd.DataFrame(frames, index=_idx(len(next(iter(data.values())))))
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


def test_batch_parses_multiindex_and_keeps_config_order(monkeypatch):
    df = _multi_df({
        "^IXIC": [19878.0, 20123.45],
        "^DJI": [44000.0, 44120.0],
    })
    monkeypatch.setattr(market, "us_quote", lambda *a, **k: None)
    import yfinance as yf
    monkeypatch.setattr(yf, "download", lambda *a, **k: df)

    quotes = market.fetch_indices({"^DJI": "다우존스", "^IXIC": "나스닥"})
    assert [q["name"] for q in quotes] == ["다우존스", "나스닥"]   # 설정 순서 유지
    nasdaq = next(q for q in quotes if q["ticker"] == "^IXIC")
    assert nasdaq["close"] == 20123.45 and nasdaq["direction"] == "▲"


def test_batch_excludes_all_nan_ticker(monkeypatch):
    """NaN만 오는 티커는 리포트에서 아예 빠져야 한다 (nan 표기 금지)."""
    df = _multi_df({
        "^IXIC": [19878.0, 20123.45],
        "BADSYM": [np.nan, np.nan],
    })
    monkeypatch.setattr(market, "us_quote", lambda *a, **k: None)
    import yfinance as yf
    monkeypatch.setattr(yf, "download", lambda *a, **k: df)

    quotes = market.fetch_indices({"^IXIC": "나스닥", "BADSYM": "없는지표"})
    assert [q["ticker"] for q in quotes] == ["^IXIC"]
    assert all("nan" not in str(q["close"]).lower() for q in quotes)


def test_batch_failure_falls_back_to_individual(monkeypatch):
    """배치가 예외로 죽어도 개별 조회로 복구된다."""
    import yfinance as yf
    monkeypatch.setattr(yf, "download", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("rate limited")))
    monkeypatch.setattr(
        market, "us_quote",
        lambda t, n=None: market._fmt_quote(n or t, t, "US", 100.0, 1.0, "2026-07-24", 99.0),
    )
    quotes = market.fetch_indices({"^IXIC": "나스닥"})
    assert len(quotes) == 1 and quotes[0]["close"] == 100.0


def test_batch_handles_single_ticker_flat_columns(monkeypatch):
    """티커 1개일 때 yfinance는 평면 컬럼을 준다."""
    df = pd.DataFrame({"Close": [99.0, 101.0]}, index=_idx(2))
    monkeypatch.setattr(market, "us_quote", lambda *a, **k: None)
    import yfinance as yf
    monkeypatch.setattr(yf, "download", lambda *a, **k: df)

    quotes = market.fetch_indices({"SOXL": "SOXL"})
    assert len(quotes) == 1 and quotes[0]["close"] == 101.0


# ─────────────────── pykrx import 실패 방어 ───────────────────

def test_pykrx_import_failure_returns_none_not_raise(monkeypatch):
    """2026-07-25 실장애 재현: pykrx는 import 시점에 KRX 로그인을 한다.

    KRX가 비-JSON 응답을 주면 import 자체가 JSONDecodeError로 터졌고, 그것이
    verify_mentions까지 올라가 파이프라인 전체를 죽였다 (리포트 미생성).
    """
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name.startswith("pykrx"):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return real_import(name, *a, **k)

    market._pykrx_stock.cache_clear()
    market._krx_name_map.cache_clear()
    monkeypatch.setattr(builtins, "__import__", boom)
    try:
        assert market._pykrx_stock() is None
        assert market.kr_quote("005930", "삼성전자") is None      # 예외 대신 None
        assert market._krx_name_map() == {}
    finally:
        market._pykrx_stock.cache_clear()
        market._krx_name_map.cache_clear()


def test_verify_mentions_survives_pykrx_failure(monkeypatch):
    """국내 종목이 섞여 있어도 파이프라인이 계속 진행돼야 한다."""
    monkeypatch.setattr(market, "_pykrx_stock", lambda: None)
    monkeypatch.setattr(market, "fetch_news", lambda *a, **k: [])
    monkeypatch.setattr(
        market, "us_quote",
        lambda t, n=None: market._fmt_quote(n or t, t, "US", 100.0, 1.0, "2026-07-24", 99.0),
    )
    got = market.verify_mentions([
        {"name": "삼성전자", "market": "KR", "ticker_guess": "005930"},
        {"name": "엔비디아", "market": "US", "ticker_guess": "NVDA"},
    ])
    assert len(got) == 2
    assert got[0]["quote"] is None          # 국내는 비고
    assert got[1]["quote"]["close"] == 100.0  # 미국은 정상


# ───────────────────── 관련 기사 링크 ─────────────────────

def test_search_links_always_available_without_network():
    links = market.search_links("삼성전자", "KR", "005930")
    assert links and all(l["url"].startswith("https://") for l in links)
    assert "삼성전자" in links[0]["title"]


def test_search_links_include_yahoo_for_us():
    links = market.search_links("엔비디아", "US", "NVDA")
    assert any("NVDA" in l["url"] for l in links)


def test_news_item_parses_new_yfinance_schema():
    raw = {"content": {
        "title": "Nvidia hits record high",
        "canonicalUrl": {"url": "https://finance.yahoo.com/news/nvda-1"},
        "provider": {"displayName": "Reuters"},
    }}
    got = market._news_item(raw)
    assert got["title"] == "Nvidia hits record high"
    assert got["url"] == "https://finance.yahoo.com/news/nvda-1"
    assert got["publisher"] == "Reuters"


def test_news_item_parses_legacy_schema():
    raw = {"title": "구형 스키마", "link": "https://example.com/a", "publisher": "AP"}
    got = market._news_item(raw)
    assert got["url"] == "https://example.com/a" and got["publisher"] == "AP"


def test_news_item_rejects_incomplete():
    assert market._news_item({"content": {"title": "제목만 있고 링크 없음"}}) is None
    assert market._news_item({}) is None


def test_fetch_news_falls_back_to_search_when_api_fails(monkeypatch):
    """뉴스 API가 죽어도 최소 검색 링크는 나와야 한다."""
    import yfinance as yf
    monkeypatch.setattr(yf, "Ticker", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("no news")))
    got = market.fetch_news("엔비디아", "US", "NVDA")
    assert got and all(g["url"].startswith("https://") for g in got)


def test_fetch_news_prepends_real_articles(monkeypatch):
    class FakeTicker:
        news = [{"content": {
            "title": "실제 기사",
            "canonicalUrl": {"url": "https://news/1"},
            "provider": {"displayName": "Reuters"},
        }}]

    import yfinance as yf
    monkeypatch.setattr(yf, "Ticker", lambda *a, **k: FakeTicker())
    got = market.fetch_news("엔비디아", "US", "NVDA")
    assert got[0]["title"] == "실제 기사"        # 실제 기사가 검색 링크보다 앞
    assert len(got) > 1                          # 검색 링크도 함께


def test_stale_asof_gets_flagged(monkeypatch):
    """대표 기준일보다 오래된 항목엔 stale 플래그 — 섹션 제목의 단일 날짜와 다르므로.

    2026-07-26 실측: 대표 7/24인데 KOSPI200만 7/16 데이터였다.
    """
    def fake_batch(cfg):
        out = []
        for t, n in cfg.items():
            asof = "2026-07-16" if t == "^KS200" else "2026-07-24"
            out.append(market._fmt_quote(n, t, "US", 100.0, 1.0, asof, 99.0))
        return out
    monkeypatch.setattr(market, "us_quotes_batch", fake_batch)
    quotes = market.fetch_indices(
        {"^DJI": "다우존스", "^IXIC": "나스닥", "^KS200": "KOSPI200"})
    by = {q["ticker"]: q for q in quotes}
    assert by["^KS200"].get("stale") is True
    assert "stale" not in by["^DJI"] and "stale" not in by["^IXIC"]


def test_newer_asof_not_marked_stale(monkeypatch):
    """대표보다 최신인 항목(환율 등)은 stale이 아니다."""
    def fake_batch(cfg):
        out = []
        for t, n in cfg.items():
            asof = "2026-07-26" if t == "KRW=X" else "2026-07-24"
            out.append(market._fmt_quote(n, t, "US", 100.0, 1.0, asof, 99.0))
        return out
    monkeypatch.setattr(market, "us_quotes_batch", fake_batch)
    quotes = market.fetch_indices(
        {"^DJI": "다우존스", "^IXIC": "나스닥", "KRW=X": "원/달러"})
    by = {q["ticker"]: q for q in quotes}
    assert "stale" not in by["KRW=X"]
