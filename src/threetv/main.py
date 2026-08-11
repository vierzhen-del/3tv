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
import copy
import json
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from . import market, news, tg_format
from .capture import CaptureError, capture_live_session, download_vod, find_recent_vod
from .common import (KST, load_env, load_holdings, load_settings, log, now_kst,
                     output_dir, parse_duration, parse_kst_time,
                     setup_logging, window_offsets)
from .frames import prepare_frames
from .notify_kakao import send_kakao_memo
from .notify_telegram import send_alert, send_telegram
from .obsidian_archive import (DISABLED, ArchiveResult, archive_report,
                               archive_simple_report, read_night_slots,
                               read_us_section_today, remediation, save_night_slot,
                               vault_location_link, vault_location_url)
from .report import (drop_etf_stocks, extract_mentions, generate_etf_review,
                     generate_night_digest,
                     generate_noon_report, generate_report,
                     us_stocks_in_captures)
from .transcribe import transcribe
from .vision import analyze_frames


def report_footer(settings: dict, archived: ArchiveResult) -> str:
    """텔레그램 본문 맨 아래 공통 꼬리말 — 저장위치 + 생성시각.

    세션마다(us/kr·noon·night·etf) 따로 조립하던 「옵시디안에서 열기」 줄을 하나로 모았다.
    딥링크는 탭S9에서 열리지 않아 저장위치 링크로 바꿨다(vault_location_link 참조).
    생성시각은 리포트가 언제 만들어진 것인지 나중에 되짚기 위해 항상 붙인다 —
    제목의 날짜만으로는 같은 날 여러 세션 중 어느 시점 결과인지 알 수 없다.
    """
    lines = []
    if archived.ok:
        location = vault_location_link(settings.get("obsidian", {}), archived.rel)
        if location:
            lines.append(location)
    lines.append(f"🕘 생성 {now_kst():%Y-%m-%d %H:%M} KST")
    return "\n\n" + "\n".join(lines)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="삼프로TV 라이브 분석 → 데일리 시황 리포트")
    p.add_argument("--session", required=True,
                   choices=["us", "kr", "noon", "night", "etf"])
    p.add_argument("--vod-url", help="라이브 대신 VOD URL로 실행 (테스트/복구)")
    p.add_argument("--video-file", help="이미 받은 영상 파일로 실행 (캡처 생략)")
    p.add_argument("--trim-start", metavar="MM:SS",
                    help="VOD 구간 트리밍 시작 시각 (MM:SS/HH:MM:SS/초, --vod-url 전용, --trim-duration과 함께 사용)")
    p.add_argument("--trim-duration", metavar="MM:SS",
                    help="VOD 구간 트리밍 길이 (MM:SS/HH:MM:SS/초, --trim-start와 함께 사용)")
    p.add_argument("--skip-notify", action="store_true", help="텔레그램/카카오 전송 생략")
    p.add_argument("--skip-archive", action="store_true", help="옵시디안/노션 저장 생략")
    p.add_argument("--slot", metavar="HH:MM",
                    help="--session night 전용: 이 정시만 5분 캡처해 볼트에 저장 (예: 22:00)")
    p.add_argument("--digest", action="store_true",
                    help="--session night 전용: 오늘 저장된 슬롯을 모아 종합 리포트 발행")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    if bool(args.trim_start) != bool(args.trim_duration):
        p.error("--trim-start와 --trim-duration은 함께 지정해야 합니다")
    if args.trim_start and not args.vod_url:
        p.error("--trim-start/--trim-duration은 --vod-url과 함께만 사용 가능합니다")
    if args.session == "night":
        if bool(args.slot) == bool(args.digest):
            p.error("--session night는 --slot HH:MM 또는 --digest 중 정확히 하나가 필요합니다")
    elif args.slot or args.digest:
        p.error("--slot/--digest는 --session night 전용입니다")
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


def night_session_date(hour: int, base: datetime | None = None) -> str:
    """22~23시 슬롯을 00~05시 슬롯과 같은 '야간 세션 날짜'로 묶는다.

    슬롯은 각자 자기 실행 시각의 now_kst()를 쓰므로, 22/23시 슬롯은 아직
    전날 날짜다. 하지만 06시 종합(digest)이 실행되는 날짜(=다음날 아침, 리포트가
    "오늘"이라 부르는 날짜)로 맞춰야 8개 슬롯 + digest가 같은 폴더에서 만난다.
    hour>=12(=22,23시)면 +1일, hour<12(=00~05시, digest의 06시 포함)면 그대로.
    """
    base = base or now_kst()
    if hour >= 12:
        base = base + timedelta(days=1)
    return base.strftime("%Y%m%d")


def _slot_nominal_dt(slot_hhmm: str, now: datetime) -> datetime:
    """슬롯 이름(HH:MM)이 지금(now) 기준 실제로 어제였는지 오늘이었는지 판단.

    22·23시 슬롯은 cron 지연이 자정을 넘기면 now의 날짜가 이미 다음날이라,
    슬롯 시각은 "어제" 날짜로 되돌려야 한다 — `night_session_date()`가 슬롯
    저장 시 쓰는 것과 같은 hour>=12 규칙(2026-07-28)을 여기서도 그대로 쓴다.
    """
    h, m = (int(x) for x in slot_hhmm.split(":"))
    base = now
    if h >= 12 and now.hour < 12:
        base = now - timedelta(days=1)
    return base.replace(hour=h, minute=m, second=0, microsecond=0)


def _broadcast_end_kst(session_cfg: dict, slot_nominal: datetime) -> datetime:
    """이 슬롯이 속한 밤 방송이 끝나는 시각 — slot_nominal 기준으로 다음에 오는
    06시(기본값, `digest_kst`와 동일. 필요하면 `broadcast_end_kst`로 따로 지정)."""
    end_hhmm = session_cfg.get("broadcast_end_kst") or session_cfg.get("digest_kst", "06:00")
    end = parse_kst_time(end_hhmm, slot_nominal)
    if end <= slot_nominal:
        end += timedelta(days=1)
    return end


def _slot_settings(settings: dict, slot_hhmm: str, duration_min: int,
                   now: datetime | None = None) -> dict:
    """night 세션 설정을 슬롯 시각(HH:MM)에 맞춰 캡처 시작·종료로 오버라이드한 사본.

    ⚠️ cron 지연 내성(2026-08-01) — GitHub 무료 티어 cron은 최대 3시간38분까지
    밀린다. 슬롯 정시가 이미 지났는데도 "정시부터 5분"으로 고정하면
    `record_stream()`이 즉시 "녹화 종료 시각이 이미 지남"으로 실패한다
    (night-slot 8개가 실전에서 전부 이렇게 실패했다). 슬롯이 이미 지났으면
    "정시부터"가 아니라 "지금부터 duration_min"으로 캡처한다 — 방송이
    22:00~06:00 연속이라 몇십 분 밀려도 "시간당 샘플" 목적엔 지장이 없다.
    방송 자체가 끝났는지(=지금 >= 방송 종료)는 `run_night_slot()`이 이 함수를
    부르기 전에 먼저 걸러 정상 종료(exit 0)시킨다.
    """
    now = now or now_kst()
    nominal = _slot_nominal_dt(slot_hhmm, now)
    session_cfg = settings["sessions"]["night"]
    broadcast_end = _broadcast_end_kst(session_cfg, nominal)

    if now >= nominal:
        start_dt = now
        end_dt = min(now + timedelta(minutes=duration_min), broadcast_end)
    else:
        start_dt = nominal
        end_dt = nominal + timedelta(minutes=duration_min)

    s = copy.deepcopy(settings)
    ncfg = s["sessions"]["night"]
    ncfg["poll_from_kst"] = start_dt.strftime("%H:%M")
    ncfg["start_kst"] = start_dt.strftime("%H:%M")
    ncfg["end_kst"] = end_dt.strftime("%H:%M")
    return s


def _vod_fallback(settings: dict, session: str, out_dir: Path) -> Path:
    """라이브 창을 놓쳤을 때(cron 지연·라이브 미시작) 오늘자 다시보기에서 같은
    구간을 잘라 받는다.

    noon 세션에서 먼저 확인된 패턴(2026-08-01, "VOD 폴백 신설")을 us/kr에도
    적용한다(2026-08-09) — kr 08/07 캡처가 라이브 미시작으로 실패해 사람이
    매번 `--vod-url`로 복구해야 했다. cron 지연·라이브 미시작 등 원인과
    무관하게 항상 끝나는 다시보기 경로로 매일 발행을 보장한다.

    release_timestamp(실제 방송 시작)를 구하면 세션 시작 시각까지의 오프셋을
    정확히 계산하고, 못 구하면 "방송이 정시 시작"이라는 전제로 오프셋 0에
    여유(25분)를 더해 받는다.
    """
    session_cfg = settings["sessions"][session]
    live_url = session_cfg.get("live_url") or settings["channel"]["live_url"]
    video_url, release_ts = find_recent_vod(live_url)
    target = parse_kst_time(session_cfg.get("start_kst", "00:00"))

    if release_ts is not None:
        release_dt = datetime.fromtimestamp(release_ts, tz=KST)
        offset_sec = max(0, int((target - release_dt).total_seconds()))
        end = parse_kst_time(session_cfg.get("end_kst") or session_cfg.get("start_kst", "00:00"))
        duration_sec = max(60, int((end - target).total_seconds()))
    else:
        log.warning("다시보기 시작 시각을 확인 못함 — 세션 정각 시작으로 가정(오프셋 0, 25분 확보)")
        offset_sec = 0
        duration_sec = 25 * 60

    log.info("다시보기 폴백: %s (오프셋 %d초, %d초 분량)", video_url, offset_sec, duration_sec)
    return download_vod(video_url, out_dir / f"{session}_vod.mp4",
                        settings["capture"]["resolution"], offset_sec, duration_sec)


def run_noon(args: argparse.Namespace, settings: dict, out_dir: Path) -> int:
    """겸손은힘들다 "12시에 만나요" — 12:00~12:10 시황 요약 + 장중 KR 지수.

    us/kr과 달리 종목기사 검색·보유종목 체크가 없다(사용자 확정). 캡처·프레임·
    비전·전사는 기존 파이프라인 그대로 재사용하고, 리포트만 가벼운 전용
    generate_noon_report()로 만든다.

    라이브 창을 놓치면(cron 지연 등) 다시보기로 자동 전환한다(2026-08-01) —
    `used_vod_fallback`이 참이면 헤더에 "(다시보기 기준)"을 붙여 출처를 밝힌다.
    """
    session_cfg = settings["sessions"]["noon"]
    label = session_cfg["label"]
    log.info("=== 3tv %s 세션 시작 (%s) ===", label, now_kst().strftime("%Y-%m-%d %H:%M"))

    used_vod_fallback = False
    try:
        if args.vod_url or args.video_file:
            video = get_video(args, settings, out_dir)   # 수동 테스트/복구 — 기존 경로 그대로
        else:
            from .common import session_window
            _, _, end = session_window(settings, "noon")
            if now_kst() < end:
                video = get_video(args, settings, out_dir)
            else:
                log.warning("라이브 창(~%s KST)을 이미 지나 다시보기로 전환", end.strftime("%H:%M"))
                video = _vod_fallback(settings, "noon", out_dir)
                used_vod_fallback = True
    except CaptureError as e:
        log.error("캡처 실패(라이브+다시보기 모두): %s", e)
        if not args.skip_notify:
            send_alert(f"⚠️ 3tv {label} 캡처 실패 ({now_kst():%m/%d %H:%M})\n{e}")
        return 1

    selected = prepare_frames(video, out_dir, settings["frames"],
                              mode=session_cfg.get("frame_filter", "white"))
    all_vision: list[dict] = []
    if selected:
        try:
            all_vision = analyze_frames(
                selected, settings["models"]["gemini"], out_dir,
                batch_size=settings["frames"].get("vision_batch_size", 16),
                max_requests=settings["frames"].get("vision_max_requests", 6),
                fallback_model=settings["models"].get("gemini_fallback", ""),
            )
        except Exception as e:
            log.error("Gemini 비전 분석 실패(전사만으로 진행): %s", e)
    vision_results = [r for r in all_vision if r.get("type") == "자료화면"]
    drop_etf_stocks(vision_results)

    transcript = ""
    twin = window_offsets(session_cfg.get("start_kst", "12:00"), session_cfg.get("transcribe_window"))
    if twin:
        try:
            transcript = transcribe(video, out_dir, settings["models"]["whisper"],
                                    start_sec=twin[0], dur_sec=twin[1])
        except Exception as e:
            log.error("전사 실패(화면 캡처만으로 진행): %s", e)

    kr_indices = [q for q in market.fetch_indices(settings["market"]["indices"])
                 if q.get("market") == "KR"]

    report = generate_noon_report(settings, vision_results, transcript, kr_indices, out_dir)

    if args.skip_archive:
        archived = ArchiveResult(ok=False, skipped=True, reason=DISABLED)
    else:
        archived = archive_simple_report(
            settings["obsidian"], "3protv정오", report["title_keyword"],
            tg_format.to_obsidian(report["markdown_report"]),
        )

    label_suffix = f"{label}" if not used_vod_fallback else f"{label} · 다시보기 기준"
    header = f"📌 {settings['report']['title_prefix']}_{now_kst():%Y%m%d}_{report['title_keyword']} [{label_suffix}]"
    body = f"**{header}**\n\n{report['telegram_text']}"
    body += report_footer(settings, archived)
    if args.skip_notify:
        log.info("--skip-notify: 전송 생략 (결과는 %s 에 저장됨)", out_dir)
    else:
        send_telegram(tg_format.to_telegram_html(body), settings["telegram"]["max_message_len"], html=True)
        if settings.get("kakao", {}).get("enabled", True):
            send_kakao_memo(tg_format.to_plain(body),
                            vault_location_url(settings.get("obsidian", {}), archived.rel))

    if not archived.ok and not archived.skipped:
        log.error("볼트 저장 실패: %s", archived.reason)
        if not args.skip_notify:
            send_alert(_vault_alert(label, archived.reason))
        return 1

    log.info("=== %s 세션 완료 ===", label)
    return 0


def run_night_slot(args: argparse.Namespace, settings: dict, out_dir: Path) -> int:
    """야간 미장 라이브 슬롯 1회분 — 5분 캡처 + 비전 분석 → 볼트에 저장만 하고 끝난다.

    리포트도, 텔레그램 발송도 여기선 없다. 8개 슬롯이 각자 독립 job으로 실행되며,
    06시 종합(run_night_digest)이 이 결과들을 모아 리포트 하나로 합친다.
    슬롯 하나가 실패해도(방송 미시작 등) 나머지 슬롯·종합은 영향받지 않는다 —
    그래서 실패해도 텔레그램 경고를 보내지 않는다(8번의 소음을 피함).
    """
    session_cfg = settings["sessions"]["night"]
    duration_min = session_cfg.get("slot_duration_min", 5)
    hour_label = args.slot.split(":")[0]
    now = now_kst()

    # 실전 라이브 경로에서만 "방송 이미 끝남"을 확인한다 — --vod-url/--video-file
    # 테스트·복구 실행은 지금이 몇 시든 그대로 진행돼야 한다.
    if not args.vod_url and not args.video_file:
        nominal = _slot_nominal_dt(args.slot, now)
        broadcast_end = _broadcast_end_kst(session_cfg, nominal)
        if now >= broadcast_end:
            log.info("야간 슬롯(%s) 스킵: 방송 종료(%s KST) 이후 실행돼 캡처하지 않음 "
                     "(지금 %s KST) — 실패 아님, 정상 종료",
                     args.slot, broadcast_end.strftime("%H:%M"), now.strftime("%H:%M"))
            return 0

    slot_settings = _slot_settings(settings, args.slot, duration_min, now)
    log.info("=== 3tv 야간 슬롯 %s 시작 (%s) ===", args.slot, now.strftime("%Y-%m-%d %H:%M"))

    try:
        video = get_video(args, slot_settings, out_dir)
    except CaptureError as e:
        log.warning("야간 슬롯(%s) 캡처 실패(다른 슬롯엔 영향 없음): %s", args.slot, e)
        return 1

    selected = prepare_frames(video, out_dir, settings["frames"],
                              mode=session_cfg.get("frame_filter", "all"))
    vision_results: list[dict] = []
    if selected:
        try:
            all_vision = analyze_frames(
                selected, session_cfg.get("vision_model") or settings["models"]["gemini"], out_dir,
                batch_size=settings["frames"].get("vision_batch_size", 16),
                max_requests=settings["frames"].get("vision_max_requests", 6),
                fallback_model="",  # 이미 별도(무료) 버킷이라 추가 폴백 불필요
            )
            vision_results = [r for r in all_vision if r.get("type") == "자료화면"]
            drop_etf_stocks(vision_results)
        except Exception as e:
            log.error("야간 슬롯(%s) 비전 분석 실패: %s", args.slot, e)

    if args.skip_archive:
        log.info("--skip-archive: 슬롯 저장 생략 (결과는 %s 에 저장됨)", out_dir)
        return 0

    date_ymd = night_session_date(int(hour_label))
    payload = {"vision_results": vision_results, "timestamp_kst": now_kst().isoformat()}
    result = save_night_slot(settings["obsidian"], date_ymd, hour_label, payload)
    if not result.ok and not result.skipped:
        log.error("야간 슬롯(%s) 저장 실패: %s", args.slot, result.reason)
        return 1
    log.info("=== 야간 슬롯 %s 완료 ===", args.slot)
    return 0


def run_night_digest(args: argparse.Namespace, settings: dict, out_dir: Path) -> int:
    """06시 — 오늘 밤 저장된 슬롯 8개를 모아 종합 리포트 1건을 발행한다."""
    session_cfg = settings["sessions"]["night"]
    label = session_cfg["label"]
    n_slots = len(session_cfg.get("slots_kst", [])) or 8
    date_ymd = night_session_date(6)
    log.info("=== 3tv %s 종합 시작 (%s) ===", label, now_kst().strftime("%Y-%m-%d %H:%M"))

    slots, reason = read_night_slots(settings["obsidian"], date_ymd)
    if not slots:
        log.error("야간 슬롯을 하나도 읽지 못함: %s", reason)
        if not args.skip_notify:
            send_alert(
                f"⚠️ 3tv {label} 종합 실패 ({now_kst():%m/%d %H:%M})\n사유: {reason}\n"
                f"조치: {remediation(reason)}"
            )
        return 1
    if len(slots) < n_slots:
        log.warning("야간 슬롯 %d/%d개만 수집됨(일부 슬롯 job 실패 추정) — 있는 만큼으로 종합",
                   len(slots), n_slots)

    report = generate_night_digest(settings, slots, out_dir)

    if args.skip_archive:
        archived = ArchiveResult(ok=False, skipped=True, reason=DISABLED)
    else:
        archived = archive_simple_report(
            settings["obsidian"], "3protv야간", report["title_keyword"],
            tg_format.to_obsidian(report["markdown_report"]),
        )

    header = f"📌 {settings['report']['title_prefix']}_{now_kst():%Y%m%d}_{report['title_keyword']} [{label}]"
    body = f"**{header}**\n\n{report['telegram_text']}"
    if len(slots) < n_slots:
        body += f"\n\n⚠️ 슬롯 {len(slots)}/{n_slots}개만 수집됨 — 일부 시간대 캡처가 누락됐을 수 있습니다."
    body += report_footer(settings, archived)

    if args.skip_notify:
        log.info("--skip-notify: 전송 생략 (결과는 %s 에 저장됨)", out_dir)
    else:
        send_telegram(tg_format.to_telegram_html(body), settings["telegram"]["max_message_len"], html=True)
        if settings.get("kakao", {}).get("enabled", True):
            send_kakao_memo(tg_format.to_plain(body),
                            vault_location_url(settings.get("obsidian", {}), archived.rel))

    if not archived.ok and not archived.skipped:
        log.error("볼트 저장 실패: %s", archived.reason)
        if not args.skip_notify:
            send_alert(_vault_alert(label, archived.reason))
        return 1

    log.info("=== %s 종합 완료 ===", label)
    return 0


def run_etf_review(args: argparse.Namespace, settings: dict, out_dir: Path) -> int:
    """ETF 포트폴리오 리뷰 — KRX 공시 PDF의 전일 대비 구성 변화를 1건 발송.

    방송과 무관한 순수 데이터 리포트라 캡처·비전·전사·LLM을 전부 타지 않는다
    (Gemini 요청 0건). 그래서 kr 세션에 얹지 않고 독립 실행한다 — kr이 캡처
    단계에서 실패해도 이 리포트는 정상 발행된다.
    """
    cfg = settings.get("etf_review") or {}
    targets = cfg.get("targets") or []
    if not targets:
        log.error("etf_review.targets가 비어 있습니다 — 설정을 확인하세요")
        return 1
    log.info("=== 3tv ETF 포트폴리오 리뷰 시작 (%s) ===",
             now_kst().strftime("%Y-%m-%d %H:%M"))

    today_ymd = now_kst().strftime("%Y%m%d")
    results, failed = [], []
    for t in targets:
        ticker, name = str(t["ticker"]), t.get("name") or str(t["ticker"])
        today, prev, prev_date = market.etf_pdf_with_prev(ticker, today_ymd)
        if not today:
            log.warning("ETF PDF 조회 실패(건너뜀): %s %s", name, ticker)
            failed.append(name)
            continue
        if not prev:
            log.warning("직전 영업일 PDF 없음 — 비교 생략: %s", name)
            failed.append(f"{name}(비교불가)")
            continue
        results.append({"name": name, "ticker": ticker, "prev_date": prev_date,
                        "diff": market.etf_pdf_diff(today, prev,
                                                    top=int(cfg.get("top", 5)))})

    if not results:
        log.error("ETF PDF를 하나도 읽지 못했습니다 (KRX 공시 지연 또는 pykrx 실패)")
        if not args.skip_notify:
            send_alert(f"⚠️ 3tv ETF 리뷰 실패 ({now_kst():%m/%d %H:%M})\n"
                       f"KRX PDF 조회 0건 — 대상: {', '.join(failed) or '없음'}")
        return 1

    report = generate_etf_review(settings, results)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(report["markdown_report"], encoding="utf-8")
    (out_dir / "report.json").write_text(
        json.dumps({**report, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    body = report["telegram_text"]
    if failed:
        body += f"\n\n⚠️ 조회 실패: {', '.join(failed)}"

    if args.skip_archive:
        archived = ArchiveResult(ok=False, skipped=True, reason=DISABLED)
    else:
        archived = archive_simple_report(
            settings["obsidian"], "3protvETF", report["title_keyword"],
            tg_format.to_obsidian(report["markdown_report"]))
    body += report_footer(settings, archived)

    if args.skip_notify:
        log.info("--skip-notify: 전송 생략 (결과는 %s 에 저장됨)", out_dir)
    else:
        # generate_etf_review()는 다른 세션과 동일하게 마크다운(**굵게**)으로 본문을
        # 만든다(2026-08-08 수정 전엔 <b> 태그를 직접 박아넣어서, 여기서 to_telegram_html()이
        # 그 태그를 이스케이프해 텔레그램 화면에 <b> 글자가 그대로 노출됐다).
        send_telegram(tg_format.to_telegram_html(body),
                      settings["telegram"]["max_message_len"], html=True)
        if settings.get("kakao", {}).get("enabled", True):
            send_kakao_memo(tg_format.to_plain(body),
                            vault_location_url(settings.get("obsidian", {}), archived.rel))

    if not archived.ok and not archived.skipped:
        log.error("볼트 저장 실패: %s", archived.reason)
        if not args.skip_notify:
            send_alert(_vault_alert("ETF 리뷰", archived.reason))
        return 1

    log.info("=== ETF 포트폴리오 리뷰 완료 (%d종) ===", len(results))
    return 0


def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    session = args.session
    # 트리밍 테스트는 전체구간 결과와 겹치지 않게 별도 폴더(<session>_trim)에 저장
    out_dir = output_dir(session, tag="trim" if args.trim_start else None)

    # noon/night은 us/kr과 리포트 구조·아카이브 방식이 달라 전용 경로로 분기한다
    # (noon: 종목기사 없는 가벼운 리포트 / night: 슬롯 캡처+저장만 또는 8슬롯 종합)
    if session == "noon":
        return run_noon(args, settings, out_dir)
    if session == "night":
        if args.digest:
            return run_night_digest(args, settings, out_dir)
        return run_night_slot(args, settings, out_dir)
    if session == "etf":
        return run_etf_review(args, settings, out_dir)

    label = settings["sessions"][session]["label"]
    log.info("=== 3tv %s 세션 시작 (%s) ===", label, now_kst().strftime("%Y-%m-%d %H:%M"))

    # 1. 캡처 (라이브 / VOD / 로컬 파일) — 라이브 창을 이미 지났거나(cron 지연)
    #    라이브 캡처 자체가 실패하면(스트림 미시작 등) 다시보기로 자동 전환한다
    #    (noon에서 먼저 검증된 패턴, 2026-08-09 kr 08/07 캡처 실패로 us/kr에도 적용).
    #    --vod-url/--video-file 수동 실행은 그대로 기존 경로를 쓴다(폴백 대상 아님).
    used_vod_fallback = False
    try:
        if args.vod_url or args.video_file:
            video = get_video(args, settings, out_dir)
        else:
            from .common import session_window
            _, _, end = session_window(settings, session)
            if now_kst() < end:
                try:
                    video = get_video(args, settings, out_dir)
                except CaptureError as e:
                    log.warning("라이브 캡처 실패 — 다시보기로 전환: %s", e)
                    video = _vod_fallback(settings, session, out_dir)
                    used_vod_fallback = True
            else:
                log.warning("라이브 창(~%s KST)을 이미 지나 다시보기로 전환", end.strftime("%H:%M"))
                video = _vod_fallback(settings, session, out_dir)
                used_vod_fallback = True
    except CaptureError as e:
        log.error("캡처 실패(라이브+다시보기 모두): %s", e)
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
    label_suffix = f"{label}" if not used_vod_fallback else f"{label} · 다시보기 기준"
    base = f"📌 {settings['report']['title_prefix']}_{now_kst():%Y%m%d}_{report['title_keyword']}"
    # 텔레그램은 접힌 버전(news_telegram)을 쓰고, 옵시디안 아카이브(위 news_markdown)는
    # 접지 않은 news를 쓴다 — 저장본은 나중에 컨텍스트로 재사용될 때 전종목이
    # 검색돼야 하기 때문(2026-08-09 확정).
    parts = [
        (f"{base} [{label_suffix}]", reports.get("sihwang", "")),
        (f"{base} [{label_suffix} · 종목기사]",
         reports.get("news_telegram") or reports.get("news", "")),
    ]
    messages = [f"**{head}**\n\n{body}" for head, body in parts if body.strip()]
    if messages:
        messages[-1] += report_footer(settings, archived)

    if args.skip_notify:
        log.info("--skip-notify: 전송 생략 (결과는 %s 에 저장됨)", out_dir)
    else:
        max_len = settings["telegram"]["max_message_len"]
        for md in messages:
            # HTML로 보내야 링크가 제목만 보이고 접기 블록이 눌러서 펼쳐진다
            send_telegram(tg_format.to_telegram_html(md), max_len, html=True)
        if settings.get("kakao", {}).get("enabled", True):
            # 카카오는 HTML/접기를 지원하지 않으므로 접기 마커만 걷어낸 평문으로
            send_kakao_memo(tg_format.to_plain("\n\n".join(messages)),
                            vault_location_url(settings.get("obsidian", {}), archived.rel))

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
    lines += ["", f"조치: {remediation(reason)}"]
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
