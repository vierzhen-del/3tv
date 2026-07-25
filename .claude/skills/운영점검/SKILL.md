---
name: 운영점검
description: 3tv 파이프라인 실패 진단 — GitHub cron 지연, Secrets 상태, Gemini 무료 티어 예산, VOD 재실행 절차. "리포트가 안 왔어요", "스케줄이 실패했어요" 문의나 테스트 실행 전에 사용.
---

# 운영점검 — 실패 진단 · 예산 관리

## 진단 순서

1. `mcp__github__actions_list` 로 실제 실행 여부·지연을 확인한다.
2. 실패 로그에서 원인을 구분한다.
   - **캡처 실패** → cron 지연 또는 YouTube 차단
   - **400** → Anthropic 크레딧 (현재는 `claude_disabled: true` 라 발생하지 않아야 정상)
   - **429** → Gemini 할당량
3. 필요하면 VOD로 재실행한다.

## GitHub cron 지연 (실측 40~55분)

스케줄이 예정보다 40~55분 늦게 시작되는 것이 2026-07 운영 로그로 실측됐다. 7/12~7/16 스케줄 런
10회 전부가 이 지연(+당시 크레딧 문제)으로 실패했다.

**대응**: cron을 방송 55분 전(us 20:00 UTC = 05:00 KST / kr 22:00 UTC = 07:00 KST)으로 이동(2026-07-18).
`capture.py` 가 방송 시작(05:55 / 07:55)까지 대기하므로 코드 변경은 불필요하다.
지연이 55분을 넘는 날은 여전히 실패 → 텔레그램 경고 후 VOD 재실행으로 복구.

## Secrets 상태 (2026-07-19~20 실측)

| Secret | 상태 |
|---|---|
| `GEMINI_API_KEY` | 등록 완료 |
| `ANTHROPIC_API_KEY` | 등록됐으나 **크레딧 0** — 사용자 확정: 충전 없이 Gemini 전용 운영 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 등록 완료 |
| `KRX_ID` / `KRX_PW` | 등록 완료(2026-07-19) |
| `YOUTUBE_COOKIES` | 등록 완료 |
| `KAKAO_REST_API_KEY` / `KAKAO_REFRESH_TOKEN` | 등록 완료(2026-07-19) |
| n8n용 PAT | 발급·워크플로 활성화 완료(2026-07-20) |

- 노션 7/18 미결점검의 "Secrets 전부 미등록"은 **구정보** — 7/19 실행 로그로 등록이 확인됐다.
- **KRX 로그인 검증**: pykrx의 `KRXSession` 이 1시간 세션 쿠키로 인증 요청을 보낸다. 미등록이어도
  익명 요청으로 폴백해 동작하지만 GitHub Actions 공유 IP는 차단 가능성이 더 높다. API로 Secrets 값
  자체는 조회 불가하므로 **실행 로그의 `KRX 로그인 완료.` 문자열이 유일한 검증 수단**이다.
- **카카오**: `settings.yaml` 의 `kakao.enabled: true` 라 코드 변경 없이 "나에게 보내기"로도 리포트가
  나간다. refresh token 유효기간 약 2개월 — 만료 임박 시 파이프라인이 자동 갱신을 시도하고(GH_PAT 필요),
  실패하면 텔레그램으로 재발급 안내가 온다. 로컬 발급은 `scripts/kakao_get_token.py`(Client Secret 미사용).

## Gemini 무료 티어 예산 (flash: 20요청/일)

| 용도 | 요청 수 |
|---|---|
| 비전 분석 | 세션당 최대 4 (64프레임 ÷ 16장/배치) × us+kr = 하루 8 |
| report.py Gemini 리포트 | 세션당 +2 (종목 추출 1 + 리포트 1) × 2 = 하루 +4 |
| **합계** | 최대 12/일 — 한도 내 |

같은 날 수동 테스트를 반복하면 한도에 걸린다. 429 시 flash-lite(별도 할당량 버킷)로 자동 전환된다.
상세는 `vision.py` 모듈 docstring.

**테스트는 할당량을 아껴서**: `--trim-start` / `--trim-duration`(구간 트리밍) 또는
`--skip-notify --skip-archive` 를 쓴다.
