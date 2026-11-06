"""Local per-shot lip-sync and strict dialogue assembly using MuseTalk 1.5."""
from __future__ import annotations

import argparse
import csv
import hashlib
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


def _run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "命令执行失败")[-1800:])


def _load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def _probe(path: Path, ffmpeg: str) -> dict[str, Any]:
    probe = str(Path(ffmpeg).with_name("ffprobe.exe")) if os.name == "nt" else (shutil.which("ffprobe") or "ffprobe")
    result = subprocess.run([probe, "-v", "error", "-show_entries",
                             "stream=codec_name,width,height,r_frame_rate,sample_rate,channels:format=duration,size",
                             "-of", "json", str(path)], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
    return json.loads(result.stdout or "{}") if result.returncode == 0 else {}


def _signature(*paths: Path) -> str:
    digest = hashlib.sha256()
    for path in paths:
        stat = path.stat()
        digest.update(str(path.resolve()).encode("utf-8"))
        digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
    return digest.hexdigest()[:16]


def _shots(project: Path, episode: str) -> list[dict[str, str]]:
    path = project / "03_storyboard" / f"{episode}-shotlist.csv"
    if not path.exists():
        raise FileNotFoundError(f"缺少镜头表：{path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _duration(shot: dict[str, str]) -> float:
    try:
        return max(0.5, float(shot.get("duration_s") or 5))
    except (TypeError, ValueError):
        return 5.0


def _dialogue(shot: dict[str, str]) -> str:
    return str(shot.get("dialogue_text") or shot.get("dialogue_or_vo") or "").strip()


def _eligible(shot: dict[str, str], line_count: int) -> bool:
    route = str(shot.get("audio_route") or "")
    notes = str(shot.get("notes") or "")
    blocked = any(word in route + notes for word in ("画外音", "无对白", "不做口型", "双人对白"))
    return bool(_dialogue(shot)) and "口型" in route and not blocked and line_count == 1


def _performance_lines(project: Path, episode: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    spec = _load(project / "05_generation" / f"{episode}-performance-tts-spec.json", {})
    state = _load(project / "05_generation" / f"{episode}-performance-tts-state.json", {})
    grouped: dict[str, list[dict[str, Any]]] = {}
    for line in spec.get("lines", []):
        if line.get("shot_id"):
            grouped.setdefault(str(line["shot_id"]), []).append(line)
    return grouped, state.get("lines", {}) if isinstance(state, dict) else {}


def _ledger_lines(project: Path, episode: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    path = project / "05_generation" / "audio" / f"{episode}-voice-jobs.csv"
    grouped: dict[str, list[dict[str, Any]]] = {}
    state: dict[str, Any] = {}
    if not path.exists():
        return grouped, state
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if not row.get("text") or not row.get("audio_path"):
                continue
            line_id = f"VOICE-{row['shot_id']}"
            grouped.setdefault(row["shot_id"], []).append({
                "id": line_id, "shot_id": row["shot_id"], "speaker": row.get("speaker", ""),
                "text": row.get("text", ""), "start_offset": 0.18, "max_duration": 999.0,
            })
            state[line_id] = {"local_path": row["audio_path"], "status": row.get("status", "")}
    return grouped, state


def _make_stem(ffmpeg: str, project: Path, output: Path, duration: float,
               lines: list[dict[str, Any]], line_state: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if not lines:
        if output.exists():
            return
        _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
              f"anullsrc=r=48000:cl=stereo:d={duration:.6f}", "-c:a", "pcm_s16le", str(output)])
        return
    source_paths = [project / str(line_state.get(str(line.get("id")), {}).get("local_path") or "") for line in lines]
    valid_sources = [path for path in source_paths if path.exists()]
    if output.exists() and valid_sources and output.stat().st_mtime_ns >= max(path.stat().st_mtime_ns for path in valid_sources):
        return
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    filters: list[str] = []
    labels: list[str] = []
    usable = 0
    for line in lines:
        item = line_state.get(str(line.get("id")), {})
        source = project / str(item.get("local_path") or "")
        if not source.exists():
            continue
        command.extend(["-i", str(source)])
        delay = round(max(0.0, float(line.get("start_offset") or 0.18)) * 1000)
        filters.append(f"[{usable}:a]aresample=48000,adelay={delay}|{delay}[d{usable}]")
        labels.append(f"[d{usable}]")
        usable += 1
    if not usable:
        raise RuntimeError(f"对白音频不存在：{output.stem}")
    filters.append(f"anullsrc=r=48000:cl=stereo:d={duration:.6f}[sil]")
    filters.append("[sil]" + "".join(labels) +
                   f"amix=inputs={usable + 1}:duration=first:normalize=0,alimiter=limit=0.95,atrim=0:{duration:.6f}[out]")
    command.extend(["-filter_complex", ";".join(filters), "-map", "[out]", "-ar", "48000", "-ac", "2",
                    "-c:a", "pcm_s16le", str(output)])
    _run(command)


def _runtime(project: Path) -> Path:
    config = _load(project / "09_automation" / "factory-config.json", {})
    configured = str((config.get("musetalk") or {}).get("runtime") or "").strip()
    return Path(configured) if configured else Path.home() / ".cache" / "musetalk-runtime"


def run(project: Path, episode: str) -> Path:
    project = project.resolve()
    episode = episode.upper()
    ffmpeg = batch_video_engine.find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg。")
    runtime = _runtime(project)
    python = runtime / ".venv" / "Scripts" / "python.exe"
    required = [python, runtime / "scripts" / "inference.py",
                runtime / "models" / "musetalkV15" / "unet_fp16.safetensors", runtime / "models" / "whisper"]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError("MuseTalk 本地环境不完整：" + "；".join(missing))

    shots = _shots(project, episode)
    perf_lines, perf_state = _performance_lines(project, episode)
    ledger_lines, ledger_state = _ledger_lines(project, episode)
    lines_by_shot = perf_lines or ledger_lines
    line_state = perf_state if perf_lines else ledger_state
    if not lines_by_shot:
        raise RuntimeError("没有逐镜配音。请先点击“生成本集配音”。")

    work = project / "05_generation" / "lipsync" / episode
    stems, outputs, normalized = work / "audio-stems", work / "videos", work / "normalized"
    for folder in (stems, outputs, normalized):
        folder.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    tasks: list[dict[str, str]] = []
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    runtime_inputs = runtime / "inputs" / "desktop" / episode / run_id
    (runtime_inputs / "video").mkdir(parents=True, exist_ok=True)
    (runtime_inputs / "audio").mkdir(parents=True, exist_ok=True)
    result_rel = Path("results") / "desktop" / episode / run_id

    for shot in shots:
        shot_id = shot["shot_id"]
        source = project / "05_generation" / "videos" / episode / f"{shot_id}.mp4"
        if not source.exists():
            raise FileNotFoundError(f"缺少画面：{source}")
        duration = _duration(shot)
        lines = lines_by_shot.get(shot_id, [])
        stem = stems / f"{shot_id}-dialogue.wav"
        _make_stem(ffmpeg, project, stem, duration, lines, line_state)
        eligible = _eligible(shot, len(lines))
        signature = _signature(source, stem)
        synced = outputs / f"{shot_id}-{signature}.mp4"
        if eligible and not synced.exists():
            video_input = runtime_inputs / "video" / f"{shot_id}.mp4"
            audio_input = runtime_inputs / "audio" / f"{shot_id}.wav"
            shutil.copy2(source, video_input)
            shutil.copy2(stem, audio_input)
            tasks.append({"id": shot_id.replace("-", "_"), "video": video_input.relative_to(runtime).as_posix(),
                          "audio": audio_input.relative_to(runtime).as_posix(), "name": f"{shot_id}-{signature}.mp4",
                          "target": str(synced), "shot_id": shot_id})
        entries.append({"shot_id": shot_id, "duration": duration, "source": str(source), "stem": str(stem),
                        "eligible": eligible, "signature": signature, "synced": str(synced),
                        "mode": "口型同步" if eligible else ("对白/画外音合成" if lines else "静音")})

    if tasks:
        config_dir = runtime / "configs" / "inference"
        config_dir.mkdir(parents=True, exist_ok=True)
        yaml_path = config_dir / f"desktop-{episode}-{run_id}.yaml"
        yaml_lines: list[str] = []
        for task in tasks:
            yaml_lines.extend([f"{task['id']}:", f"  video_path: \"{task['video']}\"",
                               f"  audio_path: \"{task['audio']}\"", "  face_side: \"largest\"",
                               f"  result_name: \"{task['name']}\""])
        yaml_path.write_text("\n".join(yaml_lines) + "\n", encoding="utf-8")
        env = os.environ.copy()
        env["MUSETALK_LOW_VRAM"] = "1"
        env["PYTHONPATH"] = str(runtime)
        print(f"本地口型同步：{len(tasks)} 个对白镜头（不调用付费 API）", flush=True)
        _run([str(python), "scripts/inference.py", "--inference_config", yaml_path.relative_to(runtime).as_posix(),
              "--result_dir", result_rel.as_posix(), "--unet_model_path", "models/musetalkV15/unet_fp16.safetensors",
              "--unet_config", "models/musetalkV15/musetalk.json", "--whisper_dir", "models/whisper",
              "--version", "v15", "--use_float16", "--batch_size", "1", "--saved_coord"], cwd=runtime, env=env)
        for task in tasks:
            produced = runtime / result_rel / "v15" / task["name"]
            if not produced.exists():
                raise RuntimeError(f"口型输出缺失：{produced}")
            shutil.copy2(produced, Path(task["target"]))
            print(f"口型完成：{task['shot_id']}", flush=True)

    failed = [item["shot_id"] for item in entries if item["eligible"] and not Path(item["synced"]).exists()]
    if failed:
        raise RuntimeError("口型同步未全部完成，禁止输出同步版成片：" + "、".join(failed))

    concat_video, concat_audio = work / "concat-video.txt", work / "concat-audio.txt"
    video_lines: list[str] = []
    audio_lines: list[str] = []
    for item in entries:
        chosen = Path(item["synced"]) if item["eligible"] else Path(item["source"])
        target_norm = normalized / f"{item['shot_id']}.mp4"
        _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(chosen), "-an", "-vf",
              ("scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2:black,"
               f"setsar=1,fps=30,tpad=stop_mode=clone:stop_duration=2,trim=duration={item['duration']:.6f},"
               "setpts=PTS-STARTPTS,format=yuv420p"), "-c:v", "libx264", "-preset", "medium", "-crf", "18",
              "-pix_fmt", "yuv420p", str(target_norm)])
        video_lines.append(f"file '{target_norm.as_posix()}'")
        audio_lines.append(f"file '{Path(item['stem']).as_posix()}'")
    concat_video.write_text("\n".join(video_lines) + "\n", encoding="utf-8")
    concat_audio.write_text("\n".join(audio_lines) + "\n", encoding="utf-8")
    build_video, build_audio = work / f"{episode}-picture-sync.mp4", work / f"{episode}-dialogue-sync.wav"
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat_video),
          "-an", "-c", "copy", "-movflags", "+faststart", str(build_video)])
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat_audio),
          "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(build_audio)])

    exports = project / "06_edit" / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    target = exports / f"{episode}-auto-final-lipsync.mp4"
    if target.exists():
        history = project / "09_automation" / "generation-history" / episode / "final-lipsync" / run_id
        history.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, history / target.name)
    temp = target.with_suffix(".part.mp4")
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(build_video), "-i", str(build_audio),
          "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
          "-ac", "2", "-shortest", "-movflags", "+faststart", str(temp)])
    temp.replace(target)
    report = {"schema_version": "1.0", "episode": episode, "status": "PASSED",
              "created_at": datetime.now().isoformat(timespec="seconds"), "engine": "MuseTalk 1.5 local",
              "output": str(target.relative_to(project)).replace("\\", "/"), "probe": _probe(target, ffmpeg),
              "shots": entries, "rules": {"voice_over_lipsync": False, "silent_shots_have_voice": False,
                                            "all_required_lipsync_succeeded": True}}
    report_path = project / "07_qc" / f"{episode}-desktop-lipsync-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"同步版成片完成：{target}", flush=True)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地逐镜口型同步")
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
