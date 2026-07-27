"""GH_PAT로 옵시디안 볼트(3tv-reports) push가 되는지만 확인하는 진단 스크립트.

리포트 파이프라인 전체(캡처·Gemini 비전·LLM 리포트)를 돌리지 않고 git
clone/commit/push 세 단계만 실행해 GH_PAT 재발급 후 빠르게 확인할 때 쓴다.
`3protv/` 안의 실제 리포트와 안 섞이도록 `_healthcheck/latest.md` 한 파일만
매번 덮어써 커밋한다 (기록이 쌓이지 않음).

주 1회 스케줄로도 돌아 GH_PAT 만료를 **터지기 전에** 잡는다 — 만료를 사후에만 알아
8일간 볼트가 비어 있었던 사고가 있었다(2026-07-27). `--alert` 를 주면 실패 시
텔레그램 경고까지 보낸다(수동 진단에서는 소음이 없도록 기본 꺼짐).

사용 (GitHub Actions에서는 vault-check.yml 워크플로가 호출):
    PYTHONPATH=src python scripts/check_vault_push.py [--alert]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from threetv.common import load_env, load_settings, log, now_kst, setup_logging  # noqa: E402
from threetv.notify_telegram import send_alert  # noqa: E402
from threetv.obsidian_archive import VAULT_TMP, _clone_vault  # noqa: E402


def _fail(msg: str, alert: bool) -> int:
    print(f"FAIL: {msg}")
    if alert:
        send_alert(
            f"⚠️ 3tv 볼트 push 점검 실패 ({now_kst():%m/%d %H:%M})\n"
            f"사유: {msg}\n\n"
            "이대로 두면 매일 아침 리포트가 옵시디안(탭S9·S26)에 올라가지 않습니다.\n"
            "조치: GH_PAT 재발급 후 vault-check 워크플로를 다시 실행하세요."
        )
    return 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--alert", action="store_true",
                   help="실패 시 텔레그램 경고 전송 (스케줄 실행용)")
    args = p.parse_args()

    setup_logging()
    load_env()
    settings = load_settings()
    obsidian_cfg = settings["obsidian"]

    vault, reason = _clone_vault(obsidian_cfg)
    if not vault:
        return _fail(reason or "볼트 clone 실패", args.alert)

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
            return _fail(f"커밋 실패 - {commit.stderr[-300:]}", args.alert)
        branch = obsidian_cfg.get("vault_branch", "main")
        push = subprocess.run(
            ["git", "-C", str(vault), "push", "-u", "origin", branch],
            capture_output=True, text=True, timeout=120,
        )
        if push.returncode != 0:
            return _fail(f"push 실패 - {push.stderr[-300:]}", args.alert)
        print("OK: GH_PAT로 볼트(3tv-reports) push 성공")
        return 0
    finally:
        import shutil
        shutil.rmtree(VAULT_TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
