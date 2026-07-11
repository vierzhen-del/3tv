"""3tv 파이프라인 진입점.

사용 예:
  # 평일 아침 라이브 자동 실행 (Actions cron이 호출)
  python -m threetv.main --session us
  python -m threetv.main --session kr

  # 과거 VOD로 테스트 (라이브 대신 지정 영상 다운로드)
  python -m threetv.main --session us --vod-url "https://www.youtube.com/watch?v=..."

  # 이미 받은 영상 파일로 재실행 (캡처 생략)
  python -m threetv.main --session us --video-file output/20260711/us/us_capture.mp4

  # 전송 없이 분석 결과만 파일로 (구조 검증/디버깅)
  python -m threetv.main --session us --vod-url ... --skip-notify
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from . import market
from .capture import CaptureError, capture_live_session, download_vod
from .common import (load_env, load_holdings, load_settings, log, now_kst,
                     output_dir, setup_logging)
from .frames import prepare_frames
from .notify_kakao import send_kakao_memo
from .notify_telegram import send_alert, send_telegram
from .obsidian_archive import archive_report, read_us_section_today
from .report import extract_mentions, generate_report
from .transcribe import transcribe
from .vision import analyze_frames


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="삼프로TV 라이브 분석 → 데일리 시황 리포트")
    p.add_argument("--session", required=True, choices=["us", "kr"])
    p.add_argument("--vod-url", help="라이브 대신 VOD URL로 실행 (테스트/복구)")
    p.add_argument("--video-file", help="이미 받은 영상 파일로 실행 (캡처 생략)")
    p.add_argument("--skip-notify", action="store_true", help="텔레그램/카카오 전송 생략")
    p.add_argument("--skip-archive", action="store_true", help="옵시디안/노션 저장 생략")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def get_video(args: argparse.Namespace, settings: dict, out_dir: Path) -> Path:
    if args.video_file:
        video = Path(args.video_file)
        if not video.exists():
            raise CaptureError(f"영상 파일 없음: {video}")
        return video
    if args.vod_url:
        return download_vod(
            args.vod_url, out_dir / f"{args.session}_vod.mp4",
            settings["capture"]["resolution"],
        )
    return capture_live_session(settings, args.session, out_dir)


def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    session = args.session
    out_dir = output_dir(session)
    label = settings["sessions"][session]["label"]
    log.info("=== 3tv %s 세션 시작 (%s) ===", label, now_kst().strftime("%Y-%m-%d %H:%M"))

    # 1. 캡처 (라이브 / VOD / 로컬 파일)
    try:
        video = get_video(args, settings, out_dir)
    except CaptureError as e:
        log.error("캡처 실패: %s", e)
        if not args.skip_notify:
            send_alert(
                f"⚠️ 3tv {label} 캡처 실패 ({now_kst():%m/%d %H:%M})\n{e}\n"
                f"→ 로컬 실행 또는 VOD 재실행(--vod-url)으로 복구하세요."
            )
        return 1

    # 2. 프레임 추출 + 자료화면 선별 (흰 배경 휴리스틱 + 중복 제거)
    selected = prepare_frames(video, out_dir, settings["frames"])
    if not selected:
        log.warning("자료화면 후보가 0장 — 전사만으로 리포트 진행")

    # 3. Gemini 비전 분석 (자료화면 확정 + 텍스트/그래프 추출, 광고 제외)
    vision_results = analyze_frames(selected, settings["models"]["gemini"], out_dir) \
        if selected else []

    # 4. Whisper 음성 전사
    transcript = transcribe(video, out_dir, settings["models"]["whisper"])

    # 5. 종목 추출 → 실시세 검증
    mentions = extract_mentions(settings["models"]["claude"], vision_results, transcript)
    verified = market.verify_mentions(mentions)
    indices = market.fetch_indices(settings["market"]["indices"])
    holdings_data = load_holdings()
    holdings_quotes = market.fetch_holdings_quotes(
        holdings_data["holdings"] + holdings_data["watchlist"]
    )

    # kr 세션은 오늘 아침 us 리포트를 컨텍스트로 사용
    us_context = ""
    if session == "kr" and not args.skip_archive:
        us_context = read_us_section_today(settings["obsidian"])

    # 6. Claude 최종 리포트
    report = generate_report(
        settings, session, vision_results, transcript,
        indices, verified, holdings_data, holdings_quotes,
        out_dir, us_context_md=us_context,
    )

    # 7. 전송 (텔레그램 → 카카오, best-effort)
    header = f"📌 {settings['report']['title_prefix']}_{now_kst():%Y%m%d}_{report['title_keyword']} [{label}]"
    telegram_text = f"{header}\n\n{report['telegram_text']}"
    if args.skip_notify:
        log.info("--skip-notify: 전송 생략 (결과는 %s 에 저장됨)", out_dir)
    else:
        send_telegram(telegram_text, settings["telegram"]["max_message_len"])
        if settings.get("kakao", {}).get("enabled", True):
            send_kakao_memo(telegram_text)

    # 8. 아카이브 (옵시디안 볼트 → S26/탭S9 동기화, 노션 선택)
    if not args.skip_archive:
        archive_report(
            settings["obsidian"], session, report["title_keyword"],
            report["markdown_report"], indices, report.get("holdings_mentioned", []),
        )
        if settings.get("notion", {}).get("enabled"):
            from .notion_archive import archive_to_notion
            archive_to_notion(report["title_keyword"], report["markdown_report"])

    log.info("=== %s 세션 완료 ===", label)
    return 0


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)
    load_env()
    try:
        return run(args)
    except Exception as e:
        log.error("파이프라인 오류: %s\n%s", e, traceback.format_exc())
        if not args.skip_notify:
            send_alert(f"❌ 3tv {args.session} 세션 오류 ({now_kst():%m/%d %H:%M})\n{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
