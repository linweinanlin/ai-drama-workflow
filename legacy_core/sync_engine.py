"""配音 / 字幕 / 画面 三方对齐引擎。

原则（按需求）：
- 配音是时间基准：先测出每句真实发声起止，再排画面与字幕。
- 台词超镜头时长时压缩台词（atempo），绝不拉伸画面。
- 每句结束后留反应时间：普通 0.1–0.3 秒，转折镜头 0.3–0.6 秒。
- 字幕入点=发声起点，出点=发声终点+0.1 秒（不超过镜尾）；停顿超 0.5 秒必须断字幕。
- 字幕版式：底部安全区（离底 5–8% 画面高）、字号 6–8% 画面高、按说话人左/中/右、按情绪微调。
- 分析统一在 16 kHz 单声道工作副本上做；成片混音仍保持 48 kHz。
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import batch_video_engine
try:
    import lip_sync
except Exception:  # noqa: BLE001
    lip_sync = None

ANALYSIS_RATE = 16000
MIX_RATE = 48000
LEAD_IN = 0.15           # 台词在镜头内起播偏移
TAIL_NORMAL = 0.18       # 普通镜头句尾留白
TAIL_TURN = 0.40         # 转折镜头句尾留白
SUBTITLE_HOLD = 0.10     # 字幕比声音多留 0.1 秒
SUBTITLE_MAX_HOLD = 0.50
PAUSE_BREAK = 0.50       # 句内停顿超过 0.5 秒断字幕
MAX_ATEMPO = 2.0
MIN_ATEMPO = 0.85

TURN_WORDS = ("反转", "但是", "却", "竟然", "突然", "不对", "等等")
HOT_EMOTIONS = ("愤怒", "震惊警觉")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=0x08000000)


def _ffmpeg() -> str:
    ffmpeg = batch_video_engine.find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg")
    return ffmpeg


def _probe_duration(path: Path) -> float:
    ffmpeg = _ffmpeg()
    probe = str(Path(ffmpeg).with_name("ffprobe.exe"))
    result = _run([probe, "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    try:
        return float((result.stdout or "0").strip())
    except ValueError:
        return 0.0


def speech_bounds(path: Path, threshold: str = "-35dB", min_silence: float = 0.12) -> tuple[float, float, float]:
    """返回 (发声起点, 发声终点, 总时长)，用 ffmpeg silencedetect 在 16k 副本上测。"""
    total = _probe_duration(path)
    if total <= 0:
        return 0.0, 0.0, 0.0
    ffmpeg = _ffmpeg()
    work = path.with_suffix(".probe16k.wav")
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
          "-ac", "1", "-ar", str(ANALYSIS_RATE), str(work)])
    result = _run([ffmpeg, "-hide_banner", "-i", str(work),
                   "-af", f"silencedetect=noise={threshold}:d={min_silence}", "-f", "null", "-"])
    text = result.stderr or ""
    starts = [float(value) for value in re.findall(r"silence_start: ([0-9.]+)", text)]
    ends = [float(value) for value in re.findall(r"silence_end: ([0-9.]+)", text)]
    speech_start = ends[0] if starts and starts[0] <= 0.05 and ends else 0.0
    speech_end = starts[-1] if starts and (not ends or ends[-1] < starts[-1]) else total
    speech_end = min(total, max(speech_start, speech_end))
    work.unlink(missing_ok=True)
    return round(speech_start, 3), round(speech_end, 3), round(total, 3)



EMOTION_WEIGHTS = (
    ("平静", 0.20), ("日常", 0.20), ("克制", 0.30), ("紧张", 0.50), ("警觉", 0.55),
    ("焦虑", 0.60), ("疑惑", 0.45), ("压抑", 0.55), ("悲伤", 0.65), ("愤怒", 0.90),
    ("震惊", 0.85), ("恐惧", 0.80), ("崩溃", 0.95), ("得意", 0.55), ("温柔", 0.25),
)


def emotion_intensity(shot: dict[str, str]) -> float:
    """0–1 的情绪强度启发式：先看情绪字段，再看动作与台词里的关键词。"""
    haystack = " ".join(str(shot.get(key) or "") for key in ("emotion", "emotion_line", "action", "dialogue_text"))
    best = 0.35
    for word, weight in EMOTION_WEIGHTS:
        if word in haystack:
            best = max(best, weight)
    if any(mark in haystack for mark in ("！", "!", "吼", "喊", "尖叫")):
        best = min(1.0, best + 0.15)
    return round(best, 2)


def _dialogue(shot: dict[str, str]) -> str:
    for key in ("dialogue_text", "dialogue_or_vo"):
        text = (shot.get(key) or "").strip()
        if text and text != "无台词":
            return text
    return ""


def _is_turn(shot: dict[str, str], next_shot: dict[str, str] | None) -> bool:
    emotion = (shot.get("emotion") or "") + (shot.get("emotion_line") or "")
    if any(word in emotion for word in HOT_EMOTIONS):
        return True
    action = (shot.get("action") or "") + (next_shot.get("action", "") if next_shot else "")
    return any(word in action for word in TURN_WORDS)


def _speaker_position(speakers: list[str], speaker: str) -> str:
    positions = ["center", "left", "right"]
    if speaker not in speakers:
        return "center"
    return positions[speakers.index(speaker) % len(positions)]


def _srt_time(seconds: float) -> str:
    value = max(0, int(round(seconds * 1000)))
    hours, value = divmod(value, 3600000)
    minutes, value = divmod(value, 60000)
    secs, ms = divmod(value, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _ass_time(seconds: float) -> str:
    value = max(0, int(round(seconds * 100)))
    hours, value = divmod(value, 360000)
    minutes, value = divmod(value, 6000)
    secs, cs = divmod(value, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _speech_segments(path: Path, min_gap: float = PAUSE_BREAK) -> list[tuple[float, float]]:
    """把一条配音按 >min_gap 的停顿切成若干发声段（相对文件起点）。"""
    total = _probe_duration(path)
    if total <= 0:
        return []
    ffmpeg = _ffmpeg()
    result = _run([ffmpeg, "-hide_banner", "-i", str(path),
                   "-af", "silencedetect=noise=-35dB:d=%.2f" % min_gap, "-f", "null", "-"])
    text = result.stderr or ""
    starts = [float(value) for value in re.findall(r"silence_start: ([0-9.]+)", text)]
    ends = [float(value) for value in re.findall(r"silence_end: ([0-9.]+)", text)]
    cuts = [(start, end) for start, end in zip(starts, ends) if start > 0.05 and end < total - 0.05]
    segments: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in cuts:
        if start - cursor > 0.05:
            segments.append((round(cursor, 3), round(start, 3)))
        cursor = end
    if total - cursor > 0.05:
        segments.append((round(cursor, 3), round(total, 3)))
    return segments or [(0.0, round(total, 3))]


def _split_text(text: str, weights: list[float]) -> list[str]:
    """按各段时长比例切分台词，尽量落在标点上。"""
    if len(weights) <= 1:
        return [text]
    total = sum(weights) or 1.0
    pieces: list[str] = []
    index = 0
    for position in range(len(weights)):
        if position == len(weights) - 1:
            pieces.append(text[index:])
            break
        take = max(1, int(round(len(text) * weights[position] / total)))
        end = min(len(text), index + take)
        for mark in ("，", "。", "？", "！", "、", "；"):
            found = text.rfind(mark, index, end + 6)
            if found > index:
                end = found + 1
                break
        pieces.append(text[index:end])
        index = end
    return [piece for piece in pieces if piece]


def _build_cues(text: str, audio_start: float, segments: list[tuple[float, float]],
                shot_start: float, shot_duration: float) -> list[dict[str, Any]]:
    """每条台词按停顿拆成字幕条：入点=发声起点，出点=发声终点+0.1 秒（不越镜尾）。"""
    pieces = _split_text(text, [end - start for start, end in segments])
    cues: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(segments):
        piece = pieces[index] if index < len(pieces) else ""
        if not piece:
            continue
        cue_start = audio_start + start
        cue_end = min(audio_start + end + SUBTITLE_HOLD, shot_start + shot_duration - 0.05)
        cues.append({
            "start": round(cue_start, 3),
            "end": round(max(cue_start + 0.25, cue_end), 3),
            "text": piece,
        })
    return cues


def _voice_source(project: Path, episode: str, shot_id: str) -> Path | None:
    base = project / "05_generation" / "audio" / "voice"
    for name in (f"{shot_id}-cloud.wav", f"{shot_id}.wav", f"{shot_id}-fit.wav"):
        candidate = base / name
        if candidate.exists():
            return candidate
    return None


def picture_durations(project: Path, episode: str, shots: list[dict[str, str]],
                      plan: dict[str, int]) -> dict[str, float]:
    """画面时长以实际视频为准（时间轴要对得上成片），没有视频才退回计划值。"""
    result: dict[str, float] = {}
    for shot in shots:
        shot_id = shot.get("shot_id", "")
        video = Path(project) / "05_generation" / "videos" / episode / f"{shot_id}.mp4"
        actual = _probe_duration(video) if video.exists() else 0.0
        result[shot_id] = round(actual, 3) if actual > 0 else float(plan.get(shot_id, 5))
    return result


def align(project: Path, episode: str, shots: list[dict[str, str]], config: dict[str, Any],
          height: int = 1280) -> dict[str, Any]:
    """跑一次完整对齐：压缩超长台词 → 重排音轨 → 生成 SRT/ASS → 输出时间码表与质检。"""
    project = Path(project)
    ffmpeg = _ffmpeg()
    plan = batch_video_engine.plan_durations(shots, config)
    pictures = picture_durations(project, episode, shots, plan)
    ordered = sorted(shots, key=lambda shot: int(shot.get("order") or 0))
    speakers = []
    for shot in ordered:
        name = (shot.get("speaker") or "").strip()
        if name and name not in {"无", "无台词"} and name not in speakers:
            speakers.append(name)

    aligned_dir = project / "05_generation" / "audio" / "aligned"
    aligned_dir.mkdir(parents=True, exist_ok=True)
    timeline: list[dict[str, Any]] = []
    cursor = 0.0
    problems: list[dict[str, str]] = []

    for index, shot in enumerate(ordered):
        shot_id = shot.get("shot_id", "")
        duration = float(pictures.get(shot_id) or plan.get(shot_id, shot.get("duration_s") or 5))
        next_shot = ordered[index + 1] if index + 1 < len(ordered) else None
        text = _dialogue(shot)
        tail = TAIL_TURN if _is_turn(shot, next_shot) else TAIL_NORMAL
        emotion_text = (shot.get("emotion") or "") + (shot.get("emotion_line") or "")
        entry: dict[str, Any] = {
            "hot": any(word in emotion_text for word in HOT_EMOTIONS),
            "shot": shot_id,
            "speaker": (shot.get("speaker") or "").strip(),
            "text": text,
            "picture_start": round(cursor, 3),
            "picture_duration": round(duration, 3),
            "audio_start": "",
            "audio_end": "",
            "speech_raw": 0.0,
            "atempo": 1.0,
            "sub_start": "",
            "sub_end": "",
            "aligned": True,
            "note": "",
        }
        source = _voice_source(project, episode, shot_id) if text else None
        mouth = None
        if text and lip_sync is not None:
            try:
                mouth = lip_sync.detect(project, episode, shot_id).get("window")
            except Exception:  # noqa: BLE001
                mouth = None
        entry["mouth_window"] = mouth
        if text and source:
            start, end, total = speech_bounds(source)
            speech = max(0.2, end - start)
            if mouth:
                lead = max(0.0, float(mouth[0]) - 0.05)
                limit = min(duration - 0.05, float(mouth[1]) + 0.80)
                available = max(0.4, limit - lead)
                entry["note"] = "口型对齐 %.2fs" % lead
            else:
                lead = LEAD_IN
                available = max(0.4, duration - LEAD_IN - tail)
                entry["note"] = "未检测到口型，按镜头头对齐"
            atempo = 1.0
            if speech > available:
                atempo = min(MAX_ATEMPO, speech / available)
                entry["note"] = (entry["note"] + "；" if entry.get("note") else "") + (
                    "台词超长，压缩 %.2fx" % atempo)
            elif speech < available * 0.6:
                atempo = max(MIN_ATEMPO, speech / (available * 0.85)) if speech > 0 else 1.0
            target = aligned_dir / f"{shot_id}-aligned.wav"
            filters = []
            if start > 0.01 or end < total - 0.01:
                filters.append(f"atrim={start:.3f}:{end:.3f}")
            if abs(atempo - 1.0) > 0.01:
                filters.append(f"atempo={atempo:.3f}")
            filters.append("atrim=0:%.3f" % available)
            filters.append(f"aresample={MIX_RATE}")
            _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
                  "-af", ",".join(filters), "-ac", "1", "-ar", str(MIX_RATE), str(target)])
            speech_new = _probe_duration(target)
            entry.update({
                "speech_raw": round(speech, 3),
                "atempo": round(atempo, 3),
                "audio_start": round(cursor + lead, 3),
                "audio_end": round(cursor + lead + speech_new, 3),
                "source": str(target.relative_to(project)).replace("\\", "/"),
            })
            segments = _speech_segments(target)
            entry["cues"] = _build_cues(text, entry["audio_start"], segments, cursor, duration)
            if entry["cues"]:
                entry["sub_start"] = entry["cues"][0]["start"]
                entry["sub_end"] = entry["cues"][-1]["end"]
                if len(entry["cues"]) > 1:
                    entry["note"] = (entry["note"] + "；" if entry["note"] else "") + (
                        "句内停顿，字幕分 %d 段" % len(entry["cues"]))
            if entry["audio_end"] > cursor + duration + 0.001:
                entry["aligned"] = False
                problems.append({
                    "shot": shot_id,
                    "issue": f"台词超出镜头边界 {entry['audio_end'] - (cursor + duration):.2f}s",
                    "fix": "继续提高 atempo 或拆句重配",
                })
        elif text:
            entry["aligned"] = False
            problems.append({"shot": shot_id, "issue": "有台词但没有配音文件", "fix": "重跑配音（生成本集配音）"})
        else:
            entry["note"] = "无台词" if not entry["note"] else entry["note"]

        # 口型/声音先后：配音起点相对画面起点的偏移
        if entry["audio_start"] != "":
            anchor = cursor + (float(mouth[0]) if mouth else LEAD_IN)
            entry["lip_offset_ms"] = int(round((float(entry["audio_start"]) - anchor) * 1000))
            entry["lead_ms"] = int(round((float(entry["audio_start"]) - cursor) * 1000))
            if mouth and abs(float(entry["audio_start"]) - anchor) > 0.25:
                problems.append({"shot": shot_id,
                                 "issue": "声音与嘴动起点差 %.0fms" % (entry["lip_offset_ms"]),
                                 "fix": "重跑对齐，或人工微调该镜起播点"})
            if mouth and entry["audio_end"] and float(entry["audio_end"]) > cursor + float(mouth[1]) + 1.0:
                problems.append({"shot": shot_id,
                                 "issue": "声音明显长于嘴动窗口 %.2fs" % (
                                     float(entry["audio_end"]) - (cursor + float(mouth[1]))),
                                 "fix": "提高压缩比或拆句重配"})
        entry["emotion"] = emotion_intensity(shot)
        timeline.append(entry)
        cursor += duration

    total_duration = cursor

    # 相邻镜头情绪跳变检查（相邻强度差 > 0.45 视为突兀）
    for index in range(1, len(timeline)):
        delta = abs(timeline[index]["emotion"] - timeline[index - 1]["emotion"])
        if delta > 0.45:
            problems.append({
                "shot": timeline[index]["shot"],
                "issue": f"与上一镜情绪跳变 {delta:.2f}（{timeline[index - 1]['emotion']:.2f} → "
                         f"{timeline[index]['emotion']:.2f}）",
                "fix": "加一个过渡镜头，或把台词/表演强度往中间带一档",
            })

    # 重排整集音轨
    track = project / "05_generation" / "audio" / f"{episode}-voice-track.wav"
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    for index, entry in enumerate(timeline):
        if not entry.get("source"):
            continue
        inputs.extend(["-i", str(project / entry["source"])])
        delay = int(float(entry["audio_start"]) * 1000)
        filters.append(f"[{len(labels)}:a]adelay={delay}|{delay}[a{len(labels)}]")
        labels.append(f"[a{len(labels)}]")
    if labels:
        filters.append(f"anullsrc=channel_layout=mono:sample_rate={MIX_RATE}:d={total_duration:.3f}[base]")
        filters.append(f"[base]{''.join(labels)}amix=inputs={len(labels) + 1}:duration=first:normalize=0,"
                       f"alimiter=limit=0.95,atrim=0:{total_duration:.3f}[out]")
        _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *inputs, "-filter_complex",
              ";".join(filters), "-map", "[out]", "-ar", str(MIX_RATE), "-ac", "1", str(track)])

    srt = _write_srt(project, episode, timeline)
    ass = _write_ass(project, episode, timeline, speakers, height)
    return {
        "episode": episode,
        "aligned_at": datetime.now().isoformat(timespec="seconds"),
        "total_duration": round(total_duration, 3),
        "timeline": timeline,
        "problems": problems,
        "track": str(track.relative_to(project)).replace("\\", "/") if track.exists() else "",
        "srt": str(srt.relative_to(project)).replace("\\", "/"),
        "ass": str(ass.relative_to(project)).replace("\\", "/"),
    }


def _write_srt(project: Path, episode: str, timeline: list[dict[str, Any]]) -> Path:
    path = project / "06_edit" / f"{episode}-auto.srt"
    path.parent.mkdir(parents=True, exist_ok=True)
    cues: list[str] = []
    index = 1
    for entry in timeline:
        for cue in entry.get("cues") or []:
            text = re.sub(r"[“”]", "", cue["text"]).strip()
            if not text:
                continue
            cues.append("%d\n%s --> %s\n%s\n" % (
                index, _srt_time(float(cue["start"])), _srt_time(float(cue["end"])), text))
            index += 1
    path.write_text("\n".join(cues), encoding="utf-8")
    return path


def _write_ass(project: Path, episode: str, timeline: list[dict[str, Any]],
               speakers: list[str], height: int) -> Path:
    """字幕版式：底部安全区 6%、字号 7%、按说话人左/中/右、情绪爆发镜头抬高并加大。"""
    path = project / "06_edit" / f"{episode}-auto.ass"
    path.parent.mkdir(parents=True, exist_ok=True)
    font_size = int(round(height * 0.07))
    margin_v = int(round(height * 0.06))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {int(height * 9 / 16)}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei,{font_size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,1,0,0,0,100,100,0,0,1,3,1,2,60,60,{margin_v},134

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    align_map = {"left": 1, "center": 2, "right": 3}
    lines: list[str] = [header]
    width = int(height * 9 / 16)
    for entry in timeline:
        speaker = entry.get("speaker", "")
        alignment = align_map[_speaker_position(speakers, speaker)]
        base_size = font_size
        base_margin = margin_v
        if entry.get("hot"):
            # 情绪爆发镜头：字幕抬高到 9% 安全区，字号 +8%
            base_size = int(round(font_size * 1.08))
            base_margin = int(round(height * 0.09))
        for cue in entry.get("cues") or []:
            text = re.sub(r"[“”]", "", cue["text"]).strip()
            if not text:
                continue
            size = base_size
            per_line = max(6, int(width * 0.94 / size))
            if len(text) > per_line * 2:
                size = max(28, int(width * 0.94 / (len(text) / 2 + 0.5)))
                per_line = max(6, int(width * 0.94 / size))
            # 均衡断行：避免最后一行只剩一个标点
            line_count = max(1, min(3, -(-len(text) // per_line)))
            chunk = -(-len(text) // line_count)
            chunks = [text[index:index + chunk] for index in range(0, len(text), chunk)]
            wrapped = "\\N".join(chunks[:2])
            override = "{\\an%d\\fs%d}" % (alignment, size)
            line = "Dialogue: 0,%s,%s,Default,%s,0,0,%d,,%s%s\n" % (
                _ass_time(float(cue["start"])), _ass_time(float(cue["end"])),
                speaker, base_margin, override, wrapped)
            lines.append(line)
    path.write_text("".join(lines), encoding="utf-8")
    return path


def write_reports(project: Path, result: dict[str, Any]) -> dict[str, Path]:
    project = Path(project)
    episode = result["episode"]
    qc_dir = project / "07_qc"
    qc_dir.mkdir(parents=True, exist_ok=True)

    timing_csv = qc_dir / f"{episode}-sync-timing.csv"
    with timing_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["镜号", "台词", "音频起点", "音频终点", "字幕入点", "字幕出点",
                         "画面起点", "画面时长", "嘴动窗口", "是否对齐", "备注"])
        for entry in result["timeline"]:
            window = entry.get("mouth_window")
            writer.writerow([
                entry["shot"], entry["text"] or "无台词",
                entry["audio_start"], entry["audio_end"],
                entry["sub_start"], entry["sub_end"],
                entry["picture_start"], entry["picture_duration"],
                ("%.2f–%.2f" % (window[0], window[1])) if window else "未检测到",
                "是" if entry["aligned"] else "否",
                entry.get("note", ""),
            ])

    qc_csv = qc_dir / f"{episode}-sync-report.csv"
    with qc_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["镜号", "问题描述", "修正建议"])
        if result["problems"]:
            for item in result["problems"]:
                writer.writerow([item["shot"], item["issue"], item["fix"]])
        else:
            writer.writerow(["全部镜头", "未发现口型/声音/字幕错位", "可进入人工复核"])

    (qc_dir / f"{episode}-sync.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"timing": timing_csv, "report": qc_csv}


def remux_final(project: Path, episode: str, burn_subtitles: bool = True) -> dict[str, str]:
    """把对齐后的音轨混进成片；可选把字幕烧进新文件。"""
    project = Path(project)
    ffmpeg = _ffmpeg()
    silent = project / "06_edit" / "exports" / f"{episode}-auto-final.mp4"
    track = project / "05_generation" / "audio" / f"{episode}-voice-track.wav"
    ass = project / "06_edit" / f"{episode}-auto.ass"
    outputs: dict[str, str] = {}
    if not silent.exists():
        return outputs
    plain = project / "06_edit" / "exports" / f"{episode}-auto-final-synced.mp4"
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(silent)]
    if track.exists():
        cmd += ["-i", str(track), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-ar", str(MIX_RATE), "-ac", "1", "-shortest"]
    else:
        cmd += ["-map", "0", "-c", "copy"]
    cmd += ["-movflags", "+faststart", str(plain)]
    _run(cmd)
    if plain.exists():
        outputs["synced"] = str(plain.relative_to(project)).replace("\\", "/")
    if burn_subtitles and ass.exists():
        burned = project / "06_edit" / "exports" / f"{episode}-auto-final-subtitled.mp4"
        escaped = str(ass).replace("\\", "/").replace(":", "\\:")
        sub_cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(plain),
                   "-vf", f"subtitles='{escaped}'", "-c:v", "libx264", "-preset", "medium",
                   "-crf", "20", "-c:a", "copy", "-movflags", "+faststart", str(burned)]
        _run(sub_cmd)
        if burned.exists():
            outputs["subtitled"] = str(burned.relative_to(project)).replace("\\", "/")
    return outputs
