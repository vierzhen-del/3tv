"""유튜브 라이브 탐지 + 구간 녹화 / VOD 다운로드.

라이브: @3protv/live 를 폴링해 라이브가 시작되면 HLS 스트림을 ffmpeg로
세션 종료 시각(KST)까지 녹화한다.
VOD: 테스트 및 캡처 실패 복구용 — 영상 URL을 직접 지정해 다운로드한다.

유튜브가 데이터센터 IP(yt-dlp)를 차단하는 경우가 있어 YOUTUBE_COOKIES
(cookies.txt 내용)를 환경변수로 받으면 yt-dlp에 주입한다.
"""
from __future__ import annotations

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


def download_vod(vod_url: str, out_file: Path, resolution: int) -> Path:
    """VOD 다운로드 (테스트/복구 경로)."""
    cookies = _cookies_file()
    cmd = _ytdlp_base(cookies) + [
        "-f", f"best[height<={resolution}]/bv*[height<={resolution}]+ba/best",
        "--merge-output-format", "mp4",
        "-o", str(out_file),
        vod_url,
    ]
    log.info("VOD 다운로드: %s", vod_url)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0 or not out_file.exists():
        raise CaptureError(f"VOD 다운로드 실패: {(proc.stderr or '')[-800:]}")
    log.info("다운로드 완료: %.1f MB", out_file.stat().st_size / 1e6)
    return out_file


def capture_live_session(settings: dict, session: str, out_dir: Path) -> Path:
    """세션 시간창에 맞춰 라이브를 녹화. 재시도 포함."""
    from .common import session_window

    cap = settings["capture"]
    _, start, end = session_window(settings, session)
    live_url = settings["channel"]["live_url"]
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
