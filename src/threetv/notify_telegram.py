"""텔레그램 전송 (기존 3protv 알림봇 토큰 재사용)."""
from __future__ import annotations

import requests

from .common import env, log


def send_telegram(text: str, max_len: int = 4000) -> bool:
    token = env("TELEGRAM_BOT_TOKEN")
    # 기존 v28 .env 호환: TELEGRAM_CHAT_ID 없으면 TELEGRAM_NOTIFY_CHANNEL 사용
    chat_id = env("TELEGRAM_CHAT_ID") or env("TELEGRAM_NOTIFY_CHANNEL")
    if not token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 미설정 — 텔레그램 전송 생략")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for chunk in _split(text, max_len):
        resp = requests.post(
            url, json={"chat_id": chat_id, "text": chunk}, timeout=30
        )
        if resp.status_code != 200:
            log.error("텔레그램 전송 실패 %d: %s", resp.status_code, resp.text[:300])
            ok = False
    if ok:
        log.info("텔레그램 전송 완료")
    return ok


def send_alert(text: str) -> None:
    """파이프라인 실패 알림 등 짧은 경고 메시지."""
    try:
        send_telegram(text)
    except Exception as e:
        log.error("알림 전송 실패: %s", e)


def _split(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks
