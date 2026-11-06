"""质检引擎：逐镜自动检查 + 人工结论 + 交付登记。

只读本地文件（视频/状态/配音台账/镜头表），不调用任何付费接口；
自动检查结果写 07_qc/<集>-qc-auto.json，人工结论写 07_qc/<集>-qc-review.json，
交付登记追加到 08_release/release-manifest.csv。
"""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import batch_video_engine

REVIEW_FIELDS = ["shot_id", "verdict", "note", "updated_at"]
VERDICTS = ("通过", "待审", "返工")


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=0x08000000)


def _probe(path: Path) -> dict[str, Any]:
    ffmpeg = batch_video_engine.find_ffmpeg()
    probe = str(Path(ffmpeg).with_name("ffprobe.exe")) if ffmpeg else (shutil.which("ffprobe") or "")
    if not probe or not path.exists():
        return {}
    result = _run([probe, "-v", "error", "-show_entries",
                   "format=duration:stream=codec_type,width,height", "-of", "json", str(path)])
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def _duration(info: dict[str, Any]) -> float:
    try:
        return float((info.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def _has_stream(info: dict[str, Any], kind: str) -> bool:
    return any(stream.get("codec_type") == kind for stream in info.get("streams", []))


def load_voice(project: Path, episode: str) -> dict[str, dict[str, str]]:
    path = project / "05_generation" / "audio" / f"{episode}-voice-jobs.csv"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {row.get("shot_id", ""): row for row in csv.DictReader(handle) if row.get("shot_id")}


def load_review(project: Path, episode: str) -> dict[str, dict[str, str]]:
    path = project / "07_qc" / f"{episode}-qc-review.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data.get("shots", {}) if isinstance(data, dict) else {}


def save_verdict(project: Path, episode: str, shot_id: str, verdict: str, note: str = "") -> Path:
    path = project / "07_qc" / f"{episode}-qc-review.json"
    data = {"schema_version": "1.0", "episode": episode, "shots": {}}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(existing, dict) and isinstance(existing.get("shots"), dict):
                data = existing
        except (OSError, json.JSONDecodeError):
            pass
    data.setdefault("shots", {})[shot_id] = {
        "verdict": verdict,
        "note": note,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _dialogue(shot: dict[str, str]) -> str:
    for key in ("dialogue_text", "dialogue_or_vo"):
        text = (shot.get(key) or "").strip()
        if text and text != "无台词":
            return text
    return ""


def _resolve(project: Path, value: str, config: dict[str, Any]) -> str:
    assets = config.get("assets") or {}
    for item in str(value or "").split("|"):
        item = item.strip()
        if not item:
            continue
        if item.startswith("@"):
            item = str(assets.get(item[1:], ""))
        if item and (project / item).exists():
            return item
    return ""


def requested_duration(project: Path, episode: str, shot_id: str) -> float | None:
    """读取生成时提交的镜头时长（video-state 里记录），用于判断实际视频是否跑偏。"""
    path = Path(project) / "09_automation" / "video-state" / episode / f"{shot_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    usage = ((data.get("response") or {}).get("usage") or {})
    for key in ("duration", "output_video_duration"):
        try:
            value = float(usage.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return round(value, 2)
    return None


def actual_duration(project: Path, episode: str, shot_id: str) -> float | None:
    video = Path(project) / "05_generation" / "videos" / episode / f"{shot_id}.mp4"
    if not video.exists():
        return None
    info = _probe(video)
    value = _duration(info)
    return round(value, 2) if value > 0 else None


def auto_checks(project: Path, episode: str, shots: list[dict[str, str]],
                config: dict[str, Any]) -> dict[str, Any]:
    """逐镜检查：视频/时长/配音/台词/帧链/场景/成片，全部只读本地文件。"""
    project = Path(project)
    voice = load_voice(project, episode)
    route = str(config.get("voice_route") or "native")
    plan = batch_video_engine.plan_durations(shots, config)
    rows: list[dict[str, Any]] = []
    for shot in shots:
        shot_id = shot.get("shot_id", "")
        video = project / "05_generation" / "videos" / episode / f"{shot_id}.mp4"
        info = _probe(video)
        video_ok = video.exists() and _duration(info) > 0
        actual = round(_duration(info), 2)
        recorded = requested_duration(project, episode, shot_id)
        if recorded:
            planned = recorded
            tolerance = 0.35
        else:
            try:
                planned = float(shot.get("duration_s") or 0) or plan.get(shot_id, 0)
            except (TypeError, ValueError):
                planned = plan.get(shot_id, 0)
            tolerance = 1.0
        duration_ok = video_ok and abs(actual - planned) <= tolerance
        text = _dialogue(shot)
        entry = voice.get(shot_id, {})
        if text:
            if route == "external_tts":
                voice_ok = entry.get("status") == "已完成" and bool(entry.get("audio_path"))
            else:
                voice_ok = _has_stream(info, "audio")
            text_ok = (entry.get("text", "").strip() == text) if entry else (not text)
        else:
            voice_ok = True
            text_ok = entry.get("status") in ("", "无台词")
        frame_ref = _resolve(project, shot.get("prev_last_frame", ""), config)
        frame_ok = bool(frame_ref) or shot is shots[0]
        scene_ref = _resolve(project, shot.get("scene_refs", ""), config)
        scene_ok = bool(scene_ref)
        rows.append({
            "shot_id": shot_id,
            "video": video_ok,
            "duration": actual,
            "planned": planned,
            "recorded": recorded,
            "duration_ok": duration_ok,
            "voice": voice_ok,
            "voice_status": entry.get("status", "无"),
            "voice_provider": entry.get("provider", ""),
            "text": text,
            "text_ok": text_ok,
            "frame_ok": frame_ok,
            "frame_ref": frame_ref,
            "scene_ok": scene_ok,
            "scene_ref": scene_ref,
            # 帧链/场景在旧版剧集定义里本来就没有字段：只算“待补充”，不算失败
            "ok": video_ok and duration_ok and voice_ok and text_ok,
        })
    sync_path = project / "07_qc" / f"{episode}-sync.json"
    sync_data = {}
    if sync_path.exists():
        try:
            sync_data = json.loads(sync_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            sync_data = {}
    sync_map = {item.get("shot", ""): item for item in sync_data.get("timeline", [])}
    for row in rows:
        item = sync_map.get(row["shot_id"], {})
        offset = item.get("lip_offset_ms")
        sub_end = item.get("sub_end")
        picture_end = None
        if item:
            try:
                picture_end = float(item["picture_start"]) + float(item["picture_duration"])
            except (KeyError, TypeError, ValueError):
                picture_end = None
        residue = bool(sub_end and picture_end and float(sub_end) > picture_end - 0.03)
        row.update({
            "sync_offset_ms": offset,
            "sync_aligned": item.get("aligned"),
            "sub_residue": residue,
            "sub_start": item.get("sub_start"),
            "sub_end": item.get("sub_end"),
        })

    final = project / "06_edit" / "exports" / f"{episode}-auto-final.mp4"
    final_info = _probe(final)
    result = {
        "schema_version": "1.0",
        "episode": episode,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "route": route,
        "shots": rows,
        "final": {
            "path": str(final.relative_to(project)).replace("\\", "/") if final.exists() else "",
            "exists": final.exists(),
            "duration": round(_duration(final_info), 2),
            "has_audio": _has_stream(final_info, "audio"),
        },
        "summary": {
            "shots": len(rows),
            "passed": sum(1 for row in rows if row["ok"]),
            "video_missing": sum(1 for row in rows if not row["video"]),
            "duration_off": sum(1 for row in rows if row["video"] and not row["duration_ok"]),
            "voice_failed": sum(1 for row in rows if not row["voice"]),
            "text_mismatch": sum(1 for row in rows if not row["text_ok"]),
            "frame_missing": sum(1 for row in rows if not row["frame_ok"]),
            "scene_missing": sum(1 for row in rows if not row["scene_ok"]),
            "core_failed": sum(1 for row in rows if not row["ok"]),
            "sync_offset_over": sum(1 for row in rows
                                    if isinstance(row.get("sync_offset_ms"), int) and row["sync_offset_ms"] > 300),
            "subtitle_residue": sum(1 for row in rows if row.get("sub_residue")),
            "synced": bool(sync_data),
            "sync_cues": sum(1 for row in rows if row.get("sub_start")),
        },
    }
    return result


def write_auto(project: Path, result: dict[str, Any]) -> Path:
    path = Path(project) / "07_qc" / f"{result['episode']}-qc-auto.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _append_markdown(project, result)
    return path


def _append_markdown(project: Path, result: dict[str, Any]) -> None:
    episode = result["episode"]
    path = Path(project) / "07_qc" / f"{episode}-qc.md"
    begin, end = "<!-- AUTO-QC:BEGIN -->", "<!-- AUTO-QC:END -->"
    summary = result["summary"]
    final = result["final"]
    lines = [
        begin,
        "## 自动质检（本地文件核对）",
        "",
        f"- 检查时间：{result['checked_at']}",
        f"- 镜头通过：{summary['passed']}/{summary['shots']}",
        f"- 缺失视频：{summary['video_missing']}　时长不符：{summary['duration_off']}",
        f"- 配音失败：{summary['voice_failed']}　台词不一致：{summary['text_mismatch']}",
        f"- 帧链缺失：{summary['frame_missing']}　场景参考缺失：{summary['scene_missing']}",
        f"- 成片：{'有' if final['exists'] else '无'}"
        + (f"（{final['duration']} 秒，{'含音轨' if final['has_audio'] else '无音轨'}）" if final["exists"] else ""),
        end,
    ]
    text = path.read_text(encoding="utf-8-sig") if path.exists() else f"# {episode} 质检单\n"
    block = "\n".join(lines)
    if begin in text and end in text:
        head = text.split(begin)[0]
        tail = text.split(end, 1)[1]
        text = head + block + tail
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    path.write_text(text, encoding="utf-8")


def release_rows(project: Path) -> list[dict[str, str]]:
    path = Path(project) / "08_release" / "release-manifest.csv"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def register_release(project: Path, episode: str, fields: dict[str, str]) -> Path:
    project = Path(project)
    path = project / "08_release" / "release-manifest.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["episode", "platform", "version", "video_path", "cover_path", "title", "description",
                  "hashtags", "published_at", "post_url", "views_24h", "retention_3s", "completion_rate",
                  "engagement_rate", "follows_or_conversion", "notes"]
    rows: list[dict[str, str]] = []
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames or fieldnames
            rows = list(reader)
    sync_path = project / "07_qc" / f"{episode}-sync.json"
    sync_data = {}
    if sync_path.exists():
        try:
            sync_data = json.loads(sync_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            sync_data = {}
    sync_map = {item.get("shot", ""): item for item in sync_data.get("timeline", [])}
    for row in rows:
        item = sync_map.get(row["shot_id"], {})
        offset = item.get("lip_offset_ms")
        sub_end = item.get("sub_end")
        picture_end = None
        if item:
            try:
                picture_end = float(item["picture_start"]) + float(item["picture_duration"])
            except (KeyError, TypeError, ValueError):
                picture_end = None
        residue = bool(sub_end and picture_end and float(sub_end) > picture_end - 0.03)
        row.update({
            "sync_offset_ms": offset,
            "sync_aligned": item.get("aligned"),
            "sub_residue": residue,
            "sub_start": item.get("sub_start"),
            "sub_end": item.get("sub_end"),
        })

    final = project / "06_edit" / "exports" / f"{episode}-auto-final.mp4"
    row = {name: "" for name in fieldnames}
    row.update({
        "episode": episode,
        "platform": fields.get("platform", ""),
        "version": fields.get("version", ""),
        "video_path": str(final.relative_to(project)).replace("\\", "/") if final.exists() else "",
        "cover_path": fields.get("cover_path", ""),
        "title": fields.get("title", ""),
        "description": fields.get("description", ""),
        "hashtags": fields.get("hashtags", ""),
        "published_at": fields.get("published_at", ""),
        "notes": fields.get("notes", ""),
    })
    rows = [item for item in rows if not (item.get("episode") == episode and item.get("version") == row["version"])]
    rows.append(row)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path
