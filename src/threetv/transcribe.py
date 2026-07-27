"""faster-whisper 오디오 전사.

라이브 방송은 자막이 없으므로 녹화 구간의 오디오를 직접 전사해
진행자/패널 발언(종목 언급, 호재·악재 맥락)을 확보한다.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .common import log


def extract_audio(
    video: Path, out_dir: Path,
    start_sec: int | None = None, dur_sec: int | None = None,
) -> Path:
    """오디오 추출. start_sec/dur_sec을 주면 그 구간만 잘라낸다.

    -ss를 -i 앞에 두는 입력 탐색(input seeking)이라 긴 영상에서도 즉시 점프한다
    (구간 전사는 45분 전체를 12분 돌리는 대신 15분만 ~4분에 끝내기 위한 것).
    """
    wav = out_dir / "audio.wav"
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if start_sec is not None:
        cmd += ["-ss", str(int(start_sec))]
    cmd += ["-i", str(video)]
    if dur_sec is not None:
        cmd += ["-t", str(int(dur_sec))]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", str(wav)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=900)
    return wav


def transcribe(
    video: Path, out_dir: Path, model_size: str = "small",
    start_sec: int | None = None, dur_sec: int | None = None,
) -> str:
    """영상에서 오디오 추출 후 한국어 전사. 타임스탬프 포함 텍스트 반환.

    start_sec/dur_sec을 주면 그 구간만 전사하고, 타임스탬프는 **영상 기준**으로
    되돌려 표기한다(start_sec을 더함) — 화면 캡처 시각과 대조할 수 있어야 하므로.
    """
    from faster_whisper import WhisperModel

    wav = extract_audio(video, out_dir, start_sec, dur_sec)
    log.info("Whisper(%s) 전사 시작...%s", model_size,
             f" (구간 {start_sec}s부터 {dur_sec}s)" if start_sec is not None else "")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(wav), language="ko", vad_filter=True, beam_size=5
    )

    offset = int(start_sec or 0)
    lines: list[str] = []
    for seg in segments:
        mm, ss = divmod(int(seg.start) + offset, 60)
        lines.append(f"[{mm:02d}:{ss:02d}] {seg.text.strip()}")
    text = "\n".join(lines)

    out_file = out_dir / "transcript.txt"
    out_file.write_text(text, encoding="utf-8")
    log.info("전사 완료: %d 세그먼트, %d자", len(lines), len(text))
    wav.unlink(missing_ok=True)  # 용량 정리
    return text
