"""카카오톡 '나에게 보내기' (카톡 메모) 전송.

카카오 개인 개발자 API 제약상 친구 대상 발송은 불가하고 본인 카톡(메모)만 가능.
- access token은 refresh token으로 매 실행 시 갱신
- 카카오가 새 refresh token을 주면 GitHub repo secret을 자동 업데이트 (GH_PAT 필요)
- 전체가 best-effort: 실패해도 텔레그램 전송에는 영향 없음
"""
from __future__ import annotations

import base64
import json

import requests

from .common import env, log

TOKEN_URL = "https://kauth.kakao.com/oauth/token"
MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"


def _refresh_access_token() -> str | None:
    rest_key = env("KAKAO_REST_API_KEY")
    refresh = env("KAKAO_REFRESH_TOKEN")
    if not rest_key or not refresh:
        log.info("KAKAO_REST_API_KEY/KAKAO_REFRESH_TOKEN 미설정 — 카카오 전송 생략")
        return None

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": rest_key,
            "refresh_token": refresh,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        log.error("카카오 토큰 갱신 실패 %d: %s", resp.status_code, resp.text[:300])
        return None
    data = resp.json()

    # 카카오는 refresh token 만료 1개월 전부터 새 refresh token을 내려줌
    new_refresh = data.get("refresh_token")
    if new_refresh and new_refresh != refresh:
        _update_github_secret("KAKAO_REFRESH_TOKEN", new_refresh)
    return data.get("access_token")


def _update_github_secret(name: str, value: str) -> None:
    """GitHub Actions repo secret 자동 갱신 (GH_PAT + GITHUB_REPOSITORY 필요)."""
    pat = env("GH_PAT")
    repo = env("GITHUB_REPOSITORY")  # Actions가 자동 주입 (owner/repo)
    if not pat or not repo:
        log.warning("새 카카오 refresh token 발급됨 — GH_PAT 미설정으로 자동 갱신 불가. "
                    "수동으로 KAKAO_REFRESH_TOKEN secret을 갱신하세요.")
        return
    try:
        from nacl import encoding, public

        headers = {
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
        }
        key_resp = requests.get(
            f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
            headers=headers, timeout=30,
        )
        key_resp.raise_for_status()
        key_data = key_resp.json()
        pub = public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
        encrypted = base64.b64encode(
            public.SealedBox(pub).encrypt(value.encode())
        ).decode()
        put_resp = requests.put(
            f"https://api.github.com/repos/{repo}/actions/secrets/{name}",
            headers=headers,
            json={"encrypted_value": encrypted, "key_id": key_data["key_id"]},
            timeout=30,
        )
        put_resp.raise_for_status()
        log.info("GitHub secret %s 자동 갱신 완료", name)
    except Exception as e:
        log.warning("GitHub secret 자동 갱신 실패(%s) — 수동 갱신 필요", e)


def send_kakao_memo(text: str) -> bool:
    """나에게 보내기. 텍스트 템플릿은 200자 제한이 있어 앞부분 요약만 전송."""
    try:
        access = _refresh_access_token()
        if not access:
            return False

        template = {
            "object_type": "text",
            # 카카오 텍스트 템플릿 제한(200자) — 헤드라인만 싣고 상세는 텔레그램/옵시디안
            "text": text[:190] + ("…" if len(text) > 190 else ""),
            "link": {"web_url": "https://www.youtube.com/@3protv"},
        }
        resp = requests.post(
            MEMO_URL,
            headers={"Authorization": f"Bearer {access}"},
            data={"template_object": json.dumps(template, ensure_ascii=False)},
            timeout=30,
        )
        if resp.status_code != 200:
            log.error("카카오 메모 전송 실패 %d: %s", resp.status_code, resp.text[:300])
            return False
        log.info("카카오 나에게 보내기 완료")
        return True
    except Exception as e:
        log.error("카카오 전송 오류(무시하고 계속): %s", e)
        return False
