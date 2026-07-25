"""시세 조회·검증: yfinance(미국/지수) + pykrx(한국).

방송에서 추출된 종목 언급은 화면 OCR/전사 기반이라 숫자가 부정확할 수 있으므로
실제 API 시세(전일 종가·등락률)로 검증해 리포트에 사용한다.
"""
from __future__ import annotations

import functools
import math
from datetime import datetime, timedelta

from .common import KST, log


def _fmt_quote(
    name: str, ticker: str, market: str, close: float, pct: float,
    asof: str = "", prev_close: float | None = None,
) -> dict | None:
    """시세 1건. asof/prev_close는 '어느 시점 종가인지'를 리포트에 명시하기 위한 것.

    ⚠️ close/pct가 NaN·inf면 None을 반환한다. yfinance는 스로틀링·데이터 결측 시
    예외 대신 **NaN이 담긴 행**을 돌려주는데, 그대로 통과시키면 리포트에
    'nan (-nan%)'이 그대로 실린다 (2026-07-25 텔레그램 실측 — 미국 지표 전부 nan).
    """
    if not (math.isfinite(close) and math.isfinite(pct)):
        log.warning("시세 값이 NaN/inf → 제외: %s (%s)", name, ticker)
        return None
    return {
        "name": name,
        "ticker": ticker,
        "market": market,
        "close": round(close, 2),
        "prev_close": round(prev_close, 2)
        if prev_close is not None and math.isfinite(prev_close) else None,
        "change_pct": round(pct, 2),
        "direction": "▲" if pct > 0 else ("▼" if pct < 0 else "-"),
        # 한눈에 등락을 보기 위한 아이콘 (텔레그램 가독성)
        "icon": "📈" if pct > 0 else ("📉" if pct < 0 else "➖"),
        "asof": asof,      # 종가 기준일 (YYYY-MM-DD)
    }


def _pair_from_closes(closes) -> tuple[float, float, str] | None:
    """종가 시계열에서 (최근 종가, 전일 종가, 기준일)을 뽑는다.

    NaN 행을 먼저 버리는 것이 핵심 — 휴장일·결측·스로틀링으로 생긴 NaN 행이
    마지막에 오면 그 값이 그대로 종가로 쓰인다.
    """
    s = closes.dropna()
    if len(s) < 2:
        return None
    close, prev = float(s.iloc[-1]), float(s.iloc[-2])
    if not (math.isfinite(close) and math.isfinite(prev)) or prev == 0:
        return None
    try:
        asof = str(s.index[-1].date())
    except Exception:
        asof = ""
    return close, prev, asof


def us_quotes_batch(tickers: dict[str, str]) -> list[dict]:
    """미국 티커 여러 개를 **한 번의 요청**으로 조회.

    티커마다 개별 요청을 보내면(33개) Yahoo가 공유 CI IP를 스로틀링해 NaN만
    돌려주는 일이 잦다 — 2026-07-25 실측 실패 원인. yf.download 배치 호출은
    요청 1건이라 이 문제를 근본적으로 줄인다.
    """
    import yfinance as yf

    symbols = list(tickers)
    if not symbols:
        return []
    try:
        df = yf.download(
            symbols, period="1mo", auto_adjust=False, group_by="ticker",
            progress=False, threads=False,
        )
    except Exception as e:
        log.warning("yfinance 배치 조회 실패 → 개별 조회로 폴백: %s", e)
        df = None

    quotes: list[dict] = []
    missing: list[str] = []
    for sym in symbols:
        closes = None
        if df is not None and len(df):
            try:
                # 여러 티커면 (ticker, field) MultiIndex, 1개면 평면 컬럼
                closes = df[sym]["Close"] if len(symbols) > 1 else df["Close"]
            except Exception:
                closes = None
        pair = _pair_from_closes(closes) if closes is not None else None
        if pair is None:
            missing.append(sym)
            continue
        close, prev, asof = pair
        q = _fmt_quote(tickers[sym], sym, "US", close, (close - prev) / prev * 100,
                       asof, prev)
        if q:
            quotes.append(q)

    # 배치에서 빠진 것만 개별 재시도 (요청 수를 최소로 유지)
    for sym in missing:
        q = us_quote(sym, tickers[sym])
        if q:
            quotes.append(q)
    if missing:
        got = {q["ticker"] for q in quotes}
        still = [s for s in missing if s not in got]
        if still:
            log.warning("시세 조회 실패(리포트에서 제외): %s", ", ".join(still))
    return quotes


def us_quote(ticker: str, name: str | None = None) -> dict | None:
    """미국 종목/지수의 최근 종가와 전일 대비 등락률 (개별 조회)."""
    import yfinance as yf

    try:
        hist = yf.Ticker(ticker).history(period="1mo", auto_adjust=False)
        if not len(hist):
            return None
        pair = _pair_from_closes(hist["Close"])
        if pair is None:
            log.debug("yfinance 유효 종가 부족 %s", ticker)
            return None
        close, prev, asof = pair
        return _fmt_quote(name or ticker, ticker, "US", close,
                          (close - prev) / prev * 100, asof, prev)
    except Exception as e:
        log.debug("yfinance 조회 실패 %s: %s", ticker, e)
        return None


@functools.lru_cache(maxsize=1)
def _pykrx_stock():
    """pykrx.stock 모듈을 안전하게 가져온다 (실패 시 None).

    ⚠️ pykrx는 **import 시점에** KRX 로그인 세션을 만든다(webio.py 모듈 레벨에서
    build_krx_session() 호출). KRX_ID/KRX_PW가 설정돼 있고 KRX가 JSON이 아닌 응답
    (점검 페이지·차단·일시 오류)을 주면 `from pykrx import stock` 자체가
    JSONDecodeError로 터져 **파이프라인 전체가 죽는다** — 2026-07-25 실측
    (verify_mentions에서 터져 리포트가 아예 생성되지 않았다).
    한 번만 시도하고 결과를 캐시해, 실패하면 국내 시세만 비고 계속 진행한다.
    """
    try:
        from pykrx import stock

        return stock
    except Exception as e:
        log.warning("pykrx 초기화 실패 — 국내 종목 시세를 건너뜁니다: %s", e)
        return None


@functools.lru_cache(maxsize=1)
def _krx_name_map() -> dict[str, str]:
    """한국 종목명 → 6자리 코드 매핑 (KOSPI + KOSDAQ)."""
    stock = _pykrx_stock()
    if stock is None:
        return {}

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
    stock = _pykrx_stock()
    if stock is None:
        return None

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
        pair = _pair_from_closes(df["종가"])
        if pair is None:
            return None
        close, prev, asof = pair
        disp_name = name or stock.get_market_ticker_name(code)
        return _fmt_quote(disp_name, code, "KR", close,
                          (close - prev) / prev * 100, asof, prev)
    except Exception as e:
        log.debug("pykrx 조회 실패 %s: %s", code, e)
        return None


def fetch_indices(indices_cfg: dict[str, str]) -> list[dict]:
    """설정된 주요 지수/자산 시세 일괄 조회 (요청 1건으로 배치 조회).

    설정 순서(방송 슬라이드 순서)를 그대로 유지해 리포트에서 화면과 대조하기 쉽게 한다.
    """
    quotes = us_quotes_batch(indices_cfg)
    order = {t: i for i, t in enumerate(indices_cfg)}
    quotes.sort(key=lambda q: order.get(q["ticker"], 999))
    log.info("주요 지표 조회: %d/%d건 성공", len(quotes), len(indices_cfg))
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
        verified.append({**m, "quote": quote, "news": fetch_news(name, market, guess)})
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


# ─────────────────────────── 관련 기사 링크 ───────────────────────────

def _news_item(raw: dict) -> dict | None:
    """yfinance 뉴스 1건을 {title,url,publisher}로 정규화.

    yfinance 1.x는 {'content': {'title', 'canonicalUrl': {'url'}, 'provider': {...}}},
    구버전은 {'title','link','publisher'} 평면 구조 — 둘 다 받는다.
    """
    c = raw.get("content") or raw
    title = (c.get("title") or "").strip()
    url = ""
    for key in ("canonicalUrl", "clickThroughUrl"):
        v = c.get(key)
        if isinstance(v, dict) and v.get("url"):
            url = v["url"]
            break
    url = url or c.get("link") or ""
    prov = c.get("provider")
    publisher = (prov.get("displayName") if isinstance(prov, dict) else None) \
        or c.get("publisher") or ""
    if not title or not url:
        return None
    return {"title": title[:160], "url": url, "publisher": publisher}


def search_links(name: str, market: str, ticker: str = "") -> list[dict]:
    """검색 링크 (네트워크 불필요 — 항상 제공되는 최소 보장 링크)."""
    from urllib.parse import quote_plus

    q = quote_plus(name)
    links = [{
        "title": f"{name} 뉴스 검색",
        "url": f"https://search.naver.com/search.naver?where=news&query={q}",
        "publisher": "네이버뉴스 검색",
    }]
    if market == "US" and ticker and ticker.isascii():
        links.append({
            "title": f"{ticker} 종목정보·뉴스",
            "url": f"https://finance.yahoo.com/quote/{ticker}/news",
            "publisher": "Yahoo Finance",
        })
    return links


def fetch_news(name: str, market: str, ticker: str = "", limit: int = 3) -> list[dict]:
    """언급 종목의 관련 기사.

    우선순위: ① 네이버 뉴스(한국어 기사, 국내·해외 종목 모두 커버)
             ② yfinance 뉴스(미국 종목 영문 기사)
             ③ 검색 링크 (네트워크 없이 생성 — 항상 최소 1건 보장)
    """
    from . import news as news_mod

    items: list[dict] = []
    if news_mod.enabled():
        items += news_mod.naver_news(name, display=limit)

    if len(items) < limit and market == "US" and ticker and ticker.isascii():
        try:
            import yfinance as yf

            for raw in (yf.Ticker(ticker).news or [])[: limit * 2]:
                it = _news_item(raw)
                if it:
                    items.append(it)
                if len(items) >= limit:
                    break
        except Exception as e:
            log.debug("뉴스 조회 실패 %s: %s", ticker, e)

    return items[:limit] + search_links(name, market, ticker)
