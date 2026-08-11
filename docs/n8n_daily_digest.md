# S9 n8n → 오늘 종합 다이제스트 텔레그램 발송

`n8n_s9_sync.md`(볼트 저장)의 **다음 단계**입니다. 이미 볼트에 저장된 오늘 리포트를
읽어 Gemini로 3~5줄 종합 요약을 만들고 텔레그램으로 보냅니다.

```
RaeVault(=vierzhen_home)/3protv/YYYY/MM/*ymd*.md ─(읽기)─▶ Gemini 요약 ─▶ 텔레그램
```

- **전제조건**: `n8n_s9_sync.md` 워크플로가 정상 동작해 그날 리포트가 이미
  `/root/obsidian/3protv/YYYY/MM/`에 저장돼 있어야 합니다. 이 워크플로는 볼트를
  다시 GitHub에서 받아오지 않고 **로컬 파일을 직접 읽습니다** (S9 proot에서 n8n이
  이미 그 경로에 쓰고 있으므로).
- **발송 시각**: 평일 **18:00 KST** (사용자 확정, 2026-08-11). 이 시각이면 그날
  오늘/기사/정오/ETF/야간(아침에 발행되는 "오늘야간" 프리뷰) 리포트가 전부 나와
  있는 게 정상입니다.
- **다이제스트 재료**: `3protv기사_*.md`(원문 기사 30~50KB 덤프)는 **제외**하고,
  이미 한 번 요약된 오늘/정오/야간/ETF 리포트만 Gemini에 넘깁니다. 기사 원문까지
  넣으면 프롬프트가 과도하게 커지고, 개별 리포트 발행 시 이미 텔레그램으로 나간
  내용과 겹치는 요약을 또 만들게 됩니다.

## 사전 준비

1. **n8n 전용 Gemini API 키 credential 등록** (또는 기존 키 재사용 — 단, 아래
   할당량 항목 필독)
2. `n8n_s9_sync.md`에서 이미 등록한 텔레그램 봇 토큰/chat id를 재사용

### ⚠️ Gemini 할당량 — 반드시 flash-lite 버킷을 쓸 것

3tv 파이프라인은 `gemini-2.5-flash` 무료 티어(20요청/일)를 us+kr(최대 8) + noon +
night(8슬롯+digest) 로 이미 대부분 소진합니다(`config/settings.yaml` 주석 참고).
이 다이제스트를 같은 `gemini-2.5-flash` 키로 호출하면 그날 나머지 실행이 429로
실패할 위험이 있습니다.

그래서 이 워크플로는 night 세션과 같은 이유로 **`gemini-3.1-flash-lite`**
(별도 할당량 버킷)를 씁니다. 워크플로 JSON의 모델명을 함부로 `gemini-2.5-flash`로
바꾸지 마세요.

## 워크플로 등록 (import 방식)

1. n8n 웹 UI → Workflows → **Import from File** → `docs/n8n_daily_digest_workflow.json`
2. import 후 수정할 것 3곳:
   - **"Gemini 요약" HTTP Request 노드**: URL의 `<N8N_GEMINI_API_KEY>`를 발급받은 키로 교체
   - **"텔레그램 발송" HTTP Request 노드**: `<N8N_TELEGRAM_BOT_TOKEN>` / `<N8N_TELEGRAM_CHAT_ID>`를
     `n8n_s9_sync.md`와 동일한 값으로 교체
3. 워크플로 Settings → **Timezone: Asia/Seoul** 확인
4. 활성화(Active 토글)

## 노드 구조

| # | 노드 | 설정 |
|---|------|------|
| 1 | Schedule Trigger | Cron: `0 18 * * 1-5` (KST, 평일 18:00) |
| 2 | Code (오늘 경로 계산) | yyyy/mm/dd/ymd/dateLabel 계산 |
| 3 | Read/Write Files from Disk (Read) | fileSelector: `/root/obsidian/3protv/{yyyy}/{mm}/*{ymd}*.md` (glob) |
| 4 | Code (다이제스트용 본문 구성) | `3protv기사_*` 제외, 리포트당 4000자 상한, 프롬프트 조립. 0건이면 `{skip:true}` |
| 5 | IF (리포트 없음?) | `{{ $json.skip }}` true → 종료, false → Gemini 호출 |
| 6 | HTTP Request (Gemini 요약) | POST `.../models/gemini-3.1-flash-lite:generateContent?key=<KEY>`, **Never Error** 켜기 |
| 7 | Code (요약 파싱) | 응답에서 텍스트 추출. 실패 시 "요약 생성 실패, 리포트는 정상 저장됨" 경고문으로 대체(침묵 실패 금지) |
| 8 | HTTP Request (텔레그램 발송) | POST `https://api.telegram.org/bot<TOKEN>/sendMessage` |

## 실패 처리 원칙

`n8n_s9_sync.md`의 2026-07-27 GH_PAT 침묵 실패 사고와 같은 원칙을 적용합니다 —
**Gemini 호출이 실패해도 아무 메시지도 안 가는 상태를 만들지 않습니다.** 429(할당량
소진) 등으로 요약 생성이 실패하면 "요약 생성 실패, 개별 리포트는 정상 저장됨"이라는
경고 텔레그램을 대신 보냅니다(7번 노드). 리포트 자체(볼트 저장)는 이 워크플로와
무관하게 이미 끝난 상태이므로, 다이제스트 실패가 리포트 유실을 의미하지 않는다는
점을 메시지에 명시합니다.

## 확인 방법

1. n8n에서 워크플로 **Execute Workflow**로 수동 1회 실행
2. 텔레그램에 "📋 3프로TV 오늘의 종합 다이제스트 (YYYY-MM-DD)" 메시지 도착 확인
3. 그날 볼트에 리포트가 하나도 없는 상태(주말/휴장일 등)로 실행 시 아무 메시지도
   안 가고 조용히 종료되는지 확인 (5번 IF 노드 분기)
4. 강제로 잘못된 API 키를 넣고 실행 → "요약 생성 실패" 경고가 오는지 확인 (침묵 실패
   방지 확인)

## 알려진 제약

- 리포트 4종(오늘/정오/야간/ETF)만 재료로 쓰고 기사 원문은 제외합니다. 기사까지
  반영한 다이제스트가 필요해지면 4번 Code 노드의 `startsWith('3protv기사')` 필터를
  없애되, 4000자 상한은 그대로 두거나 더 낮춰야 프롬프트 크기가 안전합니다.
- 18:00 발송이라 그날 22:00 이후 시작하는 야간 라이브 자체의 그날 밤 결과는
  다이제스트에 포함되지 않습니다(다음날 아침 "오늘야간" 프리뷰로만 반영). 그날 밤
  실황까지 원하면 발송 시각을 다음날 아침으로 옮겨야 합니다(사용자가 18:00을
  확정해 현재는 이 범위 밖).
