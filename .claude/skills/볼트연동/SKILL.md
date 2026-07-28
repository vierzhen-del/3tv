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
  → Syncthing (Tailscale 메시 위) 이 S23U·S26 으로 전파
```

Syncthing은 **3노드**다 — S9(원본·Send&Receive) · S23U(백업·**Receive Only**+버저닝) · S26(편집).
구성·기기별 설정값은 볼트 `00_지침/2026-07-28_tailscale-syncthing-3노드구성.md`.

## 🔴 "옵시디안에 안 보여요" 진단 순서

증상이 나오면 **뒤에서부터가 아니라 앞에서부터** 확인한다. 실제 사고는 거의 항상 첫 구간이었다.

| # | 확인 | 방법 |
|---|---|---|
| 1 | `GH_PAT` 이 살아있나 | 3tv Actions → **vault-check** 워크플로 수동 실행. FAIL이면 여기서 끝 — PAT 재발급 |
| 2 | 리포트가 중계 repo에 올라왔나 | `3tv-reports` 의 `3protv/YYYY/MM/` 에 오늘 날짜 md 2개(`3protv오늘_*`, `3protv기사_*`) |
| 3 | n8n이 받아갔나 | Tab S9 n8n → 해당 워크플로 Executions 탭 |
| 4 | 볼트 원본에 있나 | S9 `/storage/emulated/0/Documents/vierzhen_home/3protv/` |
| 5 | Tailscale이 붙어 있나 | 관리 콘솔에서 3노드 전부 `Connected`. **키 만료(기본 180일)로 조용히 빠졌을 수 있다** |
| 6 | Syncthing이 전파했나 | S23U·S26 에 도착했나. 원격 기기 상태가 `Relay` 면 직결 실패(느림) |

**S23U는 Receive Only 노드다** — 여기서 옵시디안으로 편집하면 "Local Additions"로 동기화가 멈춘다.
지워진 노트를 되살릴 때는 S23U의 Staggered 버저닝 폴더를 본다.

## ⚠️ GH_PAT 만료는 예전엔 침묵 실패였다 (2026-07-27 사고)

PAT가 만료돼 push가 8일간 끊겼는데 **Actions는 계속 초록불**이었다. `archive_report()` 가
best-effort라 실패해도 호출부가 반환값을 안 봤고, 텔레그램에는 「옵시디안에서 열기」 딥링크가
그대로 붙어 나가 눌러도 빈 검색 결과만 떴다. 그래서 다음을 넣었다:

- 볼트 저장 실패 → **텔레그램 경고 + 종료코드 1**(Actions 빨간 X). 딥링크는 성공했을 때만 붙는다
- `vault-check` 를 **주 1회 스케줄**(월 06:00 KST)로 승격 — 만료를 아침 리포트 유실 전에 잡는다
- n8n 쪽도 오늘 날짜 md가 0건이면 09:40 슬롯에서 텔레그램 경고

즉 **이제는 조용히 실패하지 않는다.** 경고 없이 "안 보인다"면 볼트/Tailscale/Syncthing 구간(위 표 4~6번)을 먼저 의심할 것.

단 **09:40 경고는 S9에서 나온다** — S9 자체가 죽으면 경고도 안 온다. 이 사각지대는 S23U(LTE 폴백 보유)의
하트비트와 GitHub Actions `vault-check` 가 메우도록 계획돼 있다(볼트 구성 문서 참조).

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
