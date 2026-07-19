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
- **ANTHROPIC_API_KEY는 크레딧 0** — 7/19 실행 3회가 리포트 생성 단계에서
  400(`credit balance too low`)으로 실패했다. 이 때문에 `report.py`에 **Gemini 자동
  폴백**이 있다(Claude 실패/키 미설정 → `models.gemini` → `models.gemini_fallback`).
  크레딧을 충전하면 코드 변경 없이 다음 실행부터 Claude가 다시 primary가 된다.
  14fiance의 "무과금 = Gemini 전용" 원칙과 같은 계열의 조치다.
- **KRX_ID/KRX_PW 미설정** — pykrx가 로그인 경고를 남기지만 market 단계는 실패를
  삼키고 quote=null로 진행하므로 치명적이지 않다(리포트에 "방송 화면 기준" 표기로 대체).
  국내 시세 검증 정확도를 올리려면 KRX 계정 secrets 등록.
- **미등록(사용자 작업 잔여)**: YOUTUBE_COOKIES(권장), KAKAO_*, n8n용 PAT.
  holdings.yaml은 SCHD 플레이스홀더 상태.

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
  n8n 수신 워크플로 import·활성화는 미확인 — docs/n8n_s9_sync.md 참고.
- second-brain git repo + Obsidian Git 방식은 이중 동기화 충돌 위험으로 폐기됨(2026-07-18).
  n8n 통합 실패 시에만 폴백으로 사용.

## 작업 시 주의

- 스케줄 실패 문의가 오면: ① actions_list로 실제 실행·지연 여부 확인 → ② 실패 로그에서
  원인 구분(캡처 실패=지연/차단, 400=크레딧, 429=Gemini 할당량) → ③ 필요 시 VOD로 재실행.
- 같은 날 워크플로를 반복 수동 실행하면 Gemini 할당량을 소모한다 — 테스트는
  `--trim-start/--trim-duration`(구간 트리밍)이나 `--skip-notify --skip-archive`로.
- 종목 목록·프롬프트 수정 시 vision.py의 PROMPT와 report.py의 세션 목표가 짝으로
  동작하므로 한쪽만 고치지 말 것.
