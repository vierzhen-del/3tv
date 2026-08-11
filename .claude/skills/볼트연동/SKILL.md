---
name: 볼트연동
description: 리포트가 옵시디안 볼트까지 도달하는 경로(3tv-reports → n8n(S9) → Syncthing → S26)와 실제 볼트 경로. "리포트가 옵시디안에 안 보여요" 문의나 동기화 구조 변경 시 사용.
---

# 볼트연동 — Syncthing 구조 (git 볼트 아님)

## 경로

```
3tv 파이프라인
  → push → 3tv-reports (private 중계 repo)
  → n8n 스케줄 (Tab S9, 07:10 / 08:50 / 09:40 KST) 이 fetch
  → 볼트의 3protv/YYYY/MM/*.md
  → Syncthing 이 S26 옵시디안으로 전파
```

## 🔴 "옵시디안에 안 보여요" 진단 순서

증상이 나오면 **뒤에서부터가 아니라 앞에서부터** 확인한다. 실제 사고는 거의 항상 첫 구간이었다.

> ⚠️ **2026-08-11 기준 4번이 구조적으로 끊겨있다 (미해결).** n8n 프로세스의
> `/root/obsidian`이 실제 볼트로 bind mount돼 있지 않아, 3번까지 전부 `success`로
> 보여도 실제로는 볼트에 도달하지 못한다. 아래 "n8n의 `/root/obsidian`이 실제
> 볼트에 bind mount돼 있지 않았다" 절이 해결되기 전까진 이 표의 3번 확인만으로는
> 부족하다 — 반드시 실제 볼트 경로(4번)에 파일이 생겼는지까지 직접 확인할 것.

| # | 확인 | 방법 |
|---|---|---|
| 1 | `GH_PAT` 이 살아있나 | 3tv Actions → **vault-check** 워크플로 수동 실행. FAIL이면 여기서 끝 — PAT 재발급 |
| 2 | 리포트가 중계 repo에 올라왔나 | `3tv-reports` 의 `3protv/YYYY/MM/` 에 오늘 날짜 md 2개(`3protv오늘_*`, `3protv기사_*`) |
| 3 | n8n이 받아갔나 | Tab S9 n8n → 해당 워크플로 Executions 탭. **`success`만 보지 말고 어느 분기를 탔는지 확인** — 아래 2026-08-11 사고 참고 |
| 4 | 실제 볼트에 파일이 있나 | S9 파일탐색기로 `/storage/emulated/0/Documents/vierzhen_home/3protv/` **직접** 확인 (n8n proot 안의 `/root/obsidian`이 아니라) |
| 5 | Syncthing이 전파했나 | 4번이 확인된 뒤에만 의미 있음 → S26에서 같은 파일 확인 |

## ⚠️ GH_PAT 만료는 예전엔 침묵 실패였다 (2026-07-27 사고)

PAT가 만료돼 push가 8일간 끊겼는데 **Actions는 계속 초록불**이었다. `archive_report()` 가
best-effort라 실패해도 호출부가 반환값을 안 봤고, 텔레그램에는 「옵시디안에서 열기」 딥링크가
그대로 붙어 나가 눌러도 빈 검색 결과만 떴다. 그래서 다음을 넣었다:

- 볼트 저장 실패 → **텔레그램 경고 + 종료코드 1**(Actions 빨간 X). 저장위치 줄은 성공했을 때만 붙는다
- `vault-check` 를 **주 1회 스케줄**(월 06:00 KST)로 승격 — 만료를 아침 리포트 유실 전에 잡는다
- n8n 쪽도 오늘 날짜 md가 0건이면 09:40 슬롯에서 텔레그램 경고

즉 **이제는 조용히 실패하지 않는다.** 경고 없이 "안 보인다"면 볼트/Syncthing 구간(위 표 3~4번)을 먼저 의심할 것.

## 🔴 n8n write 단계가 사실상 한 번도 성공한 적이 없었다 (2026-08-11 사고, 구조 수정 완료·실행 검증 대기)

3~4번(n8n/Syncthing) 진단을 실제로 파봤더니 **"RaeVault에 저장" 노드가 매번
`ENOENT: no such file or directory, open '/root/obsidian/3protv/2026/08/...'`로
죽고 있었다.** 원인은 `n8n-nodes-base.readWriteFile`의 write 오퍼레이션이
**상위 폴더를 자동 생성하지 않는다**는 것 — `3protv/2026/08/` 디렉토리 자체가
한 번도 만들어진 적이 없었다.

Executions 탭에서 과거 실행이 `success`로 보인 날들도 속아 넘어가기 쉽다 — 실제로는
그날 리포트가 아직 안 올라와서 "누락?" 분기(텔레그램 경고만 보내고 끝)를 탄 것뿐이었고,
**파일이 실제로 존재해서 다운로드→저장 경로를 탄 날은 전부 ENOENT로 죽어 있었다.**
즉 이 워크플로는 만들어진 이후 지금까지 볼트에 파일을 저장하는 데 **단 한 번도**
성공한 적이 없다 — `3tv-reports`(중계 repo, GitHub)엔 항상 정상 도착했지만
그 다음 로컬 저장 단계에서 전부 막혀 있었다.

**1차 시도(실패)**: "md 다운로드" → "RaeVault에 저장" 사이에 Code 노드를 추가해
`fs.mkdirSync(path.dirname(target), {recursive:true})`를 시도했으나, 이 n8n 인스턴스의
Code 노드 샌드박스가 `require('fs')`를 막고 있어 **또 다른 이유로 죽었다** — second-brain
워크플로에서 통했던 방식이 이 인스턴스에서도 통할 거라 가정한 게 틀렸다. n8n의
`NODE_FUNCTION_ALLOW_BUILTIN` 허용 모듈은 인스턴스별 설정이라 워크플로 간에 이식되지
않는다.

**2차 시도(구조 검증 완료)**: Code 노드 대신 **Execute Command 노드**로 교체해
`mkdir -p "$(dirname "…")"`를 셸에서 직접 실행 — 샌드박스를 타지 않는다. 단 Execute
Command는 입력 binary를 보존하지 않으므로, "md 다운로드"(binary 응답)와 "RaeVault에
저장"(그 binary를 쓰는 노드) 사이에 끼우면 또 다른 방식으로 깨진다. 그래서 **"누락?"
판정 직후·"md 다운로드" 이전**으로 위치를 옮기고, "md 다운로드"의 `url`도
`$json.download_url`(직전 노드 의존)에서 `$('오늘 파일 필터').item.json.download_url`
(명시적 참조)로 바꿔 순서가 바뀌어도 안 깨지게 했다. `docs/n8n_3tv_sync_workflow.json`에
이 구조 그대로 반영 — **재import해도 이 버그가 되살아나지 않는다.**

09:40 KST 정기 실행에서 다운로드 경로까지는 정상 진입 확인. 이후 아래 bind mount
사고가 드러나 실제 저장 검증은 그 문제 해결 후로 넘어갔다.

**교훈 세 가지**:
1. n8n Executions의 `success` 표시는 "그 실행에서 도달한 마지막 분기가 정상
   종료됐다"는 뜻이지 "리포트가 실제로 저장됐다"는 뜻이 아니다. 어느 분기를
   탔는지까지 확인할 것.
2. 다른 워크플로에서 통했던 Code 노드 + `require()` 패턴을 이 인스턴스에서도
   통할 거라 가정하지 말 것 — 인스턴스별 샌드박스 설정 차이로 조용히 죽는다.
3. 워크플로 중간에 binary를 다루지 않는 노드(Code, Execute Command 등)를 끼워
   넣을 땐 그 노드가 입력 json/binary를 그대로 통과시키는지부터 확인할 것 —
   안 그러면 뒤쪽 노드의 암묵적 `$json`/binary 의존이 조용히 끊긴다.

이 사고로 볼트에 한 번도 안 들어간 과거분(2026-07~08)은 **새 워크플로를 만들지 않고**
기존 동기화 워크플로 안에 백필 전용 경로를 추가해 메웠다 — GH_PAT가 Credential 객체가
아니라 노드에 평문으로 박혀있어서, 새 워크플로로 옮기려면 값을 다시 타이핑해야 하고
그게 곧 노출 경로이기 때문이다. 실제로 이 과정에서 GH_PAT·텔레그램 봇 토큰이 도구 호출
결과에 노출되는 사고가 있었다(`mode:"details"` 조회가 안전필터를 우회) — 상세와 교훈은
`docs/n8n_s9_sync.md`의 "과거분 백필" 절 참고. **토큰류를 다루는 일회성 작업은 새
워크플로/새 자격증명 입력을 만들지 말고, 이미 그 토큰이 박혀있는 검증된 워크플로를
재배선해서 쓸 것.**

백필 결과: `3protv/2026/07` 15개 + `2026/08` 33개 = 48개, `3tv-reports`(GitHub) 목록과
정확히 일치 — **하지만 로컬 폴더(`/root/obsidian/3protv/`)에서 실제 볼트로 사람이
수동 복사한 결과다.** n8n이 직접 쓴 게 아니다 — 바로 아래 사고 때문에 n8n은 애초에
실제 볼트 경로에 쓸 수 없는 상태였다.

## 🔴 n8n의 `/root/obsidian`이 실제 볼트에 bind mount돼 있지 않았다 (2026-08-11, 진단 중 — 미해결)

백필 도중 드러난, 지금까지의 사고보다 더 근본적인 문제. **n8n 프로세스의 마운트
네임스페이스에서 `/root/obsidian`은 실제 볼트(`/storage/emulated/0/Documents/vierzhen_home`)로
연결된 bind mount가 아니라, 그냥 비어있는 독립된 로컬 폴더다.**

- **정확한 원인(2026-08-11 진단 확정)**: 정식 시작 스크립트
  `/data/data/com.termux/files/home/n8n_launch.sh`(2026-07-20 작성, PostgreSQL
  자동 점검·복구 + cloudflared 터널 재생성까지 포함하는 idempotent 스크립트)가
  이미 존재하고, 정확히 이 사고를 막는 `--bind
  /storage/emulated/0/Documents/vierzhen_home:/root/obsidian`과
  `NODE_FUNCTION_ALLOW_BUILTIN=fs,path`를 둘 다 설정한다. 그런데 **지금 떠 있는
  n8n(PID 27580)은 이 스크립트를 거치지 않고, 어느 세션에서 즉석으로 실행한 1회성
  inline 명령(`eval 'export DB_TYPE=... n8n start'`)으로 뜬 것**이었다 — bind도
  `NODE_FUNCTION_ALLOW_BUILTIN`도 둘 다 빠진 "약식" 기동. **위 사고에서 Code 노드의
  `require('fs')`가 막혔던 것도 "n8n 인스턴스 자체의 샌드박스 제약"이 아니라
  이 즉석 기동에 `NODE_FUNCTION_ALLOW_BUILTIN`이 빠져있었기 때문**이었다 — 정식
  스크립트로 재기동하면 Code 노드 + `fs`도 될 가능성이 높다(단, Execute Command로
  바꾼 결정 자체는 되돌릴 필요 없음 — 어느 쪽이든 동작하는 더 견고한 선택).
- 그 결과 07:10/08:50/09:40 스케줄 실행이 (mkdir·저장 단계까지 포함해) "성공"으로
  보여도, 전부 이 끊어진 로컬 폴더에만 쓰고 있었다 — 실제 볼트·Syncthing에는 지금까지
  한 번도 자동으로 도달한 적이 없다. 이번에 사람이 수동 복사한 48개가 유일하게 실제
  볼트에 들어간 리포트다.
- **워크플로 쪽 경로를 아무리 고쳐도 해결 안 된다** — 마운트 네임스페이스가 다른
  이상, n8n 프로세스 자체가 정식 스크립트로 재시작돼야 한다. **교훈**: Claude
  Code 세션이 즉석으로 서비스를 띄워야 할 때는, 먼저 홈 디렉토리에 이미 그 서비스용
  정식 시작 스크립트가 있는지부터 찾을 것 — 즉석 명령은 스크립트에 이미 반영된
  환경설정(bind mount, 허용 모듈 등)을 빠뜨리기 쉽다.
- **1차 재기동 결과(부분 해결)**: `n8n_launch.sh`로 재기동해 `NODE_FUNCTION_ALLOW_BUILTIN=fs,path`는
  정상 반영됐다(Code 노드 + `fs`도 이제 될 것으로 추정). **하지만 bind mount는 여전히 안 걸렸다.**
- **🔴 구조적 제약 발견**: `proot-distro login`은 **중첩이 안 된다**(ptrace 기반 기술적 한계) —
  Claude Code CLI 세션 자체가 이미 proot 안에서 돌고 있는 상태에서는, 그 세션이 `n8n_launch.sh`를
  실행해도 내부에서 또 다른 proot(+bind)를 걸 수 없다. **이건 스크립트나 워크플로를 아무리 고쳐도
  CLI 세션 안에서는 못 푸는 문제다** — proot에 걸리지 않은 **새 Termux 세션에서 사람이 직접**
  `bash ~/n8n_launch.sh`를 실행해야 한다.
- (참고: n8n-mcp 연결은 cloudflared 터널이 아니라 셀룰러 IP 기반이라 n8n 재시작과 무관하게
  유지된다 — 위에서 우려했던 재연결 문제는 실제로는 발생하지 않았다.)
- **재시작 진행 상태**: bind mount 복구는 사용자가 별도 Termux 세션에서 직접 실행 예정 —
  완료·검증되면 이 섹션을 "해결"로 갱신할 것.

**아직 재시작하지 않았다.** 2026-07-20 복구 때 n8n이 (Node 버전 비호환 + DB 테이블
소유권 불일치 + 스키마 권한 미부여) 3중 장애로 죽은 전례가 있어, 재시작을 함부로
시도하지 않고 우선 (1) n8n이 정확히 어떤 명령/스크립트로 떠 있는지 진단 → (2) 올바른
`--bind` 옵션을 포함한 수정안 확정 → (3) 리포트 스케줄 시간대를 피해 사용자 승인 하에
재시작, 순서로 진행 중이다. **해결 전까지는 스케줄 실행이 계속 성공으로 보여도 실제
볼트엔 안 들어간다** — 진단 순서표 3~4번을 볼 때 이 사고가 완전히 풀렸는지부터 먼저
확인할 것.

## ⚠️ 「옵시디안에서 열기」 딥링크는 걷어냈다 (2026-08-02)

`obsidian://search?vault=…` 딥링크는 **탭S9에서 눌러도 옵시디안이 열리지 않았다.** 안드로이드가
커스텀 스킴을 앱으로 넘기지 못하면 링크가 죽은 채 남는데, 눌러보기 전에는 알 수 없다.
지금은 `vault_location_link()`(obsidian_archive.py)가 만드는 **저장위치 줄**이 그 자리를 대신한다:

- 링크 글자 = 볼트 안 실제 경로(`vierzhen_home/3protv/YYYY/MM/….md`) — 딥링크가 안 열리는
  기기에서도 "어디 저장됐는지"는 눈으로 확인된다
- 링크 주소 = 중계 repo(`3tv-reports`)의 그 파일 **https URL** — 브라우저로 열리므로 기기 무관
- 그 아래에 `🕘 생성 YYYY-MM-DD HH:MM KST` 한 줄(`main.report_footer()`)

`obsidian_deeplink()` 함수 자체는 남겨 뒀지만 **호출부가 없다** — 다시 쓰려면 실기기에서
열리는지부터 확인할 것.

## ⚠️ 실제 볼트 폴더명은 `vierzhen_home` (2026-07-20 실측 정정)

```
/storage/emulated/0/Documents/vierzhen_home/3protv/
```

proot 안에서는 `--bind /storage/emulated/0/Documents/vierzhen_home:/root/obsidian` 후
`/root/obsidian/3protv/`.

**"RaeVault"는 노션 문서의 별칭일 뿐 실제 폴더명이 아니었다** — 예전 문서에 남은 `RaeVault/...` 표기는
전부 위 경로로 치환해서 읽을 것.

## n8n 수신 워크플로 (2026-07-20 완료)

Tab S9에서 Claude Code CLI + n8n-mcp로 워크플로를 생성했고, GitHub PAT는 n8n 웹UI에서 사용자가 직접
입력(보안상 자동화 제외), Active 전환까지 완료했다.

이 과정에서 n8n 서버 자체가 (Node 버전 비호환 + DB 테이블 소유권 불일치 + 스키마 권한 미부여) 3중
장애로 죽어 있던 것도 함께 복구됐다.

- [노션 — n8n 재시작 실패 3종 해결](https://app.notion.com/p/3a35efd0e46281ab8353c57aa586bf6f)
- `docs/n8n_s9_sync.md` + `docs/n8n_3tv_sync_workflow.json`

## 폐기된 대안

`second-brain` git repo + Obsidian Git 플러그인 방식은 **이중 동기화 충돌 위험으로 2026-07-18 폐기**됐다.
n8n 통합이 실패할 때만 폴백으로 쓴다.
