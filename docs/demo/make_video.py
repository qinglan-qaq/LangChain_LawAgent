"""把录制好的 WebM + 中文旁白合成为 MP4。

前置：
  - 已运行 record_demo.mjs，产出 recording-log-*.json 与 demo-raw-*.webm
  - Windows 自带中文语音 Microsoft Huihui Desktop
  - pip install imageio-ffmpeg（或设置 FFMPEG 环境变量指向完整 ffmpeg）

用法：
  python docs/demo/make_video.py
  python docs/demo/make_video.py --log docs/demo/output/recording-log-xxx.json

旁白时间轴在 narration.json；如重录后时间戳变化，改 startMs/endMs 即可。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT_DIR = HERE / "output"
AUDIO_DIR = OUT_DIR / "audio"
NARRATION = HERE / "narration.json"


def find_ffmpeg() -> str:
    env = os.environ.get("FFMPEG")
    if env:
        p = Path(env)
        if p.is_dir():
            p = p / "ffmpeg.exe"
        if p.is_file():
            return str(p)
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    raise SystemExit("未找到 ffmpeg：pip install imageio-ffmpeg 或设置 FFMPEG 环境变量")


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def synthesize(text: str, out_wav: Path, voice: str, rate: int) -> None:
    tmp_txt = out_wav.with_suffix(".txt")
    tmp_txt.write_text(text, encoding="utf-8")
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SelectVoice('{voice}'); $s.Rate={rate}; "
        f"$s.SetOutputToWaveFile('{out_wav}'); "
        f"$s.Speak([IO.File]::ReadAllText('{tmp_txt}',[Text.Encoding]::UTF8)); "
        "$s.Dispose()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        check=True,
        capture_output=True,
    )
    tmp_txt.unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", help="recording-log-*.json；默认取 output 下最新")
    ap.add_argument("--out", help="输出 mp4 路径")
    args = ap.parse_args()

    logs = sorted(OUT_DIR.glob("recording-log-*.json"), key=lambda p: p.stat().st_mtime)
    if not logs:
        raise SystemExit("找不到 recording-log-*.json，先运行 record_demo.mjs")
    log_path = Path(args.log) if args.log else logs[-1]
    log = json.loads(log_path.read_text(encoding="utf-8"))
    video = Path(log["video"])
    if not video.exists():
        raise SystemExit(f"找不到录像: {video}")

    narration = json.loads(NARRATION.read_text(encoding="utf-8"))
    voice = narration.get("voice", "Microsoft Huihui Desktop")
    rate = int(narration.get("rate", 0))
    sections = narration["sections"]

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg()

    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    total_audio_end = 0.0

    for i, sec in enumerate(sections):
        wav = AUDIO_DIR / f"{i:02d}-{sec['id']}.wav"
        synthesize(sec["text"], wav, voice, rate)
        dur = wav_duration(wav)
        start = float(sec["startMs"]) / 1000.0
        window = max(0.5, (float(sec["endMs"]) - float(sec["startMs"])) / 1000.0)
        tempo = 1.0
        if dur > window:
            tempo = min(1.45, dur / window)
        chain = f"[{i + 1}:a]"
        if tempo > 1.001:
            chain += f"atempo={tempo:.3f},"
        chain += f"adelay={int(start * 1000)}|{int(start * 1000)},volume=1.0[a{i}]"
        filters.append(chain)
        labels.append(f"[a{i}]")
        inputs += ["-i", str(wav)]
        actual = dur / tempo
        total_audio_end = max(total_audio_end, start + actual)
        print(
            f"{sec['id']:<8} start={start:6.1f}s window={window:5.1f}s "
            f"tts={dur:5.1f}s tempo={tempo:.2f} end={start + actual:6.1f}s"
        )

    mix = "".join(labels) + f"amix=inputs={len(labels)}:normalize=0:duration=longest[aout]"
    filter_complex = ";".join(filters + [mix])

    out = Path(args.out) if args.out else OUT_DIR / f"demo-{log_path.stem.replace('recording-log-', '')}.mp4"
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(video),
        *inputs,
        "-filter_complex",
        filter_complex,
        "-map",
        "0:v",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-ar",
        "44100",
        "-movflags",
        "+faststart",
        "-t",
        f"{log['durationMs'] / 1000.0:.2f}",
        str(out),
    ]
    print("\nffmpeg ...")
    # Windows Defender 偶尔会短暂锁住刚被调用的 ffmpeg.exe，重试即可
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            subprocess.run(cmd, check=True)
            last_err = None
            break
        except PermissionError as exc:
            last_err = exc
            print(f"  ffmpeg 启动被拒绝，{2 * (attempt + 1)}s 后重试…")
            time.sleep(2 * (attempt + 1))
    if last_err is not None:
        raise last_err
    print(f"\nMP4  {out}")
    print(f"时长 {log['durationMs'] / 1000.0:.1f}s，旁白结束 {total_audio_end:.1f}s")


if __name__ == "__main__":
    main()
