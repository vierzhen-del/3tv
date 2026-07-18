"""공통 유틸: 설정 로딩, KST 시간, 경로, 로깅."""
from __future__ import annotations

import logging
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
OUTPUT_ROOT = REPO_ROOT / "output"

log = logging.getLogger("threetv")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def load_env() -> None:
    """로컬 실행용 .env 로딩. Actions에서는 Secrets가 env로 주입됨.

    기존 v28 파이프라인의 C:\\3protv\\.env 도 그대로 인식한다
    (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY,
     NOTION_API_KEY, NOTION_PARENT_ID 등 동일 키 재사용).
    """
    load_dotenv(REPO_ROOT / ".env")
    legacy = Path(r"C:\3protv\.env")
    if legacy.exists():
        load_dotenv(legacy, override=False)  # 이미 설정된 값은 유지


def load_settings() -> dict:
    with open(CONFIG_DIR / "settings.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_holdings() -> dict:
    path = CONFIG_DIR / "holdings.yaml"
    if not path.exists():
        return {"holdings": [], "watchlist": []}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("holdings", [])
    data.setdefault("watchlist", [])
    data["holdings"] = data["holdings"] or []
    data["watchlist"] = data["watchlist"] or []
    return data


def now_kst() -> datetime:
    return datetime.now(KST)


def parse_kst_time(hhmm: str, base: datetime | None = None) -> datetime:
    """'HH:MM' 문자열을 오늘(KST) 날짜의 datetime으로 변환."""
    base = base or now_kst()
    h, m = map(int, hhmm.split(":"))
    return base.replace(hour=h, minute=m, second=0, microsecond=0)


def session_window(settings: dict, session: str) -> tuple[datetime, datetime, datetime]:
    """세션의 (폴링시작, 방송시작, 방송종료) KST datetime."""
    cfg = settings["sessions"][session]
    poll_from = parse_kst_time(cfg["poll_from_kst"])
    start = parse_kst_time(cfg["start_kst"])
    end = parse_kst_time(cfg["end_kst"])
    return poll_from, start, end


def output_dir(session: str, date: datetime | None = None, tag: str | None = None) -> Path:
    """세션 산출물 디렉터리. tag를 주면 별도 하위 폴더로 분리
    (예: 트리밍 테스트를 전체구간 결과와 겹치지 않게 비교 보관)."""
    d = (date or now_kst()).strftime("%Y%m%d")
    subdir = f"{session}_{tag}" if tag else session
    path = OUTPUT_ROOT / d / subdir
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_duration(s: str) -> int:
    """'MM:SS', 'HH:MM:SS' 또는 순수 초 문자열을 초 단위 정수로 변환."""
    s = s.strip()
    if ":" not in s:
        return int(s)
    parts = [int(p) for p in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts[-3:]
    return h * 3600 + m * 60 + sec


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()
