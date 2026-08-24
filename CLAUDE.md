# 3tv — 저장소 함정 메모

삼프로TV 아침 라이브를 자동 녹화·분석해 시황 리포트를 만드는 파이프라인.
아키텍처·셋업은 README.md.

## 모델별 동작 (2026-07-25, Anthropic Claude 5 컨텍스트 엔지니어링 가이드 반영)

이 문서는 **열어보기 전엔 모를 함정**만 담는다. 절차 상세는 `.claude/skills/`에 있고 필요할 때만 연다.

- **Opus 5 / Fable 5** (`claude-opus-5`, `claude-fable-5`): 아래를 금지령이 아니라 배경지식으로 읽고,
  주변 코드·맥락에 맞춰 스스로 판단한다. 스킬 문서는 그 작업을 실제로 할 때만 연다.
- **Sonnet 5 이하 · Haiku · 타사 모델(Gemini 등)**: 해당 스킬 문서를 **먼저 열어** 절차대로 수행하고,
  판단으로 단계를 건너뛰지 않는다.
- **모델과 무관하게 항상 유효**: 개인정보·보유자산 데이터 커밋 금지, 매매 자문 금지, 시크릿 노출 금지.
- 모델 세대가 올라가면 옛 약점을 막으려 세워둔 규칙부터 걷어낸다 (`/doctor`로 점검).

원본 규칙: second-brain 볼트 `14rae_work/00_지침/2026-07-25_모델별-운영규칙.md`
세션 운영지침 전문: 같은 볼트 `14rae_work/00_지침/2026-08-25_운영지침-v5.9.md` (v5.9 — 태그 트리거·
형식 강제·turn 기반 모델 전환 삭제, 버전 pin은 설치 전 실측)

## 스킬 (필요할 때만 열기)

| 스킬 | 언제 |
|---|---|
| `운영점검` | 스케줄 실패 · cron 지연 · Secrets 상태 · Gemini 할당량 · VOD 재실행 |
| `볼트연동` | 리포트가 옵시디안에 안 보임 · 동기화 경로 확인 |
| `github-3tv` | 브랜치 구조 · 커밋/푸시 · Actions 수동 실행 |

## 함정

**기본 브랜치가 main이 아니다** — `claude/youtube-market-analysis-vucjwq` 에서 cron이 돈다.
코드 수정이 운영에 반영되려면 이 브랜치에 병합돼야 한다.

**Claude API는 호출하지 않는다** — `ANTHROPIC_API_KEY` 는 등록돼 있으나 크레딧 0이고, 2026-07-19
사용자 확정으로 Gemini 전용 운영이다. `config/settings.yaml` 의 `models.claude_disabled: true` 가
Claude 호출 자체를 생략하고 바로 Gemini(`models.gemini` → 실패 시 `models.gemini_fallback`)로 리포트를
생성한다(`report.py` 의 `_call_llm`). 매 실행마다 실패하는 API 호출/로그를 없애려고 단순 try/폴백이
아니라 명시적 플래그로 전환했다. 크레딧을 충전하면 이 플래그만 false로 바꾸면 Claude가 다시 primary가
된다(14fiance의 `CAPTURE_CLAUDE_API_DISABLED` 와 같은 계열).

**실제 볼트 폴더명은 `vierzhen_home`** — "RaeVault"는 노션 문서의 별칭일 뿐이다. 예전 문서의
`RaeVault/...` 표기는 `/storage/emulated/0/Documents/vierzhen_home/3protv/` 로 치환해 읽는다.
상세는 `볼트연동` 스킬.

**프롬프트는 짝으로 수정한다** — `src/threetv/vision.py` 의 `PROMPT` 와 `report.py` 의 세션 목표가
짝으로 동작한다. 종목 목록·프롬프트를 고칠 때 한쪽만 고치지 말 것.

**같은 날 반복 실행은 Gemini 무료 할당량을 소모한다** — 테스트는 `--trim-start`/`--trim-duration`
(구간 트리밍) 또는 `--skip-notify --skip-archive` 로. 예산 계산은 `운영점검` 스킬.
