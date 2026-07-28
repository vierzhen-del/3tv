"""ETF 납입자산구성내역(PDF) 전일 대비 비교 + 리뷰 리포트 테스트.

핵심 회귀 방지 대상: **비중(%) 변화와 수량 변화를 섞지 않는 것**.
매니저가 아무 매매를 안 해도 담고 있는 종목 주가가 움직이면 비중은 저절로
변한다 — 그걸 "비중 축소"로 보고하면 없던 매매를 있었다고 알리는 셈이다.
"""
from __future__ import annotations

from threetv import market
from threetv import report as report_mod

SETTINGS = {"report": {"disclaimer": "※ 투자 참고용입니다."}}


def _row(code, name, qty, weight):
    return {"code": code, "name": name, "qty": qty, "amount": None, "weight": weight}


def test_diff_detects_added_and_removed():
    prev = [_row("A", "엔비디아", 100, 50.0), _row("B", "인텔", 100, 50.0)]
    today = [_row("A", "엔비디아", 100, 50.0), _row("C", "크레도", 100, 50.0)]
    d = market.etf_pdf_diff(today, prev)
    assert [r["name"] for r in d["added"]] == ["크레도"]
    assert [r["name"] for r in d["removed"]] == ["인텔"]


def test_weight_move_without_qty_change_is_not_a_trade():
    """주가만 움직여 비중이 바뀐 경우 — 매수/매도로 잡히면 안 된다."""
    prev = [_row("A", "엔비디아", 100, 60.0), _row("B", "애플", 100, 40.0)]
    today = [_row("A", "엔비디아", 100, 55.0), _row("B", "애플", 100, 45.0)]
    d = market.etf_pdf_diff(today, prev)
    assert d["buys"] == [] and d["sells"] == []
    assert d["n_buys"] == 0 and d["n_sells"] == 0
    # 비중 변화 자체는 별도로 남는다 (참고용)
    assert [m["name"] for m in d["weight_up"]] == ["애플"]
    assert [m["name"] for m in d["weight_down"]] == ["엔비디아"]


def test_qty_change_is_reported_as_trade():
    prev = [_row("A", "엔비디아", 100, 50.0), _row("B", "마이크론", 200, 50.0)]
    today = [_row("A", "엔비디아", 150, 55.0), _row("B", "마이크론", 120, 45.0)]
    d = market.etf_pdf_diff(today, prev)
    assert [m["name"] for m in d["buys"]] == ["엔비디아"]
    assert [m["name"] for m in d["sells"]] == ["마이크론"]
    assert d["buys"][0]["dq"] == 50
    assert d["sells"][0]["dq"] == -80
    assert d["buys"][0]["dw"] == 5.0        # 비중 변화도 함께 실린다


def test_diff_survives_missing_qty_column():
    """KRX가 수량을 안 주면 비중으로만 판정하되 has_qty=False로 표시한다."""
    prev = [_row("A", "엔비디아", None, 60.0)]
    today = [_row("A", "엔비디아", None, 65.0)]
    d = market.etf_pdf_diff(today, prev)
    assert d["has_qty"] is False
    assert [m["name"] for m in d["weight_up"]] == ["엔비디아"]


def test_review_marks_price_effect_when_qty_missing():
    """수량이 없으면 '주가 등락이 섞여 있다'는 경고가 반드시 붙어야 한다."""
    prev = [_row("A", "엔비디아", None, 60.0)]
    today = [_row("A", "엔비디아", None, 65.0)]
    d = market.etf_pdf_diff(today, prev)
    out = report_mod.generate_etf_review(
        SETTINGS, [{"name": "TIME", "ticker": "426030", "diff": d, "prev_date": "20260727"}])
    assert "주가 등락에 의한 변동이 섞여" in out["markdown_report"]


def test_review_renders_trades_and_counts():
    prev = [_row("A", "엔비디아", 100, 50.0), _row("B", "마이크론", 200, 50.0)]
    today = [_row("A", "엔비디아", 150, 55.0), _row("B", "마이크론", 120, 45.0)]
    d = market.etf_pdf_diff(today, prev)
    out = report_mod.generate_etf_review(
        SETTINGS, [{"name": "TIME 미국나스닥100액티브", "ticker": "426030",
                    "diff": d, "prev_date": "20260727"}])
    md = out["markdown_report"]
    assert "TIME 미국나스닥100액티브" in md and "426030" in md
    assert "순매수 1 / 순매도 1" in md
    assert "07/27 대비" in md
    assert "엔비디아 +50주" in md
    assert "마이크론 −80주" in md
    assert out["title_keyword"] == "ETF구성변화"


def test_review_handles_no_change():
    rows = [_row("A", "엔비디아", 100, 100.0)]
    d = market.etf_pdf_diff(rows, list(rows))
    out = report_mod.generate_etf_review(
        SETTINGS, [{"name": "TIME", "ticker": "426030", "diff": d, "prev_date": "20260727"}])
    assert "전일 대비 수량 변동 없음" in out["markdown_report"]


def test_review_handles_empty_results():
    out = report_mod.generate_etf_review(SETTINGS, [])
    assert out["markdown_report"]
    assert "조회된 ETF가 없습니다" in out["markdown_report"]


def test_etf_pdf_returns_empty_without_pykrx(monkeypatch):
    """pykrx가 죽어도 리포트 전체가 죽으면 안 된다."""
    monkeypatch.setattr(market, "_pykrx_stock", lambda: None)
    assert market.etf_pdf("426030", "20260728") == []


def test_etf_pdf_diff_top_limit():
    prev = [_row(str(i), f"종목{i}", 100, 10.0) for i in range(10)]
    today = [_row(str(i), f"종목{i}", 100 + (10 - i), 10.0) for i in range(10)]
    d = market.etf_pdf_diff(today, prev, top=3)
    assert len(d["buys"]) == 3
    assert d["n_buys"] == 10            # 표시는 3개지만 집계는 전체
