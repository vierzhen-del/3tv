# S9 n8n → RaeVault 리포트 동기화 가이드

3tv 리포트를 옵시디안 볼트로 전달하는 마지막 구간입니다.

```
GitHub Actions ─push─▶ 3tv-reports repo ─(n8n fetch)─▶ RaeVault/3protv/ ─(Syncthing)─▶ S26
```

- 볼트(`RaeVault`)는 Syncthing(S9 마스터 ⇄ S26)으로 동기화되므로 GitHub와 직접 연결하지 않습니다
- S9 proot에서 이미 상시 구동 중인 **n8n**에 스케줄 워크플로 하나만 추가합니다
- 파일 watch(inotify)는 proot+Syncthing 환경에서 신뢰할 수 없으므로(2nd Brain 검토 결론) **스케줄 fetch** 방식을 사용합니다

## 사전 준비

1. **n8n 전용 GitHub PAT 발급** (Actions의 GH_PAT와 반드시 별개로!)
   - GitHub → Settings → Developer settings → Fine-grained tokens → Generate new token
   - Repository access: **Only select repositories → `3tv-reports` 하나만**
   - Permissions: **Contents: Read-only**만 (그 외 전부 No access)
   - 이 토큰은 읽기 전용 + repo 1개라 S9에 저장돼도 피해 범위가 최소입니다
2. proot 진입 시 볼트 bind 확인: `proot-distro login ubuntu --bind /storage/emulated/0/Documents/vierzhen_home:/root/obsidian`
   (n8n에서 `/root/obsidian/3protv/`에 쓸 수 있어야 함)

## 워크플로 등록 (import 방식)

1. n8n 웹 UI → Workflows → **Import from File** → `docs/n8n_3tv_sync_workflow.json` 선택
2. import 후 수정할 것 4곳:
   - **"오늘 경로 계산" Code 노드**: `OWNER` 상수를 본인 계정(`vierzhen-del`)으로
   - **"파일 목록 조회" HTTP Request 노드**: Header `Authorization` 값의 `<N8N_GITHUB_PAT>`를 위에서 발급한 토큰으로 교체 (또는 n8n Credential로 등록해 연결)
   - **"md 다운로드" HTTP Request 노드**: 같은 `<N8N_GITHUB_PAT>` 교체
   - **"텔레그램 경고" HTTP Request 노드**: URL의 `<N8N_TELEGRAM_BOT_TOKEN>` 과 JSON body의 `<N8N_TELEGRAM_CHAT_ID>` 를 3tv가 쓰는 봇 토큰·chat id로 교체
3. 워크플로 Settings → **Timezone: Asia/Seoul** 확인
4. 활성화(Active 토글)

동작: 평일 **07:10**(us 리포트) · **08:50**(kr 병합본) · **09:40**(재시도) · **12:30**(정오 세션) KST에 실행 → `3tv-reports`의 `3protv/YYYY/MM/`에서 오늘 날짜 md를 찾아 `/root/obsidian/3protv/YYYY/MM/`에 저장 → Syncthing이 1분 내 S26으로 전파.

### 2026-09-03 추가: 정오 세션 동기화 공백 (사용자 지적으로 발견)

기존 3슬롯(07:10/08:50/09:40)은 전부 오전 중이라 **정오(noon) 세션 리포트(11:05~12:20 KST 완료)를 커버하는 슬롯이 아예 없었다** — 정오 리포트는 다음날 07:10 슬롯이 돌기 전까지 로컬 볼트에 절대 안 들어오는 구조적 공백이었다(그날 안에 옵시디안에 보이려면 수동 백필이 필요했다). `_index.md`가 GitHub 쪽엔 정상 갱신돼 있는데 로컬(S9→S26)에 며칠치가 안 보인다는 신고를 조사하다 발견 — 실제로는 버그가 아니라 "다음 예정 슬롯을 기다리는 정상 지연"이었지만, 정오분만은 기다려도 그날 안엔 절대 안 온다는 게 문제였다. **12:30 KST 슬롯을 추가**해 정오 세션 완료 후 여유를 두고 그날 안에 동기화되게 했다.

> ⚠️ **이미 예전 버전을 import 해뒀다면 다시 import 해야 합니다.** n8n은 JSON 파일과 연결돼 있지 않고 import 시점의 사본을 들고 있습니다. 기존 워크플로를 열어 전체 선택 후 삭제하고 붙여넣거나, 새로 import한 뒤 옛 워크플로를 비활성화하세요 (둘 다 Active면 같은 파일을 두 번 씁니다).

### 09:40 재시도 슬롯이 있는 이유

GitHub cron 실측 지연이 40~55분입니다. 2026-07-26 kr 세션은 08:04 KST에 끝나 08:50 fetch까지 마진이 46분뿐이었습니다. 지연이 조금만 더 커지면 그날 병합본을 놓치는데, 다음날 실행은 "오늘 날짜"만 보므로 **영구 누락**이 됩니다. 09:40 슬롯이 이 구멍을 막습니다. 이미 받은 파일은 같은 내용으로 덮어쓰므로 중복 실행은 무해합니다.

### 누락 경고

`3protv/YYYY/MM/`에 오늘 날짜 md가 하나도 없으면 **"누락?" IF 노드**가 텔레그램 경고를 보냅니다.

- **09:40 슬롯에서만** 보냅니다 — 07:10 시점엔 kr 병합본이 아직 없는 게 정상이라, 그때 경고하면 매일 오탐이 납니다. 판단은 "오늘 경로 계산" 노드가 내려주는 `hour` 값으로 합니다.
- `3protv/2026/07` 폴더 자체가 없으면 GitHub API가 **404**를 주는데, "파일 목록 조회" 노드의 `neverError` 옵션이 이를 정상 응답으로 받아 넘겨 흐름이 끊기지 않게 합니다. (2026-07-27 사고 당시엔 이 404에서 워크플로가 조용히 멈췄습니다.)

### `_index.md`는 항상 별도로 받아온다 (2026-08-13~14 발견, 2026-08-21 이 브랜치에 반영)

"오늘 파일 필터" 노드는 파일명에 오늘 날짜가 포함된 것만 걸러 `today` 배열을 만드는데, `_index.md`는 파일명에 날짜가 없어서 **이 필터에 절대 안 걸립니다** — 그대로 두면 `_rebuild_month_index()`가 GitHub `3tv-reports`에 아무리 최신 인덱스를 올려도 로컬 볼트엔 영원히 반영되지 않습니다. 그래서 `missing` 판정(위 항목)과는 별개로, 그날 목록에 `_index.md`가 있으면 **매 실행마다 무조건 추가로** 다운로드 대상에 넣습니다(날짜 매칭 여부와 무관).

⚠️ **다이제스트 로컬 인덱스 패치와의 상호작용** — `docs/n8n_daily_digest_workflow.json`(18:00 KST)은 로컬 `_index.md`에 다이제스트 링크를 직접 추가하는 별도 패치를 가지고 있습니다(`docs/n8n_daily_digest.md`의 "로컬 인덱스는 GitHub 쪽과 절대 동기화되지 않는다" 참고). 이 동기화 워크플로가 위 수정으로 매 실행 `_index.md`를 GitHub 버전으로 **통째로 덮어쓰게** 되면서, 다음날 07:10 슬롯에 그 다이제스트 링크가 사라집니다(다이제스트 자체가 `3tv-reports`엔 안 올라가 있으므로 GitHub 쪽 "정식본"엔 애초에 그 링크가 없음). 즉 다이제스트 인덱스 링크는 **그날 18:00~다음날 07:10까지만** 보이는 반짝임 현상이 있습니다 — 영구적으로 남기려면 다이제스트 파일 자체를 `3tv-reports`에 올리는 별도 작업(쓰기 권한 PAT 필요)이 있어야 합니다. 아직 미결.

## 수동 구성 시 노드 구조 (import가 안 될 때)

| # | 노드 | 설정 |
|---|------|------|
| 1 | Schedule Trigger | Cron: `10 7 * * 1-5`, `50 8 * * 1-5`, `40 9 * * 1-5`, `30 12 * * 1-5` (KST) |
| 2 | Code (오늘 경로 계산) | 아래 스니펫 |
| 3 | HTTP Request (파일 목록) | GET `https://api.github.com/repos/{OWNER}/3tv-reports/contents/3protv/{yyyy}/{mm}?ref=main`, Header: `Authorization: Bearer <PAT>`, `Accept: application/vnd.github+json`, Options → Response → **Never Error** 켜기 |
| 4 | Code (오늘 파일 필터) | 응답 배열에서 `name`에 오늘 `YYYYMMDD` 포함 항목만, `download_url` 추출. 0건이고 `hour >= 9` 면 `{missing:true, text}` 1건 반환 |
| 5 | IF (누락?) | 조건: `{{ $json.missing }}` is true → 출력 0(참)=경고, 출력 1(거짓)=다운로드 |
| 6 | HTTP Request (텔레그램 경고) | POST `https://api.telegram.org/bot<TOKEN>/sendMessage`, JSON body `{chat_id, text}` |
| 7 | HTTP Request (다운로드) | GET `{{download_url}}`, Header 동일, Response: **File** |
| 8 | Read/Write Files from Disk | Operation: Write, File Path: `/root/obsidian/3protv/{yyyy}/{mm}/{name}` |

2번 Code 노드 스니펫:
```javascript
const now = new Date(new Date().toLocaleString("en-US", {timeZone: "Asia/Seoul"}));
const yyyy = String(now.getFullYear());
const mm = String(now.getMonth() + 1).padStart(2, "0");
const ymd = yyyy + mm + String(now.getDate()).padStart(2, "0");
const hour = now.getHours();   // 09:40 슬롯에서만 누락 경고를 보내기 위한 값
return [{ json: { yyyy, mm, ymd, hour } }];
```

## write 충돌 규칙 (Syncthing)

- 이 워크플로는 볼트 내 **`3protv/` 폴더에만** 씁니다 (기존 n8n `00_Inbox/` 전용 규칙에 3protv/ 추가)
- 매일 새 파일 생성 + kr 실행 시 같은 날짜 파일 덮어쓰기(전체 교체)뿐이라, S26에서 이 폴더를 동시에 편집하지만 않으면 `.sync-conflict`가 생기지 않습니다
- 리포트에 개인 메모를 달고 싶으면 파일을 직접 수정하지 말고 별도 노트에서 링크하는 방식을 권장

## 확인 방법

1. n8n에서 워크플로 **Execute Workflow**로 수동 1회 실행 → `/root/obsidian/3protv/` 아래 md 생성 확인
2. S9 옵시디안(또는 파일탐색기)에서 `/storage/emulated/0/Documents/vierzhen_home/3protv/` 확인
3. **파일이 2개 내려왔는지** — `3protv오늘_YYYYMMDD_*.md`(시황)와 `3protv기사_YYYYMMDD.md`(종목 기사).
   시황 노트가 기사 노트를 `[[위키링크]]`로 참조하므로 **둘이 같이** 있어야 링크가 깨지지 않습니다
   (2026-07-27 리포트 2분할로 생긴 신규 구조)
4. 1분 내 S26 옵시디안에 같은 파일 나타나는지 확인

수동 실행인데 아무 파일도 안 내려온다면 **볼트가 아니라 그 앞 구간**이 문제입니다 — `3tv-reports` 저장소에 오늘 날짜 md가 실제로 올라왔는지부터 확인하세요. 없다면 3tv Actions의 `vault-check` 워크플로를 수동 실행해 `GH_PAT` 부터 점검합니다.
