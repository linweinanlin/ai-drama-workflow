"""配音引擎：外部 TTS 路线（离线 SAPI）生成逐镜配音、按镜头对齐、登记与质检。

两条路线：
- native：视频模型自带音轨（请求 audio=true），这里只做登记。
- external_tts：视频静音生成，配音单独合成后按镜头时长对齐，合片时混入。
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
import cloud_tts
import qc_writer

VOICE_FIELDS = [
    "shot_id", "episode", "route", "provider", "speaker", "text", "emotion", "rate", "voice",
    "voice_label", "audio_path", "align", "status", "cost_note", "created_at", "notes",
]
DEFAULT_VOICES = {"default": "Microsoft Huihui Desktop"}
DEFAULT_RATES = {"林夏": 1, "周沉": -2, "许曼": 0, "陈默": -1}
DEFAULT_CLOUD_VOICES = {
    "林夏": "longanlingxin_v3.1",
    "周沉": "xunanchuan_v3.1",
    "许曼": "longanfengyue_v3.1",
    "陈默": "xunanchuan_v3.1",
    "旁白": "xunanchuan_v3.1",
    "narrator": "xunanchuan_v3.1",
}


SAPI_SCRIPT = r"""param(
    [Parameter(Mandatory = $true)][string]$ManifestPath,
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech

$manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer

try {
    foreach ($item in $manifest.items) {
        $target = Join-Path $OutputDirectory $item.file
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
        $synth.SelectVoice([string]$item.voice)
        $synth.Rate = [int]$item.rate
        $synth.Volume = [int]$item.volume
        $synth.SetOutputToWaveFile($target)
        $synth.Speak([string]$item.text)
        $synth.SetOutputToNull()
        Write-Output $target
    }
}
finally {
    $synth.Dispose()
}
"""


def ensure_tts_script(project: Path) -> Path:
    """项目里没有 sapi_tts.ps1 时自动补齐，保证离线配音可用。"""
    path = Path(project) / "09_automation" / "sapi_tts.ps1"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SAPI_SCRIPT, encoding="utf-8-sig")
    return path


def audio_dir(project: Path) -> Path:
    target = project / "05_generation" / "audio"
    target.mkdir(parents=True, exist_ok=True)
    return target


def ledger_path(project: Path, episode: str) -> Path:
    return audio_dir(project) / f"{episode}-voice-jobs.csv"


def load_ledger(project: Path, episode: str) -> dict[str, dict[str, str]]:
    path = ledger_path(project, episode)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {row.get("shot_id", ""): row for row in csv.DictReader(handle) if row.get("shot_id")}


def _dialogue(shot: dict[str, str]) -> str:
    for key in ("dialogue_text", "dialogue_or_vo"):
        text = (shot.get(key) or "").strip()
        if text and text != "无台词":
            return text
    return ""


def _speaker(shot: dict[str, str]) -> str:
    speaker = (shot.get("speaker") or "").strip()
    if speaker and speaker not in {"无", "无台词"}:
        return speaker
    people = [name.strip() for name in str(shot.get("characters") or "").split("|") if name.strip()]
    return people[0] if people else "旁白"


def provider_of(config: dict[str, Any]) -> str:
    return str(config.get("tts_provider") or "sapi").lower()


def _voice_for(speaker: str, config: dict[str, Any]) -> str:
    mapping = config.get("voice_map") or {}
    if isinstance(mapping, dict) and speaker in mapping:
        value = mapping[speaker]
        if isinstance(value, dict):
            value = value.get("voice", "")
        if value:
            return str(value)
    if provider_of(config) == "dashscope":
        # 配置里的 default_voice 可能是 SAPI 名称（如 Microsoft Huihui Desktop），
        # 直接传给云端会 411；这里做一次合法性兜底。
        configured = str(config.get("default_voice") or "").strip()
        if configured and not configured.lower().startswith("microsoft") and "_v" in configured:
            fallback = configured
        else:
            fallback = cloud_tts.DEFAULT_VOICE
        return DEFAULT_CLOUD_VOICES.get(speaker) or fallback
    return str(config.get("default_voice") or DEFAULT_VOICES["default"])


def voice_label(voice: str) -> str:
    return cloud_tts.VOICE_CATALOG.get(voice, "")


def estimate(shots: list[dict[str, str]], config: dict[str, Any]) -> dict[str, Any]:
    rows = plan(Path("."), "EP000", shots, config)
    spoken = [row for row in rows if row.get("text")]
    return cloud_tts.estimate_cost(spoken)


def _rate_for(speaker: str, config: dict[str, Any]) -> int:
    mapping = config.get("voice_rate") or DEFAULT_RATES
    try:
        return int(mapping.get(speaker, 0))
    except (TypeError, ValueError):
        return 0


def plan(project: Path, episode: str, shots: list[dict[str, str]], config: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for shot in shots:
        text = _dialogue(shot)
        speaker = _speaker(shot) if text else "无"
        rows.append({
            "shot_id": shot.get("shot_id", ""),
            "episode": episode,
            "route": "external_tts",
            "provider": provider_of(config),
            "speaker": speaker,
            "text": text,
            "emotion": shot.get("emotion_line") or shot.get("emotion") or "",
            "rate": _rate_for(speaker, config) if text else 0,
            "voice": _voice_for(speaker, config) if text else "",
            "voice_label": voice_label(_voice_for(speaker, config)) if text else "",
            "audio_path": "",
            "align": "",
            "status": "待合成" if text else "无台词",
            "notes": "",
        })
    return rows


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=0x08000000 if __import__("os").name == "nt" else 0)


def _duration(path: Path) -> float:
    ffmpeg = batch_video_engine.find_ffmpeg()
    probe = str(Path(ffmpeg).with_name("ffprobe.exe")) if ffmpeg else (shutil.which("ffprobe") or "")
    if not probe:
        return 0.0
    result = _run([probe, "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    try:
        return float((result.stdout or "0").strip())
    except ValueError:
        return 0.0


def synthesize(project: Path, episode: str, shots: list[dict[str, str]], config: dict[str, Any],
               progress=None) -> dict[str, Any]:
    """生成逐镜配音并写台账。返回 {rows, track, seconds}。"""
    rows = plan(project, episode, shots, config)
    spoken = [row for row in rows if row["text"]]
    target_dir = audio_dir(project) / "voice"
    target_dir.mkdir(parents=True, exist_ok=True)
    track = audio_dir(project) / f"{episode}-voice-track.wav"

    provider = provider_of(config)
    if spoken and provider == "dashscope":
        if progress:
            progress(f"云端配音：{len(spoken)} 句（按角色音色）")
        cloud_tts.synthesize(project, spoken, config, progress)
    elif spoken:
        script = ensure_tts_script(project)
        manifest_path = audio_dir(project) / f"{episode}-tts-manifest.json"
        manifest = {"items": []}
        for row in spoken:
            manifest["items"].append({
                "file": f"{row['shot_id']}.wav",
                "voice": row["voice"],
                "rate": row["rate"],
                "volume": 100,
                "text": row["text"],
            })
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if progress:
            progress(f"离线合成配音：{len(spoken)} 句")
        result = _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                       "-ManifestPath", str(manifest_path), "-OutputDirectory", str(target_dir)])
        if result.returncode != 0:
            raise RuntimeError(f"TTS 失败：{(result.stderr or result.stdout or '')[-600:]}")

    for row in rows:
        if not row["text"]:
            row["status"] = "无台词"
            row["notes"] = "无台词镜头，不生成人声"
            continue
        if row.get("status") == "失败":
            continue
        raw = project / row["audio_path"] if row.get("audio_path") else target_dir / f"{row['shot_id']}.wav"
        if not raw.exists():
            row["status"] = "失败"
            row["notes"] = "未生成音频"
            continue
        shot = next((item for item in shots if item.get("shot_id") == row["shot_id"]), {})
        try:
            target_seconds = float(shot.get("duration_s") or 5)
        except (TypeError, ValueError):
            target_seconds = 5.0
        source_seconds = _duration(raw) or target_seconds
        ratio = max(1.0, source_seconds / max(0.5, target_seconds - 0.15))
        fitted = target_dir / f"{row['shot_id']}-fit.wav"
        ffmpeg = batch_video_engine.find_ffmpeg()
        filters = f"atempo={min(ratio, 2.0):.3f},apad=whole_dur={target_seconds:.3f},atrim=0:{target_seconds:.3f}"
        result = _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw),
                       "-af", filters, "-ar", "48000", "-ac", "1", str(fitted)])
        if result.returncode != 0 or not fitted.exists():
            row["status"] = "失败"
            row["notes"] = "对齐失败"
            continue
        row["audio_path"] = str(fitted.relative_to(project)).replace("\\", "/")
        row["align"] = f"按镜头 {target_seconds:.1f}s 对齐（atempo {min(ratio, 2.0):.2f}）"
        row["status"] = "已完成"
        if provider == "dashscope" and not row.get("cost_note"):
            units = cloud_tts.billed_units(row["text"])
            row["cost_note"] = f"{units} units ≈ ¥{units / 10000 * cloud_tts.PRICE_PER_10K:.5f}"

    _write_ledger(project, episode, rows)
    written = build_track(project, episode, shots, rows)
    return {"rows": rows, "track": str(written) if written else "", "seconds": _duration(written) if written else 0.0}


def _write_ledger(project: Path, episode: str, rows: list[dict[str, Any]]) -> Path:
    path = ledger_path(project, episode)
    stamp = datetime.now().isoformat(timespec="seconds")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=VOICE_FIELDS)
        writer.writeheader()
        for row in rows:
            item = {key: row.get(key, "") for key in VOICE_FIELDS}
            item["created_at"] = item.get("created_at") or stamp
            writer.writerow(item)
    return path


def build_track(project: Path, episode: str, shots: list[dict[str, str]], rows: list[dict[str, Any]]) -> Path | None:
    """按镜头起止时间把逐句配音铺成一条整集音轨。"""
    ffmpeg = batch_video_engine.find_ffmpeg()
    if not ffmpeg:
        return None
    parts: list[str] = []
    inputs: list[str] = []
    filters: list[str] = []
    cursor = 0.0
    index = 0
    usable = 0
    for shot in shots:
        duration = float(shot.get("duration_s") or 5)
        row = next((item for item in rows if item["shot_id"] == shot.get("shot_id")), None)
        if row and row.get("audio_path"):
            path = project / row["audio_path"]
            if path.exists():
                inputs.extend(["-i", str(path)])
                delay = int(cursor * 1000)
                filters.append(f"[{index}:a]adelay={delay}|{delay}[a{index}]")
                parts.append(f"[a{index}]")
                usable += 1
                index += 1
        cursor += duration
    if not usable:
        return None
    total = cursor
    filters.append(f"anullsrc=channel_layout=mono:sample_rate=48000:d={total:.3f}[base]")
    filters.append(f"[base]{''.join(parts)}amix=inputs={usable + 1}:duration=first:normalize=0,"
                   f"alimiter=limit=0.95,atrim=0:{total:.3f}[out]")
    track = audio_dir(project) / f"{episode}-voice-track.wav"
    result = _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *inputs,
                   "-filter_complex", ";".join(filters), "-map", "[out]",
                   "-ar", "48000", "-ac", "1", str(track)])
    return track if result.returncode == 0 and track.exists() else None


def qc(project: Path, episode: str, shots: list[dict[str, str]]) -> dict[str, Any]:
    """配音质检：台词逐字一致、音频存在、时长不超镜、无台词镜头不出现人声。"""
    rows = load_ledger(project, episode)
    checks: list[dict[str, Any]] = []
    for shot in shots:
        shot_id = shot.get("shot_id", "")
        row = rows.get(shot_id, {})
        expected = _dialogue(shot)
        recorded = (row.get("text") or "").strip()
        audio = project / (row.get("audio_path") or "")
        ok_text = (expected == recorded) if expected else (recorded in ("", "无台词"))
        duration_ok = True
        if audio and audio.exists() and expected:
            duration_ok = audio_seconds_ok(project, row, shot)
        checks.append({
            "shot_id": shot_id,
            "expected_text": expected,
            "recorded_text": recorded,
            "text_match": ok_text,
            "audio_exists": bool(audio and audio.exists()),
            "duration_ok": duration_ok,
            "status": row.get("status", "未合成"),
            "align": row.get("align", ""),
        })
    passed = sum(1 for item in checks if item["text_match"] and (item["audio_exists"] or not item["expected_text"]))
    voices: dict[str, set[str]] = {}
    for row in rows.values():
        if row.get("speaker") and row.get("voice"):
            voices.setdefault(row["speaker"], set()).add(row["voice"])
    consistent = all(len(names) == 1 for names in voices.values())
    report = {
        "episode": episode,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "shots": checks,
        "passed": passed,
        "total": len(checks),
        "voice_map": {speaker: sorted(names) for speaker, names in voices.items()},
        "voice_consistent": consistent,
        "manual_items": [
            {"name": "口型与对白逐句对齐", "done": False, "note": "需要人工看回放确认"},
            {"name": "表演与情绪贴合台词", "done": False, "note": "人工审"},
        ],
    }
    qc_writer.append_voice_section(project, episode, report)
    (audio_dir(project) / f"{episode}-voice-qc.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def audio_seconds_ok(project: Path, row: dict[str, str], shot: dict[str, str]) -> bool:
    path = project / (row.get("audio_path") or "")
    try:
        limit = float(shot.get("duration_s") or 5) + 0.05
    except (TypeError, ValueError):
        limit = 5.05
    return _duration(path) <= limit
