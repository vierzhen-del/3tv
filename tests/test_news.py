"""네이버 검색 API 뉴스 수집 테스트 (두 인증 방식·HTML 정리·중복 제거)."""
from __future__ import annotations

import pytest

from threetv import news


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setattr(news, "credentials", lambda: ("cid", "secret"))
    monkeypatch.setattr(news, "_working", None)


class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


ITEM = {
    "title": "<b>마이크론</b>, 투자 확대 &quot;메모리 슈퍼사이클&quot;",
    "description": "마이크론이 <b>$2,500억</b> 투자를 발표했다 &amp; 메모리 업황이 개선됐다",
    "originallink": "https://news.example.com/mu-1",
    "link": "https://n.news.naver.com/mu-1",
    "pubDate": "Fri, 24 Jul 2026 21:00:00 +0900",
}


def test_strips_html_tags_and_entities(monkeypatch):
    monkeypatch.setattr(news, "requests", None, raising=False)
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, {"items": [ITEM]}))
    got = news.naver_news("마이크론")
    assert got[0]["title"] == '마이크론, 투자 확대 "메모리 슈퍼사이클"'
    assert "<b>" not in got[0]["summary"] and "&amp;" not in got[0]["summary"]
    assert got[0]["url"] == "https://news.example.com/mu-1"   # originallink 우선


def test_falls_back_to_second_auth_style(monkeypatch):
    """API HUB 키가 아니면 401 → 개발자센터 방식으로 재시도해야 한다."""
    calls: list[str] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        if "apigw.ntruss.com" in url:
            return FakeResp(401, text="unauthorized")
        return FakeResp(200, {"items": [ITEM]})

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    got = news.naver_news("마이크론")
    assert len(calls) == 2 and "openapi.naver.com" in calls[1]
    assert len(got) == 1


def test_remembers_working_endpoint(monkeypatch):
    """한 번 성공한 방식은 기억해 재시도를 줄인다."""
    calls: list[str] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        if "apigw.ntruss.com" in url:
            return FakeResp(403)
        return FakeResp(200, {"items": [ITEM]})

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    news.naver_news("첫 호출")
    n_first = len(calls)
    news.naver_news("두 번째 호출")
    assert len(calls) == n_first + 1      # 두 번째는 1회만


def test_disabled_without_credentials(monkeypatch):
    monkeypatch.setattr(news, "credentials", lambda: ("", ""))
    assert news.enabled() is False
    assert news.naver_news("아무거나") == []


def test_skips_items_without_title_or_url(monkeypatch):
    payload = {"items": [
        {"title": "제목만", "description": "본문"},          # url 없음
        {"title": "", "link": "https://a/b"},                # 제목 없음
        ITEM,
    ]}
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, payload))
    assert len(news.naver_news("q")) == 1


def test_dedupe_by_url_and_title():
    items = [
        {"title": "마이크론 투자 확대", "url": "https://a/1"},
        {"title": "마이크론 투자 확대", "url": "https://b/2"},   # 제목 중복(다른 매체)
        {"title": "엔비디아 신고가", "url": "https://a/1"},      # url 중복
        {"title": "엔비디아 신고가", "url": "https://c/3"},
    ]
    got = news.dedupe(items)
    assert [g["title"] for g in got] == ["마이크론 투자 확대", "엔비디아 신고가"]


def test_dedupe_ignores_punctuation_differences():
    items = [
        {"title": "마이크론, 투자 확대!", "url": "https://a/1"},
        {"title": "마이크론 투자 확대", "url": "https://b/2"},
    ]
    assert len(news.dedupe(items)) == 1


def test_collect_briefing_tags_query_and_dedupes(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, {"items": [ITEM]}))
    got = news.collect_briefing(["마이크론", "엔비디아"], per_query=2)
    assert len(got) == 1                       # 같은 기사 → 중복 제거
    assert got[0]["query"] == "마이크론"        # 어느 검색에서 나왔는지 기록


def test_collect_briefing_empty_without_credentials(monkeypatch):
    monkeypatch.setattr(news, "credentials", lambda: ("", ""))
    assert news.collect_briefing(["마이크론"]) == []


def test_network_error_returns_empty(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("timeout")))
    assert news.naver_news("q") == []
