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

동작: 평일 **07:10**(us 리포트) · **08:50**(kr 병합본) · **09:40**(재시도) KST에 실행 → `3tv-reports`의 `3protv/YYYY/MM/`에서 오늘 날짜 md를 찾아 `/root/obsidian/3protv/YYYY/MM/`에 저장 → Syncthing이 1분 내 S26으로 전파.

> ⚠️ **이미 예전 버전을 import 해뒀다면 다시 import 해야 합니다.** n8n은 JSON 파일과 연결돼 있지 않고 import 시점의 사본을 들고 있습니다. 기존 워크플로를 열어 전체 선택 후 삭제하고 붙여넣거나, 새로 import한 뒤 옛 워크플로를 비활성화하세요 (둘 다 Active면 같은 파일을 두 번 씁니다).

### 09:40 재시도 슬롯이 있는 이유

GitHub cron 실측 지연이 40~55분입니다. 2026-07-26 kr 세션은 08:04 KST에 끝나 08:50 fetch까지 마진이 46분뿐이었습니다. 지연이 조금만 더 커지면 그날 병합본을 놓치는데, 다음날 실행은 "오늘 날짜"만 보므로 **영구 누락**이 됩니다. 09:40 슬롯이 이 구멍을 막습니다. 이미 받은 파일은 같은 내용으로 덮어쓰므로 중복 실행은 무해합니다.

### 누락 경고

`3protv/YYYY/MM/`에 오늘 날짜 md가 하나도 없으면 **"누락?" IF 노드**가 텔레그램 경고를 보냅니다.

- **09:40 슬롯에서만** 보냅니다 — 07:10 시점엔 kr 병합본이 아직 없는 게 정상이라, 그때 경고하면 매일 오탐이 납니다. 판단은 "오늘 경로 계산" 노드가 내려주는 `hour` 값으로 합니다.
- `3protv/2026/07` 폴더 자체가 없으면 GitHub API가 **404**를 주는데, "파일 목록 조회" 노드의 `neverError` 옵션이 이를 정상 응답으로 받아 넘겨 흐름이 끊기지 않게 합니다. (2026-07-27 사고 당시엔 이 404에서 워크플로가 조용히 멈췄습니다.)

### 🔴 write 단계가 볼트 쪽 폴더 미생성으로 매번 죽고 있었다 (2026-08-11 사고)

`readWriteFile`의 write 오퍼레이션은 **상위 폴더를 자동 생성하지 않습니다.** 그 달 처음
저장되는 시점에 `/root/obsidian/3protv/YYYY/MM/`이 없으면 `ENOENT`로 죽습니다 — 이
워크플로가 생긴 이후 **다운로드+저장 경로를 탄 날은 전부 이 오류로 실패**했고, Executions에
`success`로 보인 날들은 전부 "누락?" 분기(리포트가 아직 안 올라와 경고만 보내고 끝)만 탄
것이었습니다. 즉 볼트에 파일이 실제로 저장된 적이 사실상 없었습니다.

**첫 시도(실패)**: "md 다운로드"와 "RaeVault에 저장" 사이에 Code 노드를 넣고
`fs.mkdirSync(path.dirname(target), {recursive:true})`를 실행하려 했으나, **이 n8n
인스턴스의 Code 노드 샌드박스가 `require('fs')`/`require('path')`를 막아** 그 자리에서
또 죽었다(second-brain 워크플로에서 통했던 방식이 여기선 안 통함 — 인스턴스마다
`NODE_FUNCTION_ALLOW_BUILTIN` 설정이 다를 수 있음을 의미).

**수정(적용됨)**: Code 노드 대신 **Execute Command 노드**로 교체 —
`mkdir -p "$(dirname "{{ target }}")"`를 셸에서 직접 실행하므로 n8n 샌드박스를
타지 않는다. 단, 이 노드를 끼우는 **위치**가 중요하다:

- Execute Command 노드는 입력의 **binary를 보존하지 않는다** (출력 json이 stdout/exitCode로
  대체됨). "md 다운로드"(binary 응답)와 "RaeVault에 저장"(그 binary를 쓰는 노드) 사이에
  끼우면 binary가 끊겨 write가 "binary data not found"로 또 실패한다.
- 그래서 **"누락?" 판정 직후, "md 다운로드" 이전**에 배치했다 — mkdir은 애초에
  `target` 경로 문자열만 있으면 되고 다운로드 결과와 무관하므로 순서를 앞당겨도 무방하다.
- 대신 "md 다운로드"의 `url` 파라미터를 `$json.download_url`(직전 노드에 의존)에서
  `$('오늘 파일 필터').item.json.download_url`(명시적 노드 참조)로 바꿔, 사이에
  Execute Command 노드가 끼어들어도 안 깨지게 했다. "RaeVault에 저장"은 원래부터
  `$('오늘 파일 필터')`를 명시 참조하고 있었어서 그대로 뒀다.

`n8n_3tv_sync_workflow.json`에 이 구조(누락? → 저장 폴더 생성 → md 다운로드 → RaeVault에
저장) 그대로 반영해 뒀다. **재import하면 이 순서·노드가 같이 들어오므로 다시 이 버그로
돌아가지 않는다.**

교훈 두 가지:
1. Executions의 `success`는 "그 실행이 끝까지 정상 종료됐다"는 뜻이지 "리포트가
   실제로 저장됐다"는 뜻이 아니다. 어느 분기를 탔는지(다운로드 경로 vs 누락 경고 경로)까지
   확인해야 한다.
2. 다른 워크플로(예: second-brain)에서 통했던 Code 노드 + `require()` 패턴이 이 n8n
   인스턴스에서도 통한다고 가정하지 말 것 — 샌드박스 허용 모듈은 인스턴스별 설정이다.
   셸 명령이 필요하면 처음부터 Execute Command 노드를 고려한다.
3. 파이프라인 중간에 **binary 데이터를 다루지 않는 노드**(Code, Execute Command 등)를
   끼워 넣을 땐 그 노드가 입력 binary/json을 그대로 통과시키는지 먼저 확인할 것 —
   안 그러면 뒤쪽 노드의 암묵적 `$json`/binary 의존이 조용히 끊긴다.

## 수동 구성 시 노드 구조 (import가 안 될 때)

| # | 노드 | 설정 |
|---|------|------|
| 1 | Schedule Trigger | Cron: `10 7 * * 1-5`, `50 8 * * 1-5`, `40 9 * * 1-5` (KST) |
| 2 | Code (오늘 경로 계산) | 아래 스니펫 |
| 3 | HTTP Request (파일 목록) | GET `https://api.github.com/repos/{OWNER}/3tv-reports/contents/3protv/{yyyy}/{mm}?ref=main`, Header: `Authorization: Bearer <PAT>`, `Accept: application/vnd.github+json`, Options → Response → **Never Error** 켜기 |
| 4 | Code (오늘 파일 필터) | 응답 배열에서 `name`에 오늘 `YYYYMMDD` 포함 항목만, `download_url` 추출. 0건이고 `hour >= 9` 면 `{missing:true, text}` 1건 반환 |
| 5 | IF (누락?) | 조건: `{{ $json.missing }}` is true → 출력 0(참)=경고, 출력 1(거짓)=저장 폴더 생성 |
| 6 | HTTP Request (텔레그램 경고) | POST `https://api.telegram.org/bot<TOKEN>/sendMessage`, JSON body `{chat_id, text}` |
| 7 | Execute Command (저장 폴더 생성) | `mkdir -p "$(dirname "{{ $json.target }}")"` — Code 노드 + `require('fs')`는 이 인스턴스 샌드박스에 막혀 안 됨. **다운로드보다 먼저** 배치(이유는 위 사고 기록 참고) |
| 8 | HTTP Request (다운로드) | GET `{{ $('오늘 파일 필터').item.json.download_url }}` (직전 노드가 바뀌어도 안 깨지게 명시 참조), Header 동일, Response: **File** |
| 9 | Read/Write Files from Disk | Operation: Write, File Path: `{{ $('오늘 파일 필터').item.json.target }}`, Data Property: `data` — "다운로드" 바로 뒤에 붙여 binary 유지 |

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
