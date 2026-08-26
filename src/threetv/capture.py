"""유튜브 라이브 탐지 + 구간 녹화 / VOD 다운로드.

라이브: @3protv/live 를 폴링해 라이브가 시작되면 HLS 스트림을 ffmpeg로
세션 종료 시각(KST)까지 녹화한다.
VOD: 테스트 및 캡처 실패 복구용 — 영상 URL을 직접 지정해 다운로드한다.

유튜브가 데이터센터 IP(yt-dlp)를 차단하는 경우가 있어 YOUTUBE_COOKIES
(cookies.txt 내용)를 환경변수로 받으면 yt-dlp에 주입한다.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from .common import env, log, now_kst


class CaptureError(RuntimeError):
    pass


def _cookies_file() -> Path | None:
    """YOUTUBE_COOKIES 환경변수(cookies.txt 내용)를 임시 파일로 저장."""
    content = env("YOUTUBE_COOKIES")
    if not content:
        return None
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".cookies", delete=False, encoding="utf-8"
    )
    f.write(content)
    f.close()
    return Path(f.name)


def _ytdlp_base(cookies: Path | None) -> list[str]:
    cmd = ["yt-dlp", "--no-warnings", "--quiet"]
    # YouTube의 "n challenge"(안티봇 서명 챌린지)를 풀 solver 스크립트를 GitHub에서
    # 받아오도록 허용 — 이게 없으면 JS 런타임(Deno)이 있어도 storyboard 썸네일
    # 포맷만 노출되고 실제 video/audio 포맷은 전부 숨겨져 다운로드가 실패한다
    # (CI 환경엔 Deno 설치가 별도로 필요 — 워크플로의 "Install Deno" 스텝 참고)
    cmd += ["--remote-components", "ejs:github"]
    if cookies:
        cmd += ["--cookies", str(cookies)]
    return cmd


def get_stream_url(page_url: str, resolution: int, cookies: Path | None) -> str | None:
    """라이브/영상 페이지에서 직접 재생 가능한 스트림 URL을 얻는다. 라이브가 아니면 None."""
    cmd = _ytdlp_base(cookies) + [
        "-g",
        "-f", f"best[height<={resolution}]/best",
        page_url,
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=True
        ).stdout.strip()
        return out.splitlines()[0] if out else None
    except subprocess.CalledProcessError as e:
        log.debug("yt-dlp 스트림 URL 획득 실패: %s", (e.stderr or "")[-500:])
        return None
    except subprocess.TimeoutExpired:
        return None


def wait_for_live(
    live_url: str,
    resolution: int,
    deadline: datetime,
    poll_interval: int,
    cookies: Path | None,
) -> str:
    """라이브가 시작될 때까지 폴링. deadline(KST)까지 시작 안 되면 실패."""
    last_err = ""
    while now_kst() < deadline:
        url = get_stream_url(live_url, resolution, cookies)
        if url:
            log.info("라이브 스트림 감지됨")
            return url
        last_err = "라이브 미시작 또는 yt-dlp 접근 차단"
        log.info("라이브 대기 중... (%s)", now_kst().strftime("%H:%M:%S"))
        time.sleep(poll_interval)
    raise CaptureError(f"라이브 스트림을 찾지 못함 (마감 {deadline:%H:%M} KST): {last_err}")


def record_stream(stream_url: str, out_file: Path, until_kst: datetime) -> Path:
    """HLS 스트림을 until_kst(KST)까지 녹화."""
    duration = int((until_kst - now_kst()).total_seconds())
    if duration <= 0:
        raise CaptureError("녹화 종료 시각이 이미 지남")
    log.info("녹화 시작: %d초 (%s KST까지)", duration, until_kst.strftime("%H:%M"))
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", stream_url,
        "-t", str(duration),
        "-c", "copy",
        str(out_file),
    ]
    # 라이브 스트림 중단 등에 대비해 여유 타임아웃
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=duration + 300)
    if not out_file.exists() or out_file.stat().st_size < 1_000_000:
        raise CaptureError(f"녹화 파일이 비정상적으로 작음: {(proc.stderr or '')[-500:]}")
    log.info("녹화 완료: %s (%.1f MB)", out_file.name, out_file.stat().st_size / 1e6)
    return out_file


_VOD_DOWNLOAD_TIMEOUT = 1800   # 초 — 트리밍 실패 시 아래에서 같은 값으로 1회 더 씀


def download_vod(
    vod_url: str,
    out_file: Path,
    resolution: int,
    start_sec: int | None = None,
    duration_sec: int | None = None,
) -> Path:
    """VOD 다운로드 (테스트/복구 경로).

    start_sec/duration_sec을 함께 주면 yt-dlp --download-sections로 해당
    구간만 정확히 잘라 받는다 (사전검토용 트리밍 테스트 — 시간·비용 절감).
    영상 전체를 받은 뒤 자르는 방식이 아니라 필요한 구간만 다운로드한다.

    ⚠️ 2026-08-27 실측: 방금 올라온 VOD는 유튜브 쪽에 구간(range) seek이
    편한 포맷이 아직 준비 안 돼 있어 --download-sections가 30분 타임아웃
    안에 못 끝난 사례가 있었다(us-session run #50, 사람이 트리밍 없이 수동
    재실행해 복구). 트리밍 다운로드가 타임아웃되면 자동으로 트리밍 없는
    전체 다운로드를 1회 재시도한다 — 그날 수동 복구했던 우회를 그대로
    코드에 넣은 것. 둘 다 실패하면(전체도 타임아웃) 호출부의 실패 처리로
    넘어간다.
    """
    cookies = _cookies_file()
    # 마지막 bestvideo+bestaudio/best는 무조건 폴백 — 특정 영상에 480p 제약을
    # 만족하는 포맷이 없어 "Requested format is not available"로 죽는 것을 방지
    # (해상도 상한을 못 지키더라도 다운로드 자체는 성공시키는 게 우선)
    base_cmd = _ytdlp_base(cookies) + [
        "-f", f"best[height<={resolution}]/bv*[height<={resolution}]+ba/bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
    ]
    trimmed = start_sec is not None and duration_sec is not None
    cmd = list(base_cmd)
    if trimmed:
        end_sec = start_sec + duration_sec
        cmd += ["--download-sections", f"*{start_sec}-{end_sec}"]
        log.info("VOD 구간 트리밍: %d초~%d초 (%d초 분량)만 다운로드", start_sec, end_sec, duration_sec)
    cmd += ["-o", str(out_file), vod_url]
    log.info("VOD 다운로드: %s", vod_url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_VOD_DOWNLOAD_TIMEOUT)
    except subprocess.TimeoutExpired:
        if not trimmed:
            raise CaptureError(f"VOD 다운로드가 {_VOD_DOWNLOAD_TIMEOUT}초 내에 끝나지 않음: {vod_url}")
        log.warning("구간 트리밍 다운로드가 %d초 내 실패 — 트리밍 없이 전체 다운로드 재시도",
                    _VOD_DOWNLOAD_TIMEOUT)
        full_cmd = base_cmd + ["-o", str(out_file), vod_url]
        try:
            proc = subprocess.run(full_cmd, capture_output=True, text=True,
                                   timeout=_VOD_DOWNLOAD_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise CaptureError(
                f"VOD 다운로드가 트리밍/전체 재시도 모두 {_VOD_DOWNLOAD_TIMEOUT}초 내에 끝나지 않음: {vod_url}"
            )
    if proc.returncode != 0 or not out_file.exists():
        raise CaptureError(f"VOD 다운로드 실패: {(proc.stderr or '')[-800:]}")
    log.info("다운로드 완료: %.1f MB", out_file.stat().st_size / 1e6)
    return out_file


def find_recent_vod(channel_live_url: str, max_candidates: int = 5) -> tuple[str, int | None]:
    """라이브 창을 놓쳤을 때 쓰는 다시보기 폴백(2026-08-01, noon 세션).

    `channel_live_url`은 세션의 `.../@handle/live` 형태 — 핸들만 뽑아
    `.../streams`(지난 방송 목록) 탭을 대신 본다. 데일리 라이브 채널이라
    가장 최근 항목이 곧 오늘 방송이라는 전제다.
    반환: (영상 URL, 실제 방송 시작 유닉스 타임스탬프 | None).
    두 번째 값은 `release_timestamp`가 없는 영상(비공개 처리·메타데이터
    누락 등)이면 None — 호출 측이 오프셋을 추정치로 대체해야 한다.
    """
    base = channel_live_url.rsplit("/", 1)[0]   # '.../@handle/live' → '.../@handle'
    streams_url = f"{base}/streams"
    cookies = _cookies_file()

    cmd = _ytdlp_base(cookies) + [
        "--flat-playlist", "-J", "--playlist-end", str(max_candidates), streams_url,
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=True
        ).stdout
        entries = json.loads(out).get("entries") or []
    except subprocess.CalledProcessError as e:
        raise CaptureError(f"다시보기 목록 조회 실패: {(e.stderr or '')[-500:]}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        raise CaptureError(f"다시보기 목록 조회 실패: {e}")
    if not entries or not entries[0].get("id"):
        raise CaptureError("다시보기 목록이 비어 있음")

    video_url = f"https://www.youtube.com/watch?v={entries[0]['id']}"

    release_ts: int | None = None
    try:
        info_cmd = _ytdlp_base(cookies) + ["-J", video_url]
        info = json.loads(subprocess.run(
            info_cmd, capture_output=True, text=True, timeout=60, check=True
        ).stdout)
        release_ts = info.get("release_timestamp") or info.get("timestamp")
    except Exception as e:
        log.warning("다시보기 시작 시각 확인 실패(오프셋은 추정치로 대체): %s", e)

    return video_url, release_ts


def capture_live_session(settings: dict, session: str, out_dir: Path) -> Path:
    """세션 시간창에 맞춰 라이브를 녹화. 재시도 포함."""
    from .common import session_window

    cap = settings["capture"]
    _, start, end = session_window(settings, session)
    # us/kr은 3protv 채널 전역 URL을 쓰고, noon/night처럼 다른 채널을 보는 세션은
    # sessions.<name>.live_url로 자신의 채널을 지정한다 (없으면 전역으로 폴백)
    live_url = settings["sessions"][session].get("live_url") or settings["channel"]["live_url"]
    cookies = _cookies_file()
    out_file = out_dir / f"{session}_capture.mp4"

    # 방송 시작 전이면 시작 시각까지 대기 후 폴링
    if now_kst() < start:
        wait_sec = (start - now_kst()).total_seconds()
        log.info("방송 시작(%s KST)까지 %d초 대기", start.strftime("%H:%M"), int(wait_sec))
        time.sleep(max(0, wait_sec))

    last_exc: Exception | None = None
    for attempt in range(1, cap["max_retries"] + 1):
        try:
            stream = wait_for_live(
                live_url, cap["resolution"], end, cap["poll_interval_sec"], cookies
            )
            return record_stream(stream, out_file, end)
        except CaptureError as e:
            last_exc = e
            log.warning("캡처 시도 %d/%d 실패: %s", attempt, cap["max_retries"], e)
            if now_kst() >= end:
                break
            time.sleep(10)
    raise CaptureError(f"라이브 캡처 최종 실패: {last_exc}")
