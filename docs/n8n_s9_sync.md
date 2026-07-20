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
2. import 후 수정할 것 2곳:
   - **"오늘 경로 계산" Code 노드**: `OWNER` 상수를 본인 계정(`vierzhen-del`)으로
   - **"파일 목록 조회" HTTP Request 노드**: Header `Authorization` 값의 `<N8N_GITHUB_PAT>`를 위에서 발급한 토큰으로 교체 (또는 n8n Credential로 등록해 연결)
3. 워크플로 Settings → **Timezone: Asia/Seoul** 확인
4. 활성화(Active 토글)

동작: 평일 **07:10**(us 리포트)과 **08:50**(kr 병합본) KST에 실행 → `3tv-reports`의 `3protv/YYYY/MM/`에서 오늘 날짜 md를 찾아 `/root/obsidian/3protv/YYYY/MM/`에 저장 → Syncthing이 1분 내 S26으로 전파.

## 수동 구성 시 노드 구조 (import가 안 될 때)

| # | 노드 | 설정 |
|---|------|------|
| 1 | Schedule Trigger | Cron: `10 7 * * 1-5` 와 `50 8 * * 1-5` (KST) |
| 2 | Code (오늘 경로 계산) | 아래 스니펫 |
| 3 | HTTP Request (파일 목록) | GET `https://api.github.com/repos/{OWNER}/3tv-reports/contents/3protv/{yyyy}/{mm}?ref=main`, Header: `Authorization: Bearer <PAT>`, `Accept: application/vnd.github+json` |
| 4 | Code (오늘 파일 필터) | 응답 배열에서 `name`에 오늘 `YYYYMMDD` 포함 항목만, `download_url` 추출 |
| 5 | HTTP Request (다운로드) | GET `{{download_url}}`, Header 동일, Response: **File** |
| 6 | Read/Write Files from Disk | Operation: Write, File Path: `/root/obsidian/3protv/{yyyy}/{mm}/{name}` |

2번 Code 노드 스니펫:
```javascript
const now = new Date(new Date().toLocaleString("en-US", {timeZone: "Asia/Seoul"}));
const yyyy = String(now.getFullYear());
const mm = String(now.getMonth() + 1).padStart(2, "0");
const ymd = yyyy + mm + String(now.getDate()).padStart(2, "0");
return [{ json: { yyyy, mm, ymd } }];
```

## write 충돌 규칙 (Syncthing)

- 이 워크플로는 볼트 내 **`3protv/` 폴더에만** 씁니다 (기존 n8n `00_Inbox/` 전용 규칙에 3protv/ 추가)
- 매일 새 파일 생성 + kr 실행 시 같은 날짜 파일 덮어쓰기(전체 교체)뿐이라, S26에서 이 폴더를 동시에 편집하지만 않으면 `.sync-conflict`가 생기지 않습니다
- 리포트에 개인 메모를 달고 싶으면 파일을 직접 수정하지 말고 별도 노트에서 링크하는 방식을 권장

## 확인 방법

1. n8n에서 워크플로 **Execute Workflow**로 수동 1회 실행 → `/root/obsidian/3protv/` 아래 md 생성 확인
2. S9 옵시디안(또는 파일탐색기)에서 `/storage/emulated/0/Documents/vierzhen_home/3protv/` 확인
3. 1분 내 S26 옵시디안에 같은 파일 나타나는지 확인
