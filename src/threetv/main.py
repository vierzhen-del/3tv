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

  # VOD 구간 트리밍 사전검토 (전체 대신 지정 구간만 다운로드·분석, 비용 절감)
  # 결과는 output/YYYYMMDD/us_trim/ 에 저장되어 전체구간 결과(output/YYYYMMDD/us/)와 비교 가능
  python -m threetv.main --session us --vod-url ... --trim-start 6:00 --trim-duration 3:00 \
    --skip-notify --skip-archive
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from . import market, news, tg_format
from .capture import CaptureError, capture_live_session, download_vod
from .common import (load_env, load_holdings, load_settings, log, now_kst,
                     output_dir, parse_duration, setup_logging, window_offsets)
from .frames import prepare_frames
from .notify_kakao import send_kakao_memo
from .notify_telegram import send_alert, send_telegram
from .obsidian_archive import (DISABLED, ArchiveResult, archive_report,
                               obsidian_deeplink, read_us_section_today)
from .report import (drop_etf_stocks, extract_mentions, generate_report,
                     us_stocks_in_captures)
from .transcribe import transcribe
from .vision import analyze_frames


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="삼프로TV 라이브 분석 → 데일리 시황 리포트")
    p.add_argument("--session", required=True, choices=["us", "kr"])
    p.add_argument("--vod-url", help="라이브 대신 VOD URL로 실행 (테스트/복구)")
    p.add_argument("--video-file", help="이미 받은 영상 파일로 실행 (캡처 생략)")
    p.add_argument("--trim-start", metavar="MM:SS",
                    help="VOD 구간 트리밍 시작 시각 (MM:SS/HH:MM:SS/초, --vod-url 전용, --trim-duration과 함께 사용)")
    p.add_argument("--trim-duration", metavar="MM:SS",
                    help="VOD 구간 트리밍 길이 (MM:SS/HH:MM:SS/초, --trim-start와 함께 사용)")
    p.add_argument("--skip-notify", action="store_true", help="텔레그램/카카오 전송 생략")
    p.add_argument("--skip-archive", action="store_true", help="옵시디안/노션 저장 생략")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    if bool(args.trim_start) != bool(args.trim_duration):
        p.error("--trim-start와 --trim-duration은 함께 지정해야 합니다")
    if args.trim_start and not args.vod_url:
        p.error("--trim-start/--trim-duration은 --vod-url과 함께만 사용 가능합니다")
    return args


def truncate_at_host_ad(
    vision_results: list[dict], transcript: str, min_sec: int
) -> tuple[list[dict], str, int | None]:
    """진행자광고(보드형 광고 소개) 감지 시 방송 종료로 취급.

    세션 시작 min_sec 이후 첫 '진행자광고' 프레임의 timestamp를 컷오프로,
    이후의 vision 결과와 전사 라인([MM:SS] 태그 기준)을 제거한다.
    """
    cutoff: int | None = None
    for r in vision_results:
        ts = int(r.get("timestamp_sec", 0))
        if r.get("type") == "진행자광고" and ts >= min_sec:
            cutoff = ts
            break
    if cutoff is None:
        return vision_results, transcript, None

    kept_vision = [r for r in vision_results if int(r.get("timestamp_sec", 0)) < cutoff]
    kept_lines = []
    for line in transcript.split("\n"):
        if line.startswith("[") and "]" in line:
            try:
                mm, ss = line[1 : line.index("]")].split(":")
                if int(mm) * 60 + int(ss) >= cutoff:
                    break  # 이후 라인은 모두 컷오프 이후
            except ValueError:
                pass
        kept_lines.append(line)
    mm, ss = divmod(cutoff, 60)
    log.info("진행자광고 감지 [%02d:%02d] → 방송 종료 취급, 이후 내용 분석 제외", mm, ss)
    return kept_vision, "\n".join(kept_lines), cutoff


def get_video(args: argparse.Namespace, settings: dict, out_dir: Path) -> Path:
    if args.video_file:
        video = Path(args.video_file)
        if not video.exists():
            raise CaptureError(f"영상 파일 없음: {video}")
        return video
    if args.vod_url:
        start_sec = duration_sec = None
        if args.trim_start:
            start_sec = parse_duration(args.trim_start)
            duration_sec = parse_duration(args.trim_duration)
        return download_vod(
            args.vod_url, out_dir / f"{args.session}_vod.mp4",
            settings["capture"]["resolution"], start_sec, duration_sec,
        )
    return capture_live_session(settings, args.session, out_dir)


def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    session = args.session
    # 트리밍 테스트는 전체구간 결과와 겹치지 않게 별도 폴더(<session>_trim)에 저장
    out_dir = output_dir(session, tag="trim" if args.trim_start else None)
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

    session_cfg = settings["sessions"][session]

    # 2. 프레임 추출 + 자료화면 후보 선별
    #    us: all 모드 (어두운 Finviz류 데이터화면 포함) / kr: white 모드 (흰배경 중심)
    selected = prepare_frames(
        video, out_dir, settings["frames"],
        mode=session_cfg.get("frame_filter", "white"),
    )
    if not selected:
        log.warning("자료화면 후보가 0장 — 전사만으로 리포트 진행")

    # 3. Gemini 비전 분석 (분류 + 텍스트/그래프 추출; 배너 광고는 프롬프트에서 무시)
    #    무료 티어 할당량(20요청/일) 대응: 배치 크기·요청 상한·429 폴백은 settings로 제어
    #    비전 단계 실패(모델명 오류·SDK 예외 등)가 세션 전체를 죽이지 않도록 방어:
    #    전사 기반 리포트는 항상 진행 가능해야 함 (자료화면 0장 처리와 동일한 원칙)
    frames_cfg = settings["frames"]
    all_vision: list[dict] = []
    if selected:
        try:
            all_vision = analyze_frames(
                selected, settings["models"]["gemini"], out_dir,
                batch_size=frames_cfg.get("vision_batch_size", 16),
                max_requests=frames_cfg.get("vision_max_requests", 6),
                fallback_model=settings["models"].get("gemini_fallback", ""),
            )
        except Exception as e:
            log.error("Gemini 비전 분석 전체 실패(무시하고 전사만으로 진행): %s", e)

    # 4. Whisper 음성 전사 (models.whisper_disabled=true면 생략)
    #    끄면 리포트는 화면 캡처(자료화면) + 종목 시세 + 뉴스링크로만 구성된다.
    #    45분 오디오 전사에 약 12분이 걸려 런타임의 대부분을 차지했다.
    #    단 sessions.<s>.transcribe_window가 있으면 그 구간만은 전사한다 —
    #    07:50~08:05의 '오늘 시황 전망' 발언은 화면 캡처로 대체할 수 없기 때문
    #    (전사 전문은 리포트에 싣지 않고 전망 요약 근거로만 쓴다. report.py 참고)
    twin = window_offsets(session_cfg.get("start_kst", "00:00"),
                          session_cfg.get("transcribe_window"))
    if not settings["models"].get("whisper_disabled"):
        transcript = transcribe(video, out_dir, settings["models"]["whisper"])
    elif twin:
        log.info("whisper_disabled=true지만 %s 구간(%ds부터 %ds)은 시황전망용으로 전사",
                 session_cfg["transcribe_window"], *twin)
        try:
            transcript = transcribe(video, out_dir, settings["models"]["whisper"],
                                    start_sec=twin[0], dur_sec=twin[1])
        except Exception as e:
            log.error("구간 전사 실패(무시하고 화면 캡처만으로 진행): %s", e)
            transcript = ""
    else:
        log.info("whisper_disabled=true → 음성 전사 생략 (화면 캡처 기반으로 진행)")
        transcript = ""

    # 4.5 방송 종료 감지: 진행자가 보드형 광고 소개를 시작하면 이후 내용 제외
    if session_cfg.get("end_on_host_ad"):
        all_vision, transcript, _ = truncate_at_host_ad(
            all_vision, transcript, session_cfg.get("host_ad_min_sec", 1200)
        )
    # 최종 분석 대상은 자료화면만 (광고/스튜디오/진행자광고 제외)
    vision_results = [r for r in all_vision if r.get("type") == "자료화면"]
    # 이 채널은 시황에서 ETF를 다루지 않는다 — 화면의 ETF는 전부 협찬·광고이므로 제거
    n_etf = drop_etf_stocks(vision_results)
    if n_etf:
        log.info("ETF 종목 %d건 제거 (이 채널은 ETF를 광고로 취급)", n_etf)
    log.info("최종 분석 자료화면: %d장", len(vision_results))

    # 5. 종목 추출 → 실시세 검증
    mentions = extract_mentions(settings["models"], vision_results, transcript)
    verified = market.verify_mentions(mentions)
    indices = market.fetch_indices(settings["market"]["indices"])
    holdings_data = load_holdings()
    holdings_quotes = market.fetch_holdings_quotes(
        holdings_data["holdings"] + holdings_data["watchlist"]
    )

    # 5.5 뉴스 브리핑용 기사 수집 (네이버 검색 API — 무료 25,000건/일)
    #     언급 종목 + 보유종목 이름으로 검색해 중복 제거 후 리포트에 브리핑으로 싣는다
    #     대상 우선순위: ① 캡처 화면에 뜬 미장 종목(사용자 확정 기준) ② 언급 종목 ③ 보유종목
    briefing_names = us_stocks_in_captures(vision_results)
    briefing_names += [
        v["name"] for v in verified
        if v.get("name") and v["name"] not in briefing_names
    ][: max(0, 8 - len(briefing_names))]
    briefing_names += [
        h["name"] for h in holdings_data["holdings"]
        if h.get("name") and h["name"] not in briefing_names
    ][:4]
    news_cfg = settings.get("news", {})
    news_briefing = news.collect_briefing(
        briefing_names,
        per_query=news_cfg.get("per_query", 4),
        limit=news_cfg.get("briefing_limit", 20),
        recency_hours=news_cfg.get("recency_hours"),
        max_queries=news_cfg.get("max_queries"),
    )

    # 5.6 리포트 6번 섹션용 수급 데이터 (전일 수급주체·순매수 top10·ETF)
    flows = market.fetch_flows(settings.get("flows"))

    # kr 세션은 오늘 아침 us 리포트를 컨텍스트로 사용
    us_context = ""
    us_context_problem = ""
    if session == "kr" and not args.skip_archive:
        us_context, reason = read_us_section_today(settings["obsidian"])
        if reason and reason != DISABLED:
            log.warning("us 컨텍스트 없이 kr 리포트를 만듭니다: %s", reason)
            us_context_problem = reason

    # 6. Claude 최종 리포트
    report = generate_report(
        settings, session, vision_results, transcript,
        indices, verified, holdings_data, holdings_quotes,
        out_dir, us_context_md=us_context, news_briefing=news_briefing,
        flows=flows,
    )

    # 7. 아카이브 (옵시디안 볼트 → 탭S9 n8n → Syncthing → S26)
    #    전송보다 **먼저** 한다 — 저장이 실패했는데 「옵시디안에서 열기」 딥링크를
    #    붙여 보내면 눌러도 빈 검색 결과가 뜬다(2026-07-27 실제 증상).
    reports = report.get("reports") or {"sihwang": report["telegram_text"], "news": ""}
    if args.skip_archive:
        archived = ArchiveResult(ok=False, skipped=True, reason=DISABLED)
    else:
        archived = archive_report(
            settings["obsidian"], session, report["title_keyword"],
            tg_format.to_obsidian(reports.get("sihwang_md")
                                  or reports.get("sihwang")
                                  or report["markdown_report"]),
            indices, report.get("holdings_mentioned", []),
            news_markdown=tg_format.to_obsidian(reports.get("news", "")),
        )
        if archived.ok and settings.get("notion", {}).get("enabled"):
            from .notion_archive import archive_to_notion
            archive_to_notion(report["title_keyword"], report["markdown_report"])

    # 8. 전송 (텔레그램 → 카카오, best-effort)
    #    세션당 2건 — ① 시황 ② 종목 기사검색. 한 건에 다 담으면 기사 목록이
    #    본문을 잠식해 읽히지 않았다(2026-07-27 실물 스크린샷 지적).
    base = f"📌 {settings['report']['title_prefix']}_{now_kst():%Y%m%d}_{report['title_keyword']}"
    deeplink = obsidian_deeplink(settings.get("obsidian", {}))
    parts = [
        (f"{base} [{label}]", reports.get("sihwang", "")),
        (f"{base} [{label} · 종목기사]", reports.get("news", "")),
    ]
    messages = [f"**{head}**\n\n{body}" for head, body in parts if body.strip()]
    if messages and deeplink and archived.ok:
        messages[-1] += f"\n\n🗂 [옵시디안에서 열기]({deeplink})"

    if args.skip_notify:
        log.info("--skip-notify: 전송 생략 (결과는 %s 에 저장됨)", out_dir)
    else:
        max_len = settings["telegram"]["max_message_len"]
        for md in messages:
            # HTML로 보내야 링크가 제목만 보이고 접기 블록이 눌러서 펼쳐진다
            send_telegram(tg_format.to_telegram_html(md), max_len, html=True)
        if settings.get("kakao", {}).get("enabled", True):
            # 카카오는 HTML/접기를 지원하지 않으므로 접기 마커만 걷어낸 평문으로
            send_kakao_memo(tg_format.to_plain("\n\n".join(messages)))

    # 9. 볼트 저장 실패는 조용히 넘기지 않는다 — 리포트는 이미 나갔지만 옵시디안엔
    #    아무것도 안 올라간 상태다. 텔레그램 경고 + 종료코드 1로 Actions를 빨갛게 만든다.
    #    (GH_PAT 만료로 8일간 침묵 실패했던 2026-07-27 사고 재발 방지)
    if not archived.ok and not archived.skipped:
        alert = _vault_alert(label, archived.reason, us_context_problem)
        log.error("볼트 저장 실패: %s", archived.reason)
        if args.skip_notify:
            log.error("--skip-notify: 경고 전송 생략 — 내용:\n%s", alert)
        else:
            send_alert(alert)
        return 1

    log.info("=== %s 세션 완료 ===", label)
    return 0


def _vault_alert(label: str, reason: str, us_context_problem: str = "") -> str:
    """볼트 저장 실패 경고 — 무엇이 되고 무엇이 안 됐는지부터 알린다."""
    lines = [
        f"⚠️ 3tv {label} 볼트 저장 실패 ({now_kst():%m/%d %H:%M})",
        f"사유: {reason}",
        "",
        "· 텔레그램/카카오 리포트는 정상 발송됐습니다.",
        "· 옵시디안(탭S9·S26)에는 오늘 리포트가 올라가지 않습니다.",
    ]
    if us_context_problem:
        lines.append(f"· kr 리포트가 미장 컨텍스트 없이 생성됨: {us_context_problem}")
    lines += [
        "",
        "조치: GH_PAT 재발급 → 3tv Actions의 vault-check 워크플로 수동 실행으로 확인",
    ]
    return "\n".join(lines)


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
