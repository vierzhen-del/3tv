---
name: github-3tv
description: 3tv의 브랜치 구조(기본 브랜치가 main이 아님), 워크플로 목록, Actions 진단·수동 실행 절차. 커밋·푸시 전이나 스케줄 실패 문의 시 사용.
---

# github-작업 — 3tv 브랜치·Actions

## 브랜치 (혼동 주의)

- **기본 브랜치 = `claude/youtube-market-analysis-vucjwq`** (main 아님). cron 스케줄이 이 브랜치에서 돈다.
  코드 수정이 실제 운영에 반영되려면 반드시 이 브랜치에 병합돼야 한다.
- main 병합은 불필요함이 확인됐다(2026-07-18) — 기본 브랜치가 위 브랜치로 지정돼 있다.

## 워크플로

| 파일 | 역할 |
|---|---|
| `.github/workflows/us-session.yml` | 미국장 세션 (cron 20:00 UTC = 05:00 KST) |
| `.github/workflows/kr-session.yml` | 국내장 세션 (cron 22:00 UTC = 07:00 KST) |
| `.github/workflows/notify.yml` | 알림 |

cron 시각은 방송 55분 전으로 앞당겨져 있다(실측 지연 대응) — 이유는 `운영점검` 스킬 참조.

## Actions 진단 순서

1. `mcp__github__actions_list` — 실제 실행·지연 여부
2. `mcp__github__get_job_logs` — 실패 원인 구분
3. `mcp__github__actions_run_trigger`(`run_workflow`) — VOD 재실행 등 수동 실행

같은 날 워크플로를 반복 수동 실행하면 **Gemini 할당량을 소모**한다 — `운영점검` 스킬의 예산표를 먼저 볼 것.

## 푸시·PR

- `git push -u origin <branch>`. 네트워크 실패 시에만 2s → 4s → 8s → 16s 지수 백오프로 최대 4회 재시도.
- PR은 사용자가 명시적으로 요청할 때만 만든다.
- API 키·쿠키·토큰은 어떤 형태로도 커밋하지 않는다(전부 GitHub Secrets에 있다).
