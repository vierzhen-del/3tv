# n8n 세션 스케줄러 — GitHub cron 대신 직접 발사

## 왜 필요한가 (2026-08-27 실측)

GitHub Actions의 `on: schedule`(cron) 트리거가 그날 반복적으로 지연·드롭됐다:
- `us-session` 자동 실행이 2시간38분 지연
- `night-slot` 마지막 슬롯이 2시간54분 지연
- `night-digest`·`noon-session`은 그날 실행 기록 자체가 아예 없음(드롭)

반면 **`workflow_dispatch`(API/수동 트리거)는 같은 날 전부 큐잉 즉시 정상 시작**했다 — 여러 번
재현 확인. 즉 문제는 GitHub의 "예약 실행 큐"에 있고, `workflow_dispatch` 경로 자체는 멀쩡하다.

이 워크플로는 `on: schedule` 대신 **S9의 n8n이 정해진 시각에 GitHub REST API로
`workflow_dispatch`를 직접 호출**해 지연·드롭을 구조적으로 우회한다. 각 세션 워크플로 YAML의
`on: schedule` 블록은 안전망으로 그대로 둔다.

**2026-08-27 후속**: 실제로 안전망 cron과 n8n이 겹쳐 돌아 us 리포트가 텔레그램에 2회
중복 발송된 사례가 있었다. 그래서 각 워크플로 맨 앞에 중복 실행 가드를 추가했다
(`github.event_name == 'schedule'`일 때만 동작, `GITHUB_TOKEN`으로 같은 워크플로의 오늘자
성공 실행을 조회해 있으면 무거운 단계를 전부 건너뜀 — night-slot은 날짜 대신 슬롯 시각
±40분 윈도우로 판단). n8n·수동 `workflow_dispatch`는 이 가드의 영향을 받지 않는다 —
복구·테스트 실행이 막히면 안 되기 때문. 상세는 각 워크플로 YAML의 "안전망 cron 중복 실행
방지" 스텝 참고.

## 커버 범위 (12개 트리거)

| 시각(KST) | 워크플로 | 비고 |
|---|---|---|
| 05:00 | us-session.yml | 월~금 |
| 07:00 | kr-session.yml | 월~금 |
| 11:05 | noon-session.yml | 월~금 |
| 22:00 | night-slot.yml (slot=22:00) | 월~금 |
| 23:00 | night-slot.yml (slot=23:00) | 월~금 |
| 00:00 | night-slot.yml (slot=00:00) | 화~토 (다음날) |
| 01:00 | night-slot.yml (slot=01:00) | 화~토 |
| 02:00 | night-slot.yml (slot=02:00) | 화~토 |
| 03:00 | night-slot.yml (slot=03:00) | 화~토 |
| 04:00 | night-slot.yml (slot=04:00) | 화~토 |
| 05:00 | night-slot.yml (slot=05:00) | 화~토 |
| 06:00 | night-digest.yml | 화~토 |

`night-slot`은 각 트리거에 `inputs.slot`을 명시적으로 넘긴다 — night-slot.yml의 "Determine
slot" 스텝이 `github.event.inputs.slot`이 있으면 그 값을 그대로 쓰고, 없을 때만 현재 시각으로
추정한다(워크플로 파일 주석 참고). 명시적으로 넘기면 n8n 트리거 자체의 지연과 무관하게 항상
올바른 슬롯으로 기록된다.

`etf-review`는 이미 GitHub cron(09:30 KST, 정각 아님)만으로 드롭 사례가 없었고 캡처·LLM을
쓰지 않아 예산 부담도 없어 이번엔 포함하지 않았다. 드롭이 관측되면 같은 패턴으로 추가한다.

## 사전 준비 — 새 GitHub PAT (Actions 쓰기 권한)

기존 `N8N_GITHUB_PAT_WRITE`(3tv-reports repo, Contents 쓰기 전용)와는 **별개**로, `3tv`
저장소에 `workflow_dispatch`를 걸 수 있는 새 Fine-grained PAT이 필요하다.

1. GitHub → 우측 상단 프로필 → Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token
2. Repository access: **Only select repositories** → `3tv` 하나만
3. Permissions → Repository permissions → **Actions: Read and write** (dispatch에 필요한
   최소 권한 — Contents 등 다른 권한은 켜지 않는다)
4. Generate token → 값을 한 번만 보여주니 즉시 복사

## n8n 설정

1. `docs/n8n_scheduler_workflow.json`을 n8n에 import (기존 다이제스트/동기화 워크플로와 같은
   방식 — Import from File)
2. 24개 노드(12쌍의 Schedule Trigger → HTTP Request "N시 발사") 중 HTTP Request 노드마다
   Authorization 헤더의 `<N8N_GITHUB_PAT_ACTIONS>` 를 위에서 발급한 토큰으로 교체
   (`Bearer github_pat_...` 형식 — `Bearer ` 뒤에 토큰만 붙이면 된다)
3. Settings → Timezone이 **Asia/Seoul**인지 확인(다이제스트 워크플로 설정 시 이미 확인한 항목
   — cron 표현식이 이 타임존 기준으로 평가된다. UTC로 되어 있으면 위 표의 KST 시각과
   전부 어긋난다)
4. 워크플로 활성화(Active 토글 ON)

## 확인 방법

- 발사 직후 GitHub → `3tv` → Actions 탭에서 해당 워크플로에 **Manual**(workflow_dispatch)
  실행이 새로 큐잉됐는지 확인 — `schedule`이 아니라 `workflow_dispatch` 이벤트여야 n8n이
  쏜 것이다
- HTTP Request 노드 응답이 204(No Content)면 정상 큐잉. 401/403이면 PAT 권한·값을,
  404면 `ref`(브랜치명)나 워크플로 파일명 오타를 의심
- n8n 실행 이력(Executions)에서 실패한 노드가 있으면 그 시각의 자동 실행이 안 나갔다는
  뜻이므로 수동으로 `workflow_dispatch`를 대신 걸어 복구

## 유지보수

- 세션 시각(예: us 05:00)이 바뀌면 이 JSON의 해당 Schedule Trigger cron과
  README "하루 흐름" 표, 각 워크플로 YAML의 `on: schedule` 블록(안전망) 세 곳을 함께 고친다
- night-slot 슬롯을 늘리거나 줄이면 이 워크플로의 트리거 개수도 맞춰 조정
