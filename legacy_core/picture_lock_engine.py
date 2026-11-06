"""Audio-only sync: preserve every generated video frame and adjust dialogue timing only."""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import batch_video_engine

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def _run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            creationflags=CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "命令执行失败")[-1600:])


def _probe_duration(path: Path, ffmpeg: str) -> float:
    probe = str(Path(ffmpeg).with_name("ffprobe.exe")) if os.name == "nt" else (shutil.which("ffprobe") or "ffprobe")
    result = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            creationflags=CREATE_NO_WINDOW)
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def _atempo(speed: float) -> str:
    parts: list[str] = []
    value = max(1.0, speed)
    while value > 2.0:
        parts.append("atempo=2.0")
        value /= 2.0
    parts.append(f"atempo={value:.6f}")
    return ",".join(parts)


def _shots(project: Path, episode: str) -> list[dict[str, str]]:
    path = project / "03_storyboard" / f"{episode}-shotlist.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _lines(project: Path, episode: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    spec = _load(project / "05_generation" / f"{episode}-performance-tts-spec.json", {})
    state = _load(project / "05_generation" / f"{episode}-performance-tts-state.json", {})
    grouped: dict[str, list[dict[str, Any]]] = {}
    for line in spec.get("lines", []):
        grouped.setdefault(str(line.get("shot_id") or ""), []).append(line)
    if grouped:
        return grouped, state.get("lines", {})
    ledger = project / "05_generation" / "audio" / f"{episode}-voice-jobs.csv"
    if ledger.exists():
        with ledger.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if not row.get("text") or not row.get("audio_path"):
                    continue
                line_id = f"VOICE-{row['shot_id']}"
                grouped.setdefault(row["shot_id"], []).append({
                    "id": line_id, "shot_id": row["shot_id"], "speaker": row.get("speaker", ""),
                    "text": row["text"], "start_offset": 0.18,
                })
                state.setdefault("lines", {})[line_id] = {"local_path": row["audio_path"]}
    return grouped, state.get("lines", {})


def _stem(ffmpeg: str, project: Path, output: Path, duration: float, lines: list[dict[str, Any]],
          states: dict[str, Any], timing: dict[str, Any]) -> list[dict[str, Any]]:
    output.parent.mkdir(parents=True, exist_ok=True)
    if not lines:
        _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
              f"anullsrc=r=48000:cl=stereo:d={duration:.6f}", "-c:a", "pcm_s16le", str(output)])
        return []
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    filters: list[str] = []
    labels: list[str] = []
    report: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        item = states.get(str(line.get("id")), {})
        source = project / str(item.get("local_path") or "")
        if not source.exists():
            raise FileNotFoundError(f"缺少对白音频：{source}")
        command.extend(["-i", str(source)])
        override = timing.get(str(line.get("id")), {})
        start = float(override.get("start", line.get("start_offset") or 0.18))
        source_duration = _probe_duration(source, ffmpeg)
        available = max(0.25, duration - start - 0.08)
        target = min(float(override.get("duration", available)), available)
        speed = max(1.0, source_duration / max(0.2, target))
        delay = round(start * 1000)
        filters.append(f"[{index}:a]aresample=48000,{_atempo(speed)},adelay={delay}|{delay}[d{index}]")
        labels.append(f"[d{index}]")
        report.append({"line_id": line.get("id"), "speaker": line.get("speaker"), "text": line.get("text"),
                       "start": start, "source_duration": source_duration,
                       "output_duration": source_duration / speed, "speed": speed})
    filters.append(f"anullsrc=r=48000:cl=stereo:d={duration:.6f}[sil]")
    filters.append("[sil]" + "".join(labels) +
                   f"amix=inputs={len(labels) + 1}:duration=first:normalize=0,alimiter=limit=0.95,atrim=0:{duration:.6f}[out]")
    command.extend(["-filter_complex", ";".join(filters), "-map", "[out]", "-ar", "48000", "-ac", "2",
                    "-c:a", "pcm_s16le", str(output)])
    _run(command)
    return report


def run(project: Path, episode: str) -> Path:
    project, episode = project.resolve(), episode.upper()
    ffmpeg = batch_video_engine.find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg。")
    shots = _shots(project, episode)
    lines_by_shot, line_states = _lines(project, episode)
    if not lines_by_shot:
        raise RuntimeError("没有逐镜配音。请先生成本集配音。")
    timing_path = project / "07_qc" / f"{episode}-picture-lock-timing.json"
    timing_doc = _load(timing_path, {})
    timing = timing_doc.get("lines", {})
    flagged = timing_doc.get("manual_review", [])

    work = project / "05_generation" / "picture-lock" / episode
    stems = work / "audio-stems"
    stems.mkdir(parents=True, exist_ok=True)
    concat_video, concat_audio = work / "concat-video.txt", work / "concat-audio.txt"
    video_rows: list[str] = []
    audio_rows: list[str] = []
    report_shots: list[dict[str, Any]] = []
    total = 0.0
    for shot in shots:
        shot_id = shot["shot_id"]
        source = project / "05_generation" / "videos" / episode / f"{shot_id}.mp4"
        if not source.exists():
            raise FileNotFoundError(f"缺少原始镜头：{source}")
        duration = _probe_duration(source, ffmpeg)
        stem = stems / f"{shot_id}.wav"
        line_report = _stem(ffmpeg, project, stem, duration, lines_by_shot.get(shot_id, []), line_states, timing)
        video_rows.append(f"file '{source.as_posix()}'")
        audio_rows.append(f"file '{stem.as_posix()}'")
        report_shots.append({"shot_id": shot_id, "source_video": str(source.relative_to(project)).replace("\\", "/"),
                             "video_operation": "COPY_ONLY", "duration": duration, "lines": line_report,
                             "manual_review": shot_id in flagged})
        total += duration
    concat_video.write_text("\n".join(video_rows) + "\n", encoding="utf-8")
    concat_audio.write_text("\n".join(audio_rows) + "\n", encoding="utf-8")
    picture = work / f"{episode}-picture-copy.mp4"
    dialogue = work / f"{episode}-dialogue-aligned.wav"
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat_video),
          "-an", "-c:v", "copy", "-movflags", "+faststart", str(picture)])
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat_audio),
          "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(dialogue)])
    exports = project / "06_edit" / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    target = exports / f"{episode}-auto-final-picture-lock.mp4"
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        history = project / "09_automation" / "generation-history" / episode / "picture-lock" / stamp
        history.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, history / target.name)
    temp = target.with_suffix(".part.mp4")
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(picture), "-i", str(dialogue),
          "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
          "-ar", "48000", "-ac", "2", "-t", f"{total:.6f}", "-movflags", "+faststart", str(temp)])
    temp.replace(target)
    status = "MANUAL_REVIEW" if flagged else "PASSED"
    report = {"schema_version": "1.0", "episode": episode, "status": status,
              "created_at": datetime.now().isoformat(timespec="seconds"), "mode": "PICTURE_LOCK_AUDIO_ONLY",
              "guarantee": "视频使用 -c:v copy；不重绘、不插帧、不裁切、不修改人物脸部或神态。",
              "output": str(target.relative_to(project)).replace("\\", "/"), "duration": total,
              "manual_review": flagged, "shots": report_shots}
    report_path = project / "07_qc" / f"{episode}-picture-lock-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"画面锁定版完成：{target}", flush=True)
    if flagged:
        print("需人工复核镜头：" + "、".join(flagged), flush=True)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="画面锁定、仅校准声音")
    parser.add_argument("--project", required=True)
    parser.add_argument("--episode", required=True)
    args = parser.parse_args(argv)
    try:
        run(Path(args.project), args.episode)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"错误：{exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
