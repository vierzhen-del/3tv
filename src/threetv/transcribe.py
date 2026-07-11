"""faster-whisper 오디오 전사.

라이브 방송은 자막이 없으므로 녹화 구간의 오디오를 직접 전사해
진행자/패널 발언(종목 언급, 호재·악재 맥락)을 확보한다.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .common import log


def extract_audio(video: Path, out_dir: Path) -> Path:
    wav = out_dir / "audio.wav"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video),
        "-vn", "-ac", "1", "-ar", "16000",
        str(wav),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=900)
    return wav


def transcribe(video: Path, out_dir: Path, model_size: str = "small") -> str:
    """영상에서 오디오 추출 후 한국어 전사. 타임스탬프 포함 텍스트 반환."""
    from faster_whisper import WhisperModel

    wav = extract_audio(video, out_dir)
    log.info("Whisper(%s) 전사 시작...", model_size)
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(wav), language="ko", vad_filter=True, beam_size=5
    )

    lines: list[str] = []
    for seg in segments:
        mm, ss = divmod(int(seg.start), 60)
        lines.append(f"[{mm:02d}:{ss:02d}] {seg.text.strip()}")
    text = "\n".join(lines)

    out_file = out_dir / "transcript.txt"
    out_file.write_text(text, encoding="utf-8")
    log.info("전사 완료: %d 세그먼트, %d자", len(lines), len(text))
    wav.unlink(missing_ok=True)  # 용량 정리
    return text
