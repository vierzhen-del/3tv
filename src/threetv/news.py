"""네이버 검색 API 뉴스 수집 (종목별 관련 기사 + 뉴스 브리핑용).

무료 한도 하루 25,000건이라 종목마다 조회해도 여유가 크다.

네이버는 인증 방식이 두 가지로 공존한다 — 사용자가 어느 쪽 키를 발급받았는지에
따라 헤더·엔드포인트가 다르므로 **둘 다 시도**하고 성공한 방식을 기억해 재사용한다:
  ① API HUB (신규, NCP)  : naverapihub.apigw.ntruss.com/search/v1/news
                           X-NCP-APIGW-API-KEY-ID / X-NCP-APIGW-API-KEY
  ② 개발자센터 (기존)     : openapi.naver.com/v1/search/news.json
                           X-Naver-Client-Id / X-Naver-Client-Secret
둘 다 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 시크릿 한 쌍으로 처리한다.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from .common import KST, env_token, log

# (엔드포인트, ID 헤더명, SECRET 헤더명)
_ENDPOINTS = [
    ("https://naverapihub.apigw.ntruss.com/search/v1/news",
     "X-NCP-APIGW-API-KEY-ID", "X-NCP-APIGW-API-KEY"),
    ("https://openapi.naver.com/v1/search/news.json",
     "X-Naver-Client-Id", "X-Naver-Client-Secret"),
]

# 처음 성공한 방식을 기억해 이후 호출에서 헛시도를 줄인다
_working: tuple | None = None

_TAG_RE = re.compile(r"<[^>]+>")


def credentials() -> tuple[str, str]:
    return env_token("NAVER_CLIENT_ID"), env_token("NAVER_CLIENT_SECRET")


def enabled() -> bool:
    cid, secret = credentials()
    return bool(cid and secret)


def _clean(s: str) -> str:
    """네이버 응답의 <b> 강조 태그와 HTML 엔티티(&quot; 등)를 제거."""
    return html.unescape(_TAG_RE.sub("", s or "")).strip()


def _published_at(pub_date: str) -> datetime | None:
    """네이버 pubDate(RFC 1123, 예: 'Mon, 27 Jul 2026 09:00:00 +0900')를 KST datetime으로.

    형식이 안 맞거나 비어 있으면 None — 호출 측이 '당일' 필터에서 버리지 않고
    뒤로 보내는 판단을 하게 한다(네이버가 종종 pubDate를 비운다).
    """
    if not pub_date:
        return None
    try:
        dt = parsedate_to_datetime(pub_date)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(KST)


def _normalize(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        title = _clean(it.get("title", ""))
        url = (it.get("originallink") or it.get("link") or "").strip()
        if not title or not url:
            continue
        pub_raw = (it.get("pubDate") or "").strip()
        pub_dt = _published_at(pub_raw)
        out.append({
            "title": title[:160],
            "summary": _clean(it.get("description", ""))[:300],
            "url": url,
            "published": pub_raw,
            "published_kst": pub_dt.strftime("%m/%d %H:%M") if pub_dt else "",
            "publisher": "네이버뉴스",
        })
    return out


def naver_news(query: str, display: int = 5, sort: str = "sim") -> list[dict]:
    """네이버 뉴스 검색. sort: 'sim'(정확도) | 'date'(최신).

    실패 시 빈 리스트 — 호출 측은 항상 검색 링크로 갈음할 수 있어야 한다.
    """
    global _working
    cid, secret = credentials()
    if not (cid and secret) or not query.strip():
        return []

    import requests

    params = {"query": query, "display": max(1, min(display, 20)), "sort": sort}
    candidates = [_working] if _working else _ENDPOINTS
    for url, id_header, secret_header in candidates:
        try:
            resp = requests.get(
                url, params=params,
                headers={id_header: cid, secret_header: secret},
                timeout=15,
            )
        except Exception as e:
            log.debug("네이버 뉴스 요청 실패 (%s): %s", url, e)
            continue
        if resp.status_code == 200:
            _working = (url, id_header, secret_header)
            try:
                return _normalize(resp.json().get("items") or [])
            except Exception as e:
                log.warning("네이버 뉴스 응답 파싱 실패: %s", e)
                return []
        if resp.status_code in (401, 403, 404):
            # 인증 방식이 안 맞는 것 — 다음 방식으로
            log.debug("네이버 뉴스 %d (%s) → 다른 인증 방식 시도", resp.status_code, url)
            _working = None
            continue
        log.warning("네이버 뉴스 조회 실패 %d: %s", resp.status_code, resp.text[:200])
        return []

    log.warning("네이버 뉴스: 두 인증 방식 모두 실패 — NAVER_CLIENT_ID/SECRET 확인 필요")
    return []


def dedupe(items: list[dict]) -> list[dict]:
    """URL과 제목이 겹치는 기사를 하나로 (같은 기사가 여러 종목에 걸리는 경우)."""
    seen_url: set[str] = set()
    seen_title: set[str] = set()
    out: list[dict] = []
    for it in items:
        url = it.get("url", "")
        # 제목의 공백·기호를 제거해 사실상 같은 제목을 잡는다
        key = re.sub(r"[^0-9A-Za-z가-힣]", "", it.get("title", ""))[:40]
        if url in seen_url or (key and key in seen_title):
            continue
        seen_url.add(url)
        if key:
            seen_title.add(key)
        out.append(it)
    return out


def collect_briefing(
    names: list[str],
    per_query: int = 4,
    limit: int = 20,
    recency_hours: int | None = None,
    max_queries: int | None = None,
) -> list[dict]:
    """브리핑용 기사 모음 — 종목명별로 검색해 합치고 중복 제거.

    각 항목에 어떤 종목 검색에서 나왔는지(`query`)를 남겨 LLM이 묶을 때 쓰게 한다.
    `recency_hours`를 주면 그 시간 안에 발행된 기사만 남기고 **최신순 정렬**한다
    (달력상 '당일'이 아니라 '직전 N시간' — 06시 방송 시점엔 전일 미국장 기사가
    핵심이라 자정 기준으로 자르면 정작 필요한 기사가 날아간다). 발행시각을
    못 읽은 기사는 버리지 않고 뒤로 보낸다(네이버가 종종 pubDate를 비운다).
    `max_queries`는 이름 목록이 커졌을 때 검색 호출 폭주를 막는 안전벨트.
    """
    if not enabled():
        return []
    if max_queries:
        names = names[:max_queries]
    collected: list[dict] = []
    for name in names:
        for it in naver_news(name, display=per_query, sort="date"):
            collected.append({**it, "query": name})
    result = dedupe(collected)

    # 정렬·필터용 발행시각은 여기서만 계산하고 item dict엔 남기지 않는다
    # (datetime은 JSON 직렬화가 안 돼 report.py의 json.dumps(verified_mentions)에서 깨진다)
    dated = [(it, _published_at(it.get("published", ""))) for it in result]

    if recency_hours is not None:
        cutoff = datetime.now(KST) - timedelta(hours=recency_hours)
        dated = [(it, dt) for it, dt in dated if dt is None or dt >= cutoff]

    dated.sort(key=lambda pair: pair[1] or datetime.min.replace(tzinfo=KST), reverse=True)
    result = [it for it, _ in dated][:limit]
    log.info("네이버 뉴스 브리핑 수집: %d건 (종목 %d개 검색)", len(result), len(names))
    return result
