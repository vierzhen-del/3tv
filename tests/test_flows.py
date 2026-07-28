"""수급 섹션 회귀 테스트 — 2026-07-29 실물 스크린샷 지적 3건."""
from __future__ import annotations

from threetv import market
from threetv import report as report_mod


def test_etf_movers_resolve_names_not_tickers(monkeypatch):
    """`get_etf_price_change_by_ticker`는 종목명 컬럼을 안 준다 — 그대로 두면
    리포트에 '0197X0 (29.67%)'처럼 코드가 실린다(실측)."""
    import pandas as pd

    df = pd.DataFrame({"등락률": [29.67, -37.16]}, index=["0197X0", "488080"])
    names = {"0197X0": "KODEX 코스닥150레버리지", "488080": "TIGER 반도체TOP10레버리지"}

    class _Stub:
        def get_etf_price_change_by_ticker(self, *a, **k):
            return df

        def get_etf_ticker_name(self, t):
            return names[t]

        def get_nearest_business_day_in_a_week(self, d, prev=True):
            return "20260728"

    monkeypatch.setattr(market, "_pykrx_stock", lambda: _Stub())
    monkeypatch.setattr(market, "_etf_name_map", lambda: {})   # 개별 조회 경로 검증
    market.etf_name.cache_clear()
    market.last_biz_days.cache_clear()
    out = market.kr_etf_top_movers(n=5)
    assert out["up"][0]["name"] == "KODEX 코스닥150레버리지"
    assert out["down"][0]["name"] == "TIGER 반도체TOP10레버리지"
    assert out["up"][0]["ticker"] == "0197X0"


def test_etf_name_falls_back_to_ticker(monkeypatch):
    class _Stub:
        def get_etf_ticker_name(self, t):
            raise RuntimeError("KRX down")

    monkeypatch.setattr(market, "_pykrx_stock", lambda: _Stub())
    monkeypatch.setattr(market, "_etf_name_map", lambda: {})
    market.etf_name.cache_clear()
    assert market.etf_name("0197X0") == "0197X0"


def test_flow_summary_has_delta_vs_previous_day(monkeypatch):
    """개인/외국인/기관 + 전일대비 증감."""
    per_day = {
        "20260728": [{"investor": "개인", "net": 1000.0},
                     {"investor": "외국인", "net": -800.0},
                     {"investor": "기관합계", "net": -200.0},
                     {"investor": "연기금 등", "net": 50.0}],
        "20260727": [{"investor": "개인", "net": 400.0},
                     {"investor": "외국인", "net": -1200.0},
                     {"investor": "기관합계", "net": 100.0},
                     {"investor": "연기금 등", "net": 10.0}],
    }
    monkeypatch.setattr(market, "last_biz_days", lambda n=2: ["20260728", "20260727"])
    monkeypatch.setattr(market, "kr_investor_flows",
                        lambda mkt="KOSPI", day=None: per_day[day])
    s = market.kr_flow_summary()
    assert [r["investor"] for r in s["main"]] == ["개인", "외국인", "기관합계"]
    assert s["main"][0]["delta"] == 600.0        # 개인 순매수 확대
    assert s["main"][1]["delta"] == 400.0        # 외국인 순매도 축소 → +
    assert s["main"][2]["delta"] == -300.0
    assert [r["investor"] for r in s["others"]] == ["연기금 등"]


def test_flow_lines_one_item_per_line():
    """쉼표로 이어붙이면 텔레그램에서 한 문단이 돼 못 읽는다."""
    flows = {
        "summary": {"date": "20260728", "prev_date": "20260727",
                    "main": [{"investor": "개인", "net": 1000.0, "delta": 600.0},
                             {"investor": "외국인", "net": -800.0, "delta": 400.0},
                             {"investor": "기관합계", "net": -200.0, "delta": None}],
                    "others": [{"investor": "연기금 등", "net": 50.0, "delta": 40.0}]},
        "etf": {"up": [{"name": "KODEX 코스닥150레버리지", "ticker": "0197X0", "pct": 29.67},
                       {"name": "TIGER 200", "ticker": "102110", "pct": 3.2}],
                "down": [{"name": "TIGER 반도체TOP10레버리지", "ticker": "488080",
                          "pct": -37.16}]},
    }
    out = report_mod._flow_lines(flows)
    assert "  · KODEX 코스닥150레버리지: +29.67%" in out
    assert "  · TIGER 반도체TOP10레버리지: -37.16%" in out
    assert "0197X0" not in out                      # 코드는 본문에 안 나온다
    assert "07/28" in out                           # 기준일 명시
    assert "  · 개인: +1,000 (전일比 +600)" in out
    assert "  · 외국인: -800 (전일比 +400)" in out
    assert "  · 기관합계: -200" in out               # delta 없으면 괄호도 없다
    assert "(전일比" not in out.split("기관합계: -200")[1].split("\n")[0]


def test_flow_lines_without_summary_still_renders():
    """summary 조회가 실패해도 investors 원자료로 폴백한다."""
    out = report_mod._flow_lines({"investors": [{"investor": "개인", "net": 10.0}]})
    assert "  · 개인: +10" in out


def test_etf_name_map_is_built_once(monkeypatch):
    """pykrx의 get_etf_ticker_name은 호출마다 전종목 목록을 3번 새로 받는다.
    종목 20개면 60요청 — 맵을 한 번만 만들어 재사용해야 한다."""
    calls = {"n": 0}

    class _Stub:
        def get_etf_ticker_name(self, t):
            calls["n"] += 1
            return "개별조회로_내려옴"

    monkeypatch.setattr(market, "_pykrx_stock", lambda: _Stub())
    monkeypatch.setattr(market, "_etf_name_map",
                        lambda: {"0197X0": "KODEX 코스닥150레버리지",
                                 "0193L0": "TIGER 200선물인버스2X"})
    market.etf_name.cache_clear()
    assert market.etf_name("0197X0") == "KODEX 코스닥150레버리지"
    assert market.etf_name("0193L0") == "TIGER 200선물인버스2X"
    assert calls["n"] == 0            # 맵에 있으면 개별 조회를 하지 않는다


def test_etf_name_falls_back_to_individual_lookup(monkeypatch):
    """맵 생성이 실패해도(pykrx 내부구조 변경) 공개 API로 이름을 채운다."""
    class _Stub:
        def get_etf_ticker_name(self, t):
            return "TIGER 반도체TOP10레버리지"

    monkeypatch.setattr(market, "_pykrx_stock", lambda: _Stub())
    monkeypatch.setattr(market, "_etf_name_map", lambda: {})
    market.etf_name.cache_clear()
    assert market.etf_name("488080") == "TIGER 반도체TOP10레버리지"
