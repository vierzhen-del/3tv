"""텔레그램 그룹방 동시발송(TELEGRAM_GROUP_CHAT_ID) 테스트.

카카오와 달리 텔레그램은 봇을 다른 사람이 있는 그룹/채널에 초대하기만 하면
API로 그대로 보낼 수 있다 — 3protv 리포트를 개인 채팅과 그룹방에 동시에
보내달라는 요청(2026-08-10)으로 추가됨.
"""
from __future__ import annotations

import threetv.notify_telegram as tg


class _FakeResp:
    def __init__(self, status_code: int = 200, text: str = "ok"):
        self.status_code = status_code
        self.text = text


def _calls_recorder(status_by_chat: dict[str, int] | None = None):
    """chat_id별로 응답 상태를 다르게 줄 수 있는 requests.post 대체 함수."""
    calls: list[dict] = []

    def fake_post(url, json=None, data=None, timeout=None, **kw):
        payload = json or {}
        calls.append(payload)
        status = 200
        if status_by_chat:
            status = status_by_chat.get(payload.get("chat_id"), 200)
        return _FakeResp(status_code=status)

    return fake_post, calls


def test_group_chat_id_unset_sends_once_to_personal_chat(monkeypatch):
    """기존 동작 보존 — TELEGRAM_GROUP_CHAT_ID가 없으면 예전처럼 한 번만 보낸다."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.delenv("TELEGRAM_GROUP_CHAT_ID", raising=False)
    fake_post, calls = _calls_recorder()
    monkeypatch.setattr(tg.requests, "post", fake_post)

    ok = tg.send_telegram("본문")

    assert ok is True
    assert len(calls) == 1
    assert calls[0]["chat_id"] == "111"


def test_group_chat_id_set_sends_to_both(monkeypatch):
    """설정하면 개인 채팅 + 그룹방 양쪽에 같은 내용이 간다."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setenv("TELEGRAM_GROUP_CHAT_ID", "-987654321")  # 그룹은 음수 chat_id
    fake_post, calls = _calls_recorder()
    monkeypatch.setattr(tg.requests, "post", fake_post)

    ok = tg.send_telegram("본문")

    assert ok is True
    sent_chat_ids = {c["chat_id"] for c in calls}
    assert sent_chat_ids == {"111", "-987654321"}
    # 두 chat_id 모두 같은 본문을 받는다
    assert all(c["text"] == "본문" for c in calls)


def test_group_send_failure_is_best_effort(monkeypatch):
    """그룹방 발송이 실패해도 개인 채팅이 성공했으면 전체 반환값은 True.

    카카오처럼 정책상 원천 차단되는 게 아니라 일시적 오류(봇 미초대 등)일 수
    있어 개인 채팅 전송 자체를 실패로 취급하면 안 된다.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setenv("TELEGRAM_GROUP_CHAT_ID", "-987654321")
    fake_post, calls = _calls_recorder(status_by_chat={"-987654321": 403})
    monkeypatch.setattr(tg.requests, "post", fake_post)

    ok = tg.send_telegram("본문")

    assert ok is True  # 개인 채팅(111) 성공이 반환값을 결정
    assert len(calls) == 2


def test_personal_chat_failure_still_attempts_group(monkeypatch):
    """개인 채팅이 실패해도 그룹 발송은 별개로 시도된다(서로 독립)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setenv("TELEGRAM_GROUP_CHAT_ID", "-987654321")
    fake_post, calls = _calls_recorder(status_by_chat={"111": 500})
    monkeypatch.setattr(tg.requests, "post", fake_post)

    ok = tg.send_telegram("본문")

    assert ok is False  # 개인 채팅 실패는 그대로 반환값에 반영
    assert len(calls) == 2  # 그룹 발송은 그래도 시도됨


def test_missing_token_skips_group_send_entirely(monkeypatch):
    """토큰·개인 chat_id가 아예 없으면 그룹 설정과 무관하게 아무것도 안 보낸다."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_GROUP_CHAT_ID", "-987654321")
    fake_post, calls = _calls_recorder()
    monkeypatch.setattr(tg.requests, "post", fake_post)

    ok = tg.send_telegram("본문")

    assert ok is False
    assert calls == []
