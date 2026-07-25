"""텔레그램으로 공지 메시지 전송 (변경사항 알림 등).

리포트와 무관한 일반 공지를 보낼 때 쓴다. 파이프라인 코드(notify_telegram)를
그대로 재사용하므로 토큰 처리·메시지 분할 규칙이 리포트 발송과 동일하다.

사용:
    python scripts/send_notice.py docs/notices/20260725_changes.md
    (GitHub Actions에서는 notify.yml 워크플로가 호출)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from threetv.common import load_env, setup_logging  # noqa: E402
from threetv.notify_telegram import send_telegram  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("사용법: send_notice.py <메시지파일>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"메시지 파일 없음: {path}", file=sys.stderr)
        return 1

    setup_logging()
    load_env()
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        print("메시지가 비어 있어 전송하지 않습니다", file=sys.stderr)
        return 1
    return 0 if send_telegram(text) else 1


if __name__ == "__main__":
    sys.exit(main())
