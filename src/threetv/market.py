"""시세 조회·검증: yfinance(미국/지수) + pykrx(한국).

방송에서 추출된 종목 언급은 화면 OCR/전사 기반이라 숫자가 부정확할 수 있으므로
실제 API 시세(전일 종가·등락률)로 검증해 리포트에 사용한다.
"""
from __future__ import annotations

import functools
from datetime import datetime, timedelta

from .common import KST, log


def _fmt_quote(
    name: str, ticker: str, market: str, close: float, pct: float,
    asof: str = "", prev_close: float | None = None,
) -> dict:
    """시세 1건. asof/prev_close는 '어느 시점 종가인지'를 리포트에 명시하기 위한 것."""
    return {
        "name": name,
        "ticker": ticker,
        "market": market,
        "close": round(close, 2),
        "prev_close": round(prev_close, 2) if prev_close is not None else None,
        "change_pct": round(pct, 2),
        "direction": "▲" if pct > 0 else ("▼" if pct < 0 else "-"),
        "asof": asof,      # 종가 기준일 (YYYY-MM-DD)
    }


def us_quote(ticker: str, name: str | None = None) -> dict | None:
    """미국 종목/지수의 최근 종가와 전일 대비 등락률."""
    import yfinance as yf

    try:
        hist = yf.Ticker(ticker).history(period="5d", auto_adjust=False)
        if len(hist) < 2:
            return None
        close = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        pct = (close - prev) / prev * 100
        asof = str(hist.index[-1].date())
        return _fmt_quote(name or ticker, ticker, "US", close, pct, asof, prev)
    except Exception as e:
        log.debug("yfinance 조회 실패 %s: %s", ticker, e)
        return None


@functools.lru_cache(maxsize=1)
def _krx_name_map() -> dict[str, str]:
    """한국 종목명 → 6자리 코드 매핑 (KOSPI + KOSDAQ)."""
    from pykrx import stock

    date = datetime.now(KST).strftime("%Y%m%d")
    mapping: dict[str, str] = {}
    try:
        for mkt in ("KOSPI", "KOSDAQ"):
            for code in stock.get_market_ticker_list(date, market=mkt):
                mapping[stock.get_market_ticker_name(code)] = code
    except Exception as e:
        log.warning("KRX 종목 리스트 로딩 실패: %s", e)
    return mapping


def kr_resolve(name_or_code: str) -> str | None:
    """한국 종목명 또는 코드를 6자리 코드로 정규화."""
    s = name_or_code.strip()
    if s.isdigit() and len(s) == 6:
        return s
    return _krx_name_map().get(s)


def kr_quote(name_or_code: str, name: str | None = None) -> dict | None:
    """한국 종목의 최근 종가(장전이면 전일 종가)와 등락률."""
    from pykrx import stock

    code = kr_resolve(name_or_code)
    if not code:
        return None
    try:
        end = datetime.now(KST)
        start = end - timedelta(days=10)
        df = stock.get_market_ohlcv_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), code
        )
        if len(df) < 2:
            return None
        close = float(df["종가"].iloc[-1])
        prev = float(df["종가"].iloc[-2])
        pct = (close - prev) / prev * 100
        disp_name = name or stock.get_market_ticker_name(code)
        asof = str(df.index[-1].date())
        return _fmt_quote(disp_name, code, "KR", close, pct, asof, prev)
    except Exception as e:
        log.debug("pykrx 조회 실패 %s: %s", code, e)
        return None


def fetch_indices(indices_cfg: dict[str, str]) -> list[dict]:
    """설정된 주요 지수/자산 시세 일괄 조회."""
    quotes = []
    for ticker, name in indices_cfg.items():
        q = us_quote(ticker, name)
        if q:
            quotes.append(q)
    return quotes


def verify_mentions(mentions: list[dict]) -> list[dict]:
    """Claude가 추출한 언급 종목 [{name, market, ticker_guess}] 를 실시세로 검증.

    검증 성공 시 quote 필드가 채워지고, 실패 시 quote 없이 이름만 남긴다.
    """
    verified = []
    seen: set[str] = set()
    for m in mentions:
        name = (m.get("name") or "").strip()
        market = (m.get("market") or "").upper()
        guess = (m.get("ticker_guess") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)

        quote = None
        if market == "US" and guess:
            quote = us_quote(guess, name)
        elif market == "KR":
            quote = kr_quote(guess or name, name)
        verified.append({**m, "quote": quote})
    ok = sum(1 for v in verified if v["quote"])
    log.info("언급 종목 시세 검증: %d/%d 성공", ok, len(verified))
    return verified


def fetch_holdings_quotes(holdings: list[dict]) -> list[dict]:
    """보유/관심 종목의 현재 시세."""
    quotes = []
    for h in holdings:
        market = (h.get("market") or "").upper()
        ticker = str(h.get("ticker") or "")
        name = h.get("name") or ticker
        q = us_quote(ticker, name) if market == "US" else kr_quote(ticker, name)
        if q:
            quotes.append(q)
    return quotes
