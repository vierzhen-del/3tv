---
name: 볼트연동
description: 리포트가 옵시디안 볼트까지 도달하는 경로(3tv-reports → n8n(S9) → Syncthing → S26)와 실제 볼트 경로. "리포트가 옵시디안에 안 보여요" 문의나 동기화 구조 변경 시 사용.
---

# 볼트연동 — Syncthing 구조 (git 볼트 아님)

## 경로

```
3tv 파이프라인
  → push → 3tv-reports (private 중계 repo)
  → n8n 스케줄 (Tab S9, 07:10 / 08:50 KST) 이 fetch
  → 볼트의 3protv/YYYY/MM/*.md
  → Syncthing 이 S26 옵시디안으로 전파
```

push 경로는 **검증 완료**(2026-07-19 05:51 KST "3protv us 리포트" 커밋 실측).

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
