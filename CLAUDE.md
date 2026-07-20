# 3tv — Claude Code 작업 규칙 (운영지식 축적, 2026-07-19 제정)

삼프로TV 아침 라이브를 자동 녹화·분석해 시황 리포트를 만드는 파이프라인.
아키텍처·셋업은 README.md, 여기는 **세션 간 인수인계용 운영지식**만 담는다.

## 브랜치 구조 (혼동 주의)

- **기본 브랜치 = `claude/youtube-market-analysis-vucjwq`** (main 아님). cron 스케줄은
  이 브랜치에서 돈다. 코드 수정이 실제 운영에 반영되려면 반드시 이 브랜치에 병합돼야 한다.
- main 병합은 불필요함이 확인됨(2026-07-18) — 기본 브랜치가 위 브랜치로 지정돼 있음.

## Secrets·자격증명 상태 (2026-07-19 실측)

- **GEMINI_API_KEY / ANTHROPIC_API_KEY / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID: 등록 완료.**
  (노션 7/18 미결점검의 "Secrets 전부 미등록"은 구정보 — 7/19 실행 로그로 등록 확인됨.)
- **ANTHROPIC_API_KEY는 크레딧 0, 2026-07-19 사용자 확정: 충전 없이 Gemini 전용 운영.**
  `config/settings.yaml`의 `models.claude_disabled: true`가 Claude 호출 자체를 생략하고
  바로 Gemini(`models.gemini` → 실패 시 `models.gemini_fallback`)로 리포트를 생성한다
  (`report.py`의 `_call_llm`). 매 실행마다 실패하는 Anthropic API 호출/로그를 없애기 위해
  단순 try/폴백이 아니라 명시적 플래그로 전환(7/19). 크레딧을 충전하면 이 플래그를
  false로 바꾸는 것만으로 코드 변경 없이 Claude가 다시 primary가 된다.
  14fiance의 `CAPTURE_CLAUDE_API_DISABLED`와 동일 계열의 조치다.
- **KRX_ID/KRX_PW 등록 완료(2026-07-19)** — data.krx.co.kr 회원 로그인 아이디·비밀번호를
  GitHub Secrets로 등록. pykrx의 `KRXSession`이 1시간 세션 쿠키로 인증 요청을 보내
  국내 시세 조회 신뢰도가 올라간다(미등록 시에도 익명 요청으로 폴백해 동작은 하지만
  GitHub Actions 공유 IP는 차단·제한 가능성이 더 높음). 실제 로그인 성공 여부는 다음
  실전 런(월요일 새벽) 또는 VOD 테스트 로그에서 `KRX 로그인 완료.` 문자열로 확인—
  API로 Secrets 값 자체는 조회 불가하므로 실행 로그가 유일한 검증 수단.
- **YOUTUBE_COOKIES: 사용자 등록 완료.**
- **KAKAO_REST_API_KEY/KAKAO_REFRESH_TOKEN 등록 완료(2026-07-19)** — `scripts/kakao_get_token.py`
  로컬 발급(Client Secret 미사용 설정) 후 Secrets 등록. `settings.yaml`의 `kakao.enabled: true`가
  이미 켜져 있어 코드 변경 없이 다음 실행부터 카카오 "나에게 보내기"로도 리포트 발송.
  refresh token 유효기간 약 2개월 — 만료 임박 시 파이프라인이 자동 갱신 시도(GH_PAT 필요),
  실패하면 텔레그램으로 재발급 안내가 온다.
- **n8n용 PAT 발급 및 워크플로 활성화 완료(2026-07-20)** — 아래 "볼트 연동" 절 참조.

## GitHub cron 지연 (실측 40~55분)

- 스케줄이 예정보다 40~55분 늦게 시작되는 것이 2026-07 운영 로그로 실측됨.
  7/12~7/16 스케줄 런 10회 전부가 이 지연(+당시 크레딧 문제)으로 실패했다.
- 대응: cron을 방송 55분 전(us 20:00 UTC=05:00 KST / kr 22:00 UTC=07:00 KST)으로
  이동(2026-07-18). capture.py가 방송 시작(05:55/07:55)까지 대기하므로 코드 변경 불필요.
  지연이 55분을 넘는 날은 여전히 실패 → 텔레그램 경고 후 VOD 재실행으로 복구.

## Gemini 무료 티어 예산 (flash: 20요청/일)

- 비전 분석: 세션당 최대 4요청(64프레임÷16장/배치), us+kr 하루 8요청.
- report.py Gemini 폴백 가동 시: 세션당 +2요청(종목 추출 1 + 리포트 1), 하루 +4요청.
- 합계 최대 12요청/일 — 한도 내. 같은 날 수동 테스트를 반복하면 한도에 걸릴 수 있고,
  429 시 flash-lite(별도 할당량 버킷)로 자동 전환된다. 상세는 vision.py 모듈 docstring.

## 볼트 연동 (Syncthing 구조 — git 볼트 아님)

- 리포트는 `3tv-reports`(private 중계 repo)에 push → S9의 n8n 스케줄(07:10/08:50 KST)이
  fetch → `RaeVault/3protv/YYYY/MM/*.md` → Syncthing이 S26에 전파.
- **push 경로는 검증 완료**(2026-07-19 05:51 KST "3protv us 리포트" 커밋 실측).
- **n8n 수신 워크플로 생성·PAT 입력·활성화 완료(2026-07-20)** — Tab S9에서 Claude Code CLI +
  n8n-mcp로 워크플로 생성, GitHub PAT는 n8n 웹UI에서 사용자가 직접 입력(보안상 자동화 제외),
  Active 전환 완료. 이 과정에서 n8n 서버 자체가 (Node 버전 비호환 + DB 테이블 소유권 불일치 +
  스키마 권한 미부여) 3중 장애로 죽어 있던 것도 함께 복구됨 — 상세:
  [노션 — n8n 재시작 실패 3종 해결](https://app.notion.com/p/3a35efd0e46281ab8353c57aa586bf6f),
  docs/n8n_s9_sync.md 참고.
- second-brain git repo + Obsidian Git 방식은 이중 동기화 충돌 위험으로 폐기됨(2026-07-18).
  n8n 통합 실패 시에만 폴백으로 사용.

## 작업 시 주의

- 스케줄 실패 문의가 오면: ① actions_list로 실제 실행·지연 여부 확인 → ② 실패 로그에서
  원인 구분(캡처 실패=지연/차단, 400=크레딧, 429=Gemini 할당량) → ③ 필요 시 VOD로 재실행.
- 같은 날 워크플로를 반복 수동 실행하면 Gemini 할당량을 소모한다 — 테스트는
  `--trim-start/--trim-duration`(구간 트리밍)이나 `--skip-notify --skip-archive`로.
- 종목 목록·프롬프트 수정 시 vision.py의 PROMPT와 report.py의 세션 목표가 짝으로
  동작하므로 한쪽만 고치지 말 것.
