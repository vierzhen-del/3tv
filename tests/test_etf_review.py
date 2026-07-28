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
    # 기준선(median)이 1.0이 되도록 안 움직인 종목을 둔다 — 실제 바스켓과 같은 모양
    prev = [_row("A", "엔비디아", 100, 40.0), _row("B", "마이크론", 200, 40.0),
            _row("C", "애플", 100, 20.0)]
    today = [_row("A", "엔비디아", 150, 45.0), _row("B", "마이크론", 120, 35.0),
             _row("C", "애플", 100, 20.0)]
    d = market.etf_pdf_diff(today, prev)
    assert [m["name"] for m in d["buys"]] == ["엔비디아"]
    assert [m["name"] for m in d["sells"]] == ["마이크론"]
    assert d["buys"][0]["dq"] == 50
    assert d["sells"][0]["dq"] == -80
    assert d["buys"][0]["dw"] == 5.0        # 비중 변화도 함께 실린다
    assert abs(d["basket_shift"]) < 1e-9    # 바스켓 자체는 안 움직였다


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
    prev = [_row("A", "엔비디아", 100, 40.0), _row("B", "마이크론", 200, 40.0),
            _row("C", "애플", 100, 20.0)]
    today = [_row("A", "엔비디아", 150, 45.0), _row("B", "마이크론", 120, 35.0),
             _row("C", "애플", 100, 20.0)]
    d = market.etf_pdf_diff(today, prev)
    out = report_mod.generate_etf_review(
        SETTINGS, [{"name": "TIME 미국나스닥100액티브", "ticker": "426030",
                    "diff": d, "prev_date": "20260727"}])
    md = out["markdown_report"]
    assert "TIME 미국나스닥100액티브" in md and "426030" in md
    assert "매수 1 / 매도 1" in md
    assert "07/27 대비" in md
    assert "엔비디아 +50주" in md
    assert "마이크론 −80주" in md
    assert "설정단위" not in md            # 바스켓은 안 움직였으니 안내가 뜨면 안 된다
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
    # 7종목은 그대로(=기준선), 3종목만 실제로 늘렸다
    prev = [_row(str(i), f"종목{i}", 100, 10.0) for i in range(10)]
    today = [_row(str(i), f"종목{i}", 100 + (20 if i < 3 else 0), 10.0)
             for i in range(10)]
    d = market.etf_pdf_diff(today, prev, top=2)
    assert len(d["buys"]) == 2
    assert d["n_buys"] == 3             # 표시는 2개지만 집계는 전체


# ── 2026-07-28 KRX 실조회에서 드러난 두 결함의 회귀 방지 ──

def test_basket_wide_rescale_is_not_counted_as_trades():
    """0015B0 실측: 50종목 중 46종목이 −0.1~−0.5주씩 줄었다. 이건 46번의 매도
    판단이 아니라 설정단위(CU) 바스켓 전체가 리스케일된 것이다."""
    prev = [_row(str(i), f"종목{i}", 100.0, 2.0) for i in range(50)]
    today = [_row(str(i), f"종목{i}", 99.7, 2.0) for i in range(50)]   # 전부 -0.3%
    d = market.etf_pdf_diff(today, prev)
    assert d["n_buys"] == 0 and d["n_sells"] == 0
    assert d["basket_shift"] < 0


def test_trade_is_still_caught_inside_a_rescaled_basket():
    """바스켓이 통째로 줄어드는 와중에도 유독 크게 판 종목은 잡아내야 한다."""
    prev = [_row(str(i), f"종목{i}", 100.0, 2.0) for i in range(50)]
    today = [_row(str(i), f"종목{i}", 99.7, 2.0) for i in range(50)]
    today[7]["qty"] = 50.0             # 이 종목만 절반으로 감축 = 실제 매도
    d = market.etf_pdf_diff(today, prev)
    assert [m["name"] for m in d["sells"]] == ["종목7"]
    assert d["n_sells"] == 1


def test_weight_derived_from_amount_when_krx_reports_zero(monkeypatch):
    """KRX는 해외 상장 구성종목의 '비중'을 0으로 준다(426030·0015B0 실측).
    금액은 정상이므로 금액 기준으로 환산해야 리포트가 0.00%로 도배되지 않는다."""
    import pandas as pd

    df = pd.DataFrame(
        {"구성종목명": ["NVIDIA CORP", "AMAZON.COM INC"],
         "계약수": [10, 5], "금액": [750, 250], "시가총액": [0, 0], "비중": [0.0, 0.0]},
        index=["NVDA", "AMZN"])

    class _Stub:
        def get_etf_portfolio_deposit_file(self, *a, **k):
            return df

    monkeypatch.setattr(market, "_pykrx_stock", lambda: _Stub())
    rows = market.etf_pdf("426030", "20260728")
    assert [round(r["weight"], 1) for r in rows] == [75.0, 25.0]


def test_real_weight_is_not_overwritten(monkeypatch):
    """국내주식 ETF처럼 비중이 정상으로 오면 건드리지 않는다."""
    import pandas as pd

    df = pd.DataFrame(
        {"구성종목명": ["삼성전자", "SK하이닉스"], "계약수": [10, 5],
         "금액": [750, 250], "비중": [60.0, 40.0]},
        index=["005930", "000660"])

    class _Stub:
        def get_etf_portfolio_deposit_file(self, *a, **k):
            return df

    monkeypatch.setattr(market, "_pykrx_stock", lambda: _Stub())
    rows = market.etf_pdf("441800", "20260728")
    assert [r["weight"] for r in rows] == [60.0, 40.0]


def test_review_flags_basket_rescale_in_text():
    prev = [_row(str(i), f"종목{i}", 100.0, 2.0) for i in range(50)]
    today = [_row(str(i), f"종목{i}", 95.0, 2.0) for i in range(50)]   # -5%
    d = market.etf_pdf_diff(today, prev)
    out = report_mod.generate_etf_review(
        SETTINGS, [{"name": "KOACT", "ticker": "0015B0", "diff": d,
                    "prev_date": "20260727"}])
    assert "설정단위" in out["markdown_report"]
    assert "개별 매매 아님" in out["markdown_report"]


def test_weight_blank_when_krx_gives_nothing(monkeypatch):
    """비중·금액·시가총액이 전부 0이면 0.00%를 찍지 말고 비워야 한다 —
    '비중이 0인 종목'이라는 없는 사실을 알리게 되기 때문(0223R0 실측)."""
    import pandas as pd

    df = pd.DataFrame(
        {"구성종목명": ["ALPHABET INC-CL A", "AMAZON.COM INC"],
         "계약수": [3.0, 2.0], "금액": [0, 0], "시가총액": [0, 0], "비중": [0.0, 0.0]},
        index=["GOOGL", "AMZN"])

    class _Stub:
        def get_etf_portfolio_deposit_file(self, *a, **k):
            return df

    monkeypatch.setattr(market, "_pykrx_stock", lambda: _Stub())
    rows = market.etf_pdf("0223R0", "20260728")
    assert all(r["weight"] is None for r in rows)


def test_review_omits_weight_when_unavailable():
    prev = [{"code": "A", "name": "ALPHABET", "qty": 3.0, "amount": None,
             "mktcap": None, "weight": None},
            {"code": "B", "name": "AMAZON", "qty": 2.0, "amount": None,
             "mktcap": None, "weight": None},
            {"code": "C", "name": "MSFT", "qty": 5.0, "amount": None,
             "mktcap": None, "weight": None}]
    today = [dict(prev[0], qty=4.0), dict(prev[1]), dict(prev[2])]
    d = market.etf_pdf_diff(today, prev)
    out = report_mod.generate_etf_review(
        SETTINGS, [{"name": "TIGER 미국테크NYSE100액티브", "ticker": "0223R0",
                    "diff": d, "prev_date": "20260727"}])
    md = out["markdown_report"]
    assert "ALPHABET +1주" in md
    assert "비중 0.00%" not in md      # 없는 값을 0으로 찍으면 안 된다


def test_cash_row_does_not_zero_out_every_stock(monkeypatch):
    """PDF에 섞인 원화예금 행이 총액을 독차지하면 개별 종목이 전부 0.00%로
    환산된다(0223R0 실측). 과반 행에 값이 없으면 환산하지 말고 비워야 한다."""
    import pandas as pd

    df = pd.DataFrame(
        {"구성종목명": ["ALPHABET", "AMAZON", "MICROSOFT", "원화예금"],
         "계약수": [3.0, 2.0, 1.0, 1.0],
         "금액": [0, 0, 0, 5_000_000],       # 주식은 0, 예금만 값이 있다
         "시가총액": [0, 0, 0, 0],
         "비중": [0.0, 0.0, 0.0, 0.0]},
        index=["GOOGL", "AMZN", "MSFT", "KRW"])

    class _Stub:
        def get_etf_portfolio_deposit_file(self, *a, **k):
            return df

    monkeypatch.setattr(market, "_pykrx_stock", lambda: _Stub())
    rows = market.etf_pdf("0223R0", "20260728")
    assert all(r["weight"] is None for r in rows)


def test_weight_still_derived_when_most_rows_have_amounts(monkeypatch):
    """과반이 정상 금액이면 (예금 행이 하나 섞여 있어도) 환산은 계속 동작한다."""
    import pandas as pd

    df = pd.DataFrame(
        {"구성종목명": ["A", "B", "C", "원화예금"],
         "계약수": [1.0, 1.0, 1.0, 1.0],
         "금액": [300, 300, 300, 100],
         "시가총액": [0, 0, 0, 0],
         "비중": [0.0, 0.0, 0.0, 0.0]},
        index=["A", "B", "C", "KRW"])

    class _Stub:
        def get_etf_portfolio_deposit_file(self, *a, **k):
            return df

    monkeypatch.setattr(market, "_pykrx_stock", lambda: _Stub())
    rows = market.etf_pdf("441800", "20260728")
    assert [round(r["weight"], 0) for r in rows] == [30.0, 30.0, 30.0, 10.0]
