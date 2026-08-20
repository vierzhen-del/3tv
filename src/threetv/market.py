"""시세 조회·검증: yfinance(미국/지수) + pykrx(한국).

방송에서 추출된 종목 언급은 화면 OCR/전사 기반이라 숫자가 부정확할 수 있으므로
실제 API 시세(전일 종가·등락률)로 검증해 리포트에 사용한다.
"""
from __future__ import annotations

import functools
import math
import statistics
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


MAX_GAP_DAYS = 7   # 금→월(3일) + 연휴를 흡수하되 데이터 공백은 걸러낼 정도


def _pair_from_closes(closes) -> tuple[float, float, str] | None:
    """종가 시계열에서 (최근 종가, 전일 종가, 기준일)을 뽑는다.

    NaN 행을 먼저 버리는 것이 핵심 — 휴장일·결측·스로틀링으로 생긴 NaN 행이
    마지막에 오면 그 값이 그대로 종가로 쓰인다.

    ⚠️ 유효한 두 종가의 **날짜 간격도 검사**한다. 결측이 많은 시계열은 dropna 후
    남은 두 점이 며칠~몇 주 떨어져 있을 수 있고, 그걸 '전일대비'로 계산하면
    터무니없는 등락률이 나온다 — 2026-07-25 실측: 같은 날 KOSPI ▲4.4% 인데
    KOSPI200 ▼7.18% 로 찍혔다(물리적으로 불가능). 간격이 크면 조회 실패로 처리한다.
    """
    s = closes.dropna()
    if len(s) < 2:
        return None
    close, prev = float(s.iloc[-1]), float(s.iloc[-2])
    if not (math.isfinite(close) and math.isfinite(prev)) or prev == 0:
        return None
    asof = ""
    try:
        d_last, d_prev = s.index[-1].date(), s.index[-2].date()
        asof = str(d_last)
        gap = (d_last - d_prev).days
        if gap > MAX_GAP_DAYS:
            log.warning("종가 간격 %d일 — 전일대비로 볼 수 없어 제외 (%s vs %s)",
                        gap, d_prev, d_last)
            return None
    except Exception:
        pass
    return close, prev, asof


# settings.yaml의 market.indices엔 국내 지수도 yfinance 티커로 섞여 있다
# (KOSPI/KOSDAQ은 pykrx가 아니라 Yahoo `^KS11`/`^KQ11`로 조회) — 이 배치 함수가
# 전부 "US"로 찍으면 정오(noon) 세션의 `market == "KR"` 필터가 KOSPI/KOSDAQ을
# 영영 못 찾는다(2026-08-20 실측: "장중 KR 지수: 조회 실패"가 매일 재현 —
# 조회 자체는 35/36건 성공했는데 market 태그가 전부 US라 필터에서 다 걸러졌다).
_KR_INDEX_TICKERS = {"^KS11", "^KQ11", "^KS200"}

# fetch_indices()의 stale 표기에서 빼는 티커 — KOSPI/KOSDAQ만. KOSPI200은
# 넣지 않는다(fetch_indices() 참고: 진짜 데이터 결측 전례가 있어 계속 감시 대상).
_STALE_EXEMPT_TICKERS = {"^KS11", "^KQ11"}


def us_quotes_batch(tickers: dict[str, str]) -> list[dict]:
    """미국(+ Yahoo 티커로 조회하는 국내 지수) 여러 개를 **한 번의 요청**으로 조회.

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
        market = "KR" if sym in _KR_INDEX_TICKERS else "US"
        q = _fmt_quote(tickers[sym], sym, market, close, (close - prev) / prev * 100,
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
    """Yahoo 티커 종목/지수의 최근 종가와 전일 대비 등락률 (개별 조회).

    이름과 달리 미국 전용이 아니다 — us_quotes_batch()가 배치 실패분을 재시도할
    때도 이 함수를 쓰는데, KOSPI/KOSDAQ도 그 배치에 섞여 있다. market 태그를
    US로 고정하면 배치가 실패한 날만 국내 지수가 US로 잘못 찍힌다.
    """
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
        market = "KR" if ticker in _KR_INDEX_TICKERS else "US"
        return _fmt_quote(name or ticker, ticker, market, close,
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

    # 기준일이 섞여 있으면 섹션 제목의 단일 기준일 표기가 오해를 부른다.
    # 대표 기준일보다 오래된 항목엔 stale 플래그를 달아, 리포트가 그 항목만
    # 자기 날짜를 함께 쓰도록 한다 (2026-07-26 실측: KOSPI200이 8일 낡은 7/16 데이터).
    #
    # ⚠️ KOSPI/KOSDAQ(^KS11/^KQ11)은 예외 — us/kr 세션이 한국장 개장 전(05~08시
    # KST)에 도는 구조상 그 시각의 "최신" 종가는 항상 전날 것일 수밖에 없다.
    # 이건 조회 실패나 데이터 결측이 아니라 세션 타이밍 때문이라 매일 재현되는데,
    # stale 표기를 달면 매일 KOSPI/KOSDAQ만 경고가 붙어 다른 지수와 다르게
    # 보인다. 2026-08-21 사용자 확정 — "타지수처럼" 종가 기준 전일대비만
    # 보여주고 날짜 경고는 달지 않는다(수급/지수 최신성이 중요하면 noon 세션의
    # "장중 KR 지수"가 그 시각 라이브 재조회로 보완한다).
    # KOSPI200(^KS200)은 이 예외에서 뺀다 — 2026-07-26 실측으로 8일 낡은 데이터가
    # 잡힌 전례가 있어(세션 타이밍이 아니라 진짜 데이터 결측), 계속 감시해야 한다.
    dates = [q["asof"] for q in quotes
            if q.get("asof") and q["ticker"] not in _STALE_EXEMPT_TICKERS]
    if dates:
        common = max(set(dates), key=dates.count)
        mismatched = []
        for q in quotes:
            if q["ticker"] in _STALE_EXEMPT_TICKERS:
                continue
            a = q.get("asof")
            if not a or a == common:
                continue
            mismatched.append(f"{q['name']}({a})")
            if a < common:      # ISO 날짜라 문자열 비교로 충분
                q["stale"] = True
        if mismatched:
            log.warning("기준일 불일치 (대표 %s): %s", common, ", ".join(mismatched[:10]))
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


# ─────────────────────── 수급 동향 (전일 국내) ───────────────────────

def _recent_biz_range(days: int = 10) -> tuple[str, str]:
    end = datetime.now(KST)
    return (end - timedelta(days=days)).strftime("%Y%m%d"), end.strftime("%Y%m%d")


@functools.lru_cache(maxsize=4)
def last_biz_days(n: int = 2) -> list[str]:
    """가장 최근 영업일부터 n개를 최신순으로 (['20260728', '20260727']).

    ⚠️ 이게 없어서 수급이 **10일 누적으로 나가던 버그**가 있었다 — pykrx의 수급
    함수는 (start, end) 구간 **합계**를 주는데 `_recent_biz_range()`가 10일 창을
    넘겨 "전일 수급"이라는 이름으로 10영업일치가 실렸다(2026-07-29 실측:
    외국인 -50,944억원 = -5조원, 개인 +67,539억원 — 하루치일 수 없는 규모).
    """
    stock = _pykrx_stock()
    if stock is None:
        return []
    days: list[str] = []
    cur = datetime.now(KST)
    for _ in range(n * 3 + 10):        # 연휴를 넉넉히 흡수
        if len(days) >= n:
            break
        try:
            d = stock.get_nearest_business_day_in_a_week(
                cur.strftime("%Y%m%d"), prev=True)
        except Exception as e:
            log.warning("영업일 조회 실패: %s", e)
            return days
        if not d:
            break
        if d not in days:
            days.append(d)
        cur = datetime.strptime(d, "%Y%m%d") - timedelta(days=1)
    return days


def kr_investor_flows(market: str = "KOSPI", day: str | None = None) -> list[dict]:
    """**하루치** 수급주체별 순매수 (기관·외국인·개인 등).

    반환: [{"investor": "외국인", "net": 1234.5}] — 단위 억원, 순매수 큰 순.
    day를 안 주면 가장 최근 영업일. pykrx 실패·컬럼 변경에도 리포트가 죽지 않도록
    전부 best-effort.
    """
    stock = _pykrx_stock()
    if stock is None:
        return []
    if day is None:
        days = last_biz_days(1)
        if not days:
            return []
        day = days[0]
    start = end = day
    try:
        df = stock.get_market_trading_value_by_investor(start, end, market)
        if df is None or not len(df):
            return []
        col = next((c for c in ("순매수", "순매수거래대금") if c in df.columns), None)
        if col is None:
            log.warning("수급 동향: 순매수 컬럼을 찾지 못함 (%s)", list(df.columns)[:6])
            return []
        rows = []
        for name, val in df[col].items():
            try:
                v = float(val)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(v) or str(name).strip() in ("전체", "합계"):
                continue
            rows.append({"investor": str(name).strip(), "net": round(v / 1e8, 1)})
        rows.sort(key=lambda r: r["net"], reverse=True)
        log.info("%s 수급주체 동향: %d개 주체 (%s)", day, len(rows), market)
        return rows
    except Exception as e:
        log.warning("수급 동향 조회 실패: %s", e)
        return []


def kr_top_net_purchases(investor: str = "외국인", n: int = 10,
                         market: str = "KOSPI", day: str | None = None) -> dict:
    """수급주체 기준 **하루치** 순매수 상위 n / 순매도 상위 n 종목.

    반환: {"investor":..., "buy":[{name, net}], "sell":[{name, net}]} (단위 억원)
    """
    stock = _pykrx_stock()
    if stock is None:
        return {}
    if day is None:
        days = last_biz_days(1)
        if not days:
            return {}
        day = days[0]
    start = end = day
    try:
        df = stock.get_market_net_purchases_of_equities(start, end, market, investor)
        if df is None or not len(df):
            return {}
        col = next((c for c in ("순매수거래대금", "순매수") if c in df.columns), None)
        name_col = next((c for c in ("종목명",) if c in df.columns), None)
        if col is None:
            log.warning("순매수 상위: 컬럼 확인 실패 (%s)", list(df.columns)[:6])
            return {}
        items = []
        for idx, row in df.iterrows():
            try:
                v = float(row[col])
            except (TypeError, ValueError, KeyError):
                continue
            if not math.isfinite(v):
                continue
            nm = str(row[name_col]).strip() if name_col else str(idx).strip()
            items.append({"name": nm, "net": round(v / 1e8, 1)})
        items.sort(key=lambda r: r["net"], reverse=True)
        result = {"investor": investor, "buy": items[:n], "sell": items[-n:][::-1]}
        log.info("%s 순매수 상위 %d / 순매도 상위 %d",
                 investor, len(result["buy"]), len(result["sell"]))
        return result
    except Exception as e:
        log.warning("순매수 상위 조회 실패(%s): %s", investor, e)
        return {}


@functools.lru_cache(maxsize=1)
def _etf_name_map() -> dict[str, str]:
    """ETF/ETN/ELW 종목코드 → 종목명 맵을 **한 번만** 만든다.

    ⚠️ pykrx의 `get_etf_ticker_name()`은 호출할 때마다 `EtxTicker()`를 새로
    생성하고, 그 생성자가 ETF·ETN·ELW **전종목 목록을 매번 새로 받아온다(요청 3건)**.
    종목 20개면 60건이 나가 느리고 KRX 스로틀링을 부른다. 목록은 하루 안에 바뀌지
    않으므로 한 번만 받아 캐시한다.

    내부 모듈을 직접 쓰므로 pykrx 버전이 바뀌면 실패할 수 있다 — 그때는 빈 맵을
    돌려주고 `etf_name()`이 공개 API 개별 조회로 폴백한다(느릴 뿐 결과는 같다).
    """
    if _pykrx_stock() is None:
        return {}
    try:
        from pykrx.website.krx.etx.ticker import EtxTicker

        df = EtxTicker().df
        mapping = {str(t).strip(): str(n).strip()
                   for t, n in df["종목명"].items() if str(n).strip()}
        log.info("ETF 종목명 맵 %d건 로딩", len(mapping))
        return mapping
    except Exception as e:
        log.warning("ETF 종목명 맵 생성 실패 — 개별 조회로 폴백: %s", e)
        return {}


@functools.lru_cache(maxsize=512)
def etf_name(ticker: str) -> str:
    """ETF 종목코드 → 종목명. 실패하면 코드를 그대로 돌려준다.

    ⚠️ `get_etf_price_change_by_ticker`는 **종목명 컬럼을 주지 않는다** — 그래서
    리포트에 "0197X0 (29.1%)"처럼 코드가 그대로 실렸다(2026-07-29 텔레그램 실측).
    사람이 읽을 수 없으니 여기서 이름을 채운다.
    """
    name = _etf_name_map().get(ticker)
    if name:
        return name
    stock = _pykrx_stock()
    if stock is None:
        return ticker
    try:
        return (stock.get_etf_ticker_name(ticker) or "").strip() or ticker
    except Exception:
        log.debug("ETF 종목명 조회 실패: %s", ticker)
        return ticker


def kr_etf_top_movers(n: int = 10, day: str | None = None) -> dict:
    """**하루치** ETF 등락 상위/하위 (거래대금 있는 종목 기준).

    반환: {"up":[{name, ticker, pct}], "down":[...]}
    """
    stock = _pykrx_stock()
    if stock is None:
        return {}
    if day is None:
        days = last_biz_days(1)
        if not days:
            return {}
        day = days[0]
    start = end = day
    try:
        df = stock.get_etf_price_change_by_ticker(start, end)
        if df is None or not len(df):
            return {}
        pct_col = next((c for c in ("등락률",) if c in df.columns), None)
        if pct_col is None:
            log.warning("ETF 등락: 컬럼 확인 실패 (%s)", list(df.columns)[:6])
            return {}
        items = []
        for idx, row in df.iterrows():
            try:
                v = float(row[pct_col])
            except (TypeError, ValueError, KeyError):
                continue
            if not math.isfinite(v) or v == 0:
                continue
            code = str(idx).strip()
            nm = str(row.get("종목명") or "").strip() or etf_name(code)
            items.append({"name": nm, "ticker": code, "pct": round(v, 2)})
        items.sort(key=lambda r: r["pct"], reverse=True)
        result = {"up": items[:n], "down": items[-n:][::-1]}
        log.info("ETF 등락 상위 %d / 하위 %d", len(result["up"]), len(result["down"]))
        return result
    except Exception as e:
        log.warning("ETF 등락 조회 실패: %s", e)
        return {}


# ─────────────────── ETF 납입자산구성내역(PDF) ───────────────────
#
# 국내 ETF는 구성종목·수량을 매 영업일 KRX에 의무 공시한다(PDF = Portfolio
# Deposit File). 운용사 홈페이지를 긁을 필요 없이 pykrx로 공식 데이터를 받는다.
#
# ⚠️ 컬럼명은 KRX 개편 때 바뀔 수 있어 후보 목록에서 찾는다 — 못 찾으면 경고만
# 남기고 빈 목록을 돌려줘 리포트 전체가 죽지 않게 한다(기존 수급 함수와 같은 방침).

_PDF_NAME_COLS = ("종목명", "구성종목명")
_PDF_QTY_COLS = ("계약수", "주식수", "수량", "보유수량")
_PDF_WEIGHT_COLS = ("비중", "구성비중")
_PDF_AMOUNT_COLS = ("금액", "평가금액", "구성금액")
_PDF_MKTCAP_COLS = ("시가총액",)


def _pick_col(df, candidates: tuple[str, ...]) -> str | None:
    return next((c for c in candidates if c in df.columns), None)


def _num(v) -> float | None:
    try:
        f = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def etf_pdf(ticker: str, date_ymd: str) -> list[dict]:
    """ETF 납입자산구성내역 1일분 → [{code, name, qty, amount, weight}].

    비어 있으면 그날은 휴장이거나 아직 공시 전이다(둘 다 정상 상황).
    """
    stock = _pykrx_stock()
    if stock is None:
        return []
    try:
        df = stock.get_etf_portfolio_deposit_file(ticker, date_ymd)
    except Exception as e:
        log.warning("ETF PDF 조회 실패 %s %s: %s", ticker, date_ymd, e)
        return []
    if df is None or not len(df):
        return []

    name_col = _pick_col(df, _PDF_NAME_COLS)
    qty_col = _pick_col(df, _PDF_QTY_COLS)
    w_col = _pick_col(df, _PDF_WEIGHT_COLS)
    amt_col = _pick_col(df, _PDF_AMOUNT_COLS)
    cap_col = _pick_col(df, _PDF_MKTCAP_COLS)
    if qty_col is None and w_col is None:
        log.warning("ETF PDF 컬럼 확인 실패 %s: %s", ticker, list(df.columns)[:8])
        return []

    rows = []
    for idx, r in df.iterrows():
        code = str(idx).strip()
        name = str(r[name_col]).strip() if name_col else code
        if not code or code.lower() == "nan":
            continue
        rows.append({
            "code": code,
            "name": name or code,
            "qty": _num(r[qty_col]) if qty_col else None,
            "amount": _num(r[amt_col]) if amt_col else None,
            "mktcap": _num(r[cap_col]) if cap_col else None,
            "weight": _num(r[w_col]) if w_col else None,
        })

    # KRX는 **해외 상장 구성종목의 '비중'을 0으로 준다** (2026-07-28 실측:
    # 426030·0015B0·0223R0의 미국 주식은 전부 0.00, 국내주식만 담는 441800은 정상).
    # 금액이나 시가총액이 있으면 거기서 환산하고, 그것마저 없으면 **비중을 None으로
    # 비운다** — 0.00%를 그대로 찍으면 "비중이 0인 종목"이라는 없는 사실을 알리게 된다.
    # ⚠️ "합계가 0보다 크다"로 판단하면 안 된다. PDF에는 원화예금 같은 행이 섞여
    # 있어서 그 한 줄만 값을 갖고 나머지 종목은 0인 경우가 있다 — 합계는 통과하지만
    # 정작 종목 비중은 전부 0.00%가 된다(2026-07-28 0223R0 금액, 2026-07-29 472150
    # 비중에서 실측: 삼성전자가 22.84%→0.00%인데 8주를 매수한 모순이 나왔다).
    # 그래서 **과반 행이 실제 값을 가지는지**로 판단한다.
    half = max(1, len(rows) // 2)

    def _usable(key: str) -> bool:
        return sum(1 for r in rows if (r.get(key) or 0) > 0) >= half

    if not _usable("weight"):
        base = next((k for k in ("amount", "mktcap") if _usable(k)), None)
        if base:
            total = sum(r.get(base) or 0 for r in rows)
            for r in rows:
                r["weight"] = (r.get(base) or 0) / total * 100
            log.info("ETF PDF %s: 비중 미공시 → %s 기준으로 환산", ticker, base)
        else:
            for r in rows:
                r["weight"] = None
            log.warning("ETF PDF %s: 비중·금액·시가총액이 대부분 비어 있어 비중을 "
                        "표시하지 않습니다 (샘플: %s)", ticker, rows[0] if rows else "-")

    log.info("ETF PDF %s %s: %d종목 (컬럼 %s)", ticker, date_ymd, len(rows),
             list(df.columns)[:6])
    return rows


def etf_pdf_with_prev(ticker: str, date_ymd: str, max_back: int = 7
                      ) -> tuple[list[dict], list[dict], str]:
    """오늘 PDF + 직전 영업일 PDF → (today, prev, prev_date).

    직전 영업일은 달력이 아니라 **실제 공시가 있는 날**로 되짚는다(휴장·연휴 대응).
    오늘 것이 아직 없으면 today도 하루씩 되짚어 가장 최근 공시일을 오늘로 삼는다.
    """
    base = datetime.strptime(date_ymd, "%Y%m%d")
    today: list[dict] = []
    today_date = date_ymd
    for back in range(max_back):
        d = (base - timedelta(days=back)).strftime("%Y%m%d")
        rows = etf_pdf(ticker, d)
        if rows:
            today, today_date = rows, d
            break
    if not today:
        return [], [], ""

    tbase = datetime.strptime(today_date, "%Y%m%d")
    for back in range(1, max_back + 1):
        d = (tbase - timedelta(days=back)).strftime("%Y%m%d")
        rows = etf_pdf(ticker, d)
        if rows:
            return today, rows, d
    return today, [], ""


def etf_pdf_diff(today: list[dict], prev: list[dict], top: int = 5,
                 min_rel: float = 0.02) -> dict:
    """전일 대비 구성 변화.

    ⚠️ **비중(%)만 보면 안 된다** — 매니저가 아무 매매를 안 해도 담고 있는 종목의
    주가가 오르내리면 비중은 저절로 움직인다. 실제 '비중조절'(운용 판단에 따른
    매매)은 **계약수(수량) 변화**로만 판정할 수 있다. 그래서 수량 기준(실매매)과
    비중 기준(주가효과 포함)을 분리해 담는다.

    ⚠️ **수량 변화도 그대로 쓰면 안 된다** — PDF는 설정단위(CU) 1좌 기준 바스켓이라
    CU 자체가 조정되면 **전 종목 수량이 한꺼번에 같은 비율로** 움직인다. 2026-07-28
    실측에서 0015B0은 50종목 중 46종목이 −0.1~−0.5주씩 줄었는데, 이건 46번의 매도
    판단이 아니라 바스켓 전체 리스케일이다. 그래서 전 종목 수량비의 **중앙값**을
    바스켓 배율로 잡고, 거기서 `min_rel` 이상 벗어난 종목만 실제 매매로 센다.
    """
    tmap = {r["code"]: r for r in today}
    pmap = {r["code"]: r for r in prev}

    added = [tmap[c] for c in tmap.keys() - pmap.keys()]
    removed = [pmap[c] for c in pmap.keys() - tmap.keys()]
    added.sort(key=lambda r: r.get("weight") or 0, reverse=True)
    removed.sort(key=lambda r: r.get("weight") or 0, reverse=True)

    # 수량 공시 여부는 **컬럼 유무**로 판단한다 — 변동 건수로 판단하면 "수량은 있는데
    # 오늘 매매가 없었다"와 "KRX가 수량을 안 준다"가 구분되지 않아, 변동 없는 날
    # 리포트가 근거 없이 비중 기준으로 바뀌어버린다.
    has_qty = any(r.get("qty") is not None for r in today) and \
        any(r.get("qty") is not None for r in prev)

    common = tmap.keys() & pmap.keys()
    ratios = [tmap[c]["qty"] / pmap[c]["qty"] for c in common
              if tmap[c].get("qty") is not None and pmap[c].get("qty")]
    scale = statistics.median(ratios) if ratios else 1.0
    # 바스켓 전체가 1% 넘게 리스케일되면 그 사실 자체를 리포트에 알린다
    basket_shift = (scale - 1) * 100

    qty_moves, weight_moves = [], []
    for code in common:
        t, p = tmap[code], pmap[code]
        if t.get("qty") is not None and p.get("qty") is not None and p["qty"]:
            dq = t["qty"] - p["qty"]
            # 바스켓 배율을 걷어낸 '상대 변화' — 이게 실제 운용 판단이다
            rel = (t["qty"] / p["qty"]) / scale - 1 if scale else 0.0
            if dq and abs(rel) >= min_rel:
                qty_moves.append({
                    "name": t["name"], "code": code, "dq": dq,
                    "rel": rel * 100,
                    "weight": t.get("weight"),
                    "dw": (t["weight"] - p["weight"])
                    if t.get("weight") is not None and p.get("weight") is not None else None,
                })
        if t.get("weight") is not None and p.get("weight") is not None:
            dw = t["weight"] - p["weight"]
            if dw:
                weight_moves.append({"name": t["name"], "code": code,
                                     "dw": dw, "weight": t["weight"]})

    # 표시 순서는 수량이 아니라 **비중 규모**로 — 100주짜리 소형주보다 1주 움직인
    # 대형주가 포트폴리오에 더 큰 영향이다. 비중을 못 구한 ETF(해외형 일부)는
    # 차선책으로 상대 변화폭이 큰 순서로 보여준다.
    if any(m.get("weight") is not None for m in qty_moves):
        qty_moves.sort(key=lambda r: (r.get("weight") or 0), reverse=True)
    else:
        qty_moves.sort(key=lambda r: abs(r["rel"]), reverse=True)
    weight_moves.sort(key=lambda r: r["dw"], reverse=True)
    buys = [m for m in qty_moves if m["dq"] > 0][:top]
    sells = [m for m in qty_moves if m["dq"] < 0][:top]

    return {
        "count_today": len(today),
        "count_prev": len(prev),
        "added": added[:top],
        "removed": removed[:top],
        "buys": buys,
        "sells": sells,
        "n_buys": sum(1 for m in qty_moves if m["dq"] > 0),
        "n_sells": sum(1 for m in qty_moves if m["dq"] < 0),
        "weight_up": [m for m in weight_moves if m["dw"] > 0][:top],
        "weight_down": ([m for m in weight_moves if m["dw"] < 0][-top:][::-1]
                        if weight_moves else []),
        "has_qty": has_qty,
        "basket_shift": basket_shift,
    }


# 수급 요약에서 앞세울 3대 주체 — 나머지(연기금·투신·사모 등)는 '그외'로 접는다
MAIN_INVESTORS = ("개인", "외국인", "기관합계")


def kr_flow_summary(market: str = "KOSPI") -> dict:
    """개인·외국인·기관 순매수와 **전일대비 증감**.

    반환: {"date":..., "prev_date":..., "main":[{investor, net, delta}],
           "others":[{investor, net, delta}]}  (단위 억원)

    delta = 당일 순매수 − 직전 영업일 순매수. 부호가 아니라 '흐름이 어느 쪽으로
    바뀌었나'를 보여주는 값이다 — 예컨대 외국인이 이틀 내리 순매도여도 매도 규모가
    줄었다면 delta는 +가 된다.
    """
    days = last_biz_days(2)
    if not days:
        return {}
    today = kr_investor_flows(market, day=days[0])
    if not today:
        return {}
    prev = kr_investor_flows(market, day=days[1]) if len(days) > 1 else []
    pmap = {r["investor"]: r["net"] for r in prev}

    rows = [{"investor": r["investor"], "net": r["net"],
             "delta": round(r["net"] - pmap[r["investor"]], 1)
             if r["investor"] in pmap else None}
            for r in today]
    main = [r for r in rows if r["investor"] in MAIN_INVESTORS]
    main.sort(key=lambda r: MAIN_INVESTORS.index(r["investor"]))
    others = [r for r in rows if r["investor"] not in MAIN_INVESTORS]
    others.sort(key=lambda r: r["net"], reverse=True)
    log.info("수급 요약 %s (전일 %s): 주요 %d주체", days[0],
             days[1] if len(days) > 1 else "-", len(main))
    return {"date": days[0], "prev_date": days[1] if len(days) > 1 else "",
            "main": main, "others": others}


def fetch_flows(cfg: dict | None = None) -> dict:
    """6번 섹션용 수급 데이터 묶음 (전부 best-effort — 실패해도 빈 dict)."""
    cfg = cfg or {}
    if not cfg.get("enabled", True):
        return {}
    n = int(cfg.get("top_n", 10))
    investor = cfg.get("investor", "외국인")
    summary = kr_flow_summary()
    day = summary.get("date") or None
    return {
        "summary": summary,
        "investors": kr_investor_flows(day=day),
        "top": kr_top_net_purchases(investor=investor, n=n, day=day),
        "etf": kr_etf_top_movers(n=n, day=day),
    }


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
    """언급 종목의 관련 기사 — **실제 기사 URL만** 반환한다.

    우선순위: ① 네이버 뉴스(한국어 기사, 국내·해외 종목 모두 커버)
             ② yfinance 뉴스(미국 종목 영문 기사)
    둘 다 실패하면 빈 리스트 — `search_links()`(검색 결과 페이지 URL)는 더 이상
    자동으로 덧붙이지 않는다. 리포트에 "기사"라며 검색 페이지 링크가 박히는
    문제(2026-07-27 실측)의 원인이라 여기서 끊는다. `search_links()` 함수 자체는
    호출부가 명시적으로 최후수단으로 쓸 수 있게 남겨둔다.
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

    return items[:limit]
