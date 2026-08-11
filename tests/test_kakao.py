"""카카오 나에게 보내기 클릭 링크 테스트.

2026-08-11 실측: 클릭 링크가 유튜브 채널 홈으로 하드코딩돼 있어, 메시지를
눌러도 그날 리포트 내용이 전혀 안 보였다. 저장위치 URL을 넘기면 그 링크가
쓰이고, 안 넘기면(저장 실패 등) 기존 채널 링크로 대체돼야 한다.
"""
from __future__ import annotations

import json

from threetv import notify_kakao as kakao


class _FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _patch_kakao(monkeypatch, sent):
    monkeypatch.setenv("KAKAO_REST_API_KEY", "k")
    monkeypatch.setenv("KAKAO_REFRESH_TOKEN", "r")

    def fake_post(url, data=None, headers=None, timeout=None):
        if "oauth/token" in url:
            return _FakeResp(200, {"access_token": "a"})
        sent["template"] = json.loads(data["template_object"])
        return _FakeResp(200)

    monkeypatch.setattr(kakao.requests, "post", fake_post)


def test_send_kakao_memo_uses_report_link_when_given(monkeypatch):
    sent: dict = {}
    _patch_kakao(monkeypatch, sent)
    report_url = "https://github.com/vierzhen-del/3tv-reports/blob/main/3protv/2026/08/x.md"
    assert kakao.send_kakao_memo("hello", report_url) is True
    assert sent["template"]["link"]["web_url"] == report_url


def test_send_kakao_memo_falls_back_to_channel_link_without_url(monkeypatch):
    sent: dict = {}
    _patch_kakao(monkeypatch, sent)
    assert kakao.send_kakao_memo("hello") is True
    assert sent["template"]["link"]["web_url"] == "https://www.youtube.com/@3protv"
