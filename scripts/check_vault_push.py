"""GH_PAT로 옵시디안 볼트(3tv-reports) push가 되는지만 확인하는 진단 스크립트.

리포트 파이프라인 전체(캡처·Gemini 비전·LLM 리포트)를 돌리지 않고 git
clone/commit/push 세 단계만 실행해 GH_PAT 재발급 후 빠르게 확인할 때 쓴다.
`3protv/` 안의 실제 리포트와 안 섞이도록 `_healthcheck/latest.md` 한 파일만
매번 덮어써 커밋한다 (기록이 쌓이지 않음).

사용 (GitHub Actions에서는 vault-check.yml 워크플로가 호출):
    PYTHONPATH=src python scripts/check_vault_push.py
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from threetv.common import load_env, load_settings, log, now_kst, setup_logging  # noqa: E402
from threetv.obsidian_archive import VAULT_TMP, _clone_vault  # noqa: E402


def main() -> int:
    setup_logging()
    load_env()
    settings = load_settings()
    obsidian_cfg = settings["obsidian"]

    vault = _clone_vault(obsidian_cfg)
    if not vault:
        print("FAIL: 볼트 clone 실패 (GH_PAT 미설정이거나 토큰이 유효하지 않습니다)")
        return 1

    try:
        check_dir = vault / "_healthcheck"
        check_dir.mkdir(parents=True, exist_ok=True)
        (check_dir / "latest.md").write_text(
            f"GH_PAT 동작 확인: {now_kst():%Y-%m-%d %H:%M:%S KST}\n"
            f"(scripts/check_vault_push.py 자동 생성 — 매번 덮어씀)\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(vault), "add", "_healthcheck/latest.md"],
            check=True, capture_output=True,
        )
        commit = subprocess.run(
            ["git", "-C", str(vault),
             "-c", "user.name=3tv-bot", "-c", "user.email=3tv-bot@users.noreply.github.com",
             "commit", "-m", f"vault-check {datetime.now():%Y-%m-%d %H:%M:%S}"],
            capture_output=True, text=True,
        )
        if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
            print(f"FAIL: 커밋 실패 - {commit.stderr[-300:]}")
            return 1
        branch = obsidian_cfg.get("vault_branch", "main")
        push = subprocess.run(
            ["git", "-C", str(vault), "push", "-u", "origin", branch],
            capture_output=True, text=True, timeout=120,
        )
        if push.returncode != 0:
            print(f"FAIL: push 실패 - {push.stderr[-300:]}")
            return 1
        print("OK: GH_PAT로 볼트(3tv-reports) push 성공")
        return 0
    finally:
        import shutil
        shutil.rmtree(VAULT_TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
