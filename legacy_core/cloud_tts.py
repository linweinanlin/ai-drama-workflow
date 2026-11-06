"""云端 TTS（阿里云百炼 qwen-audio-3.1-tts-plus）：按角色绑定音色，逐句合成并下载。

凭据只从环境变量读取：DASHSCOPE_API_KEY / DASHSCOPE_WORKSPACE_ID，不写盘、不回显。
调用会产生少量费用（约 ¥1.4 / 万字符），因此只在人工点击确认后执行。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

DEFAULT_MODEL = "qwen-audio-3.1-tts-plus"
DEFAULT_REGION = "cn-beijing"
PRICE_PER_10K = 1.4

# 项目里验证过的音色；也可以直接填官方文档里的其它 voice id。
VOICE_CATALOG = {
    "xunanchuan_v3.1": "许南川 · 磁性自然男声",
    "longanlingxin_v3.1": "龙安灵心 · 温柔细腻女声",
    "longanfengyue_v3.1": "龙安风月 · 冷静知性女声",
}
DEFAULT_VOICE = "longanlingxin_v3.1"


def endpoint(workspace_id: str, region: str = DEFAULT_REGION) -> str:
    return (f"https://{workspace_id}.{region}.maas.aliyuncs.com/api/v1/services/"
            "audio/tts/SpeechSynthesizer")


def credentials() -> tuple[str, str]:
    key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    workspace = os.getenv("DASHSCOPE_WORKSPACE_ID", "").strip()
    if not key or not workspace:
        raise RuntimeError("缺少 DASHSCOPE_API_KEY 或 DASHSCOPE_WORKSPACE_ID 环境变量，无法调用云端 TTS。")
    return key, workspace


def billed_units(text: str) -> int:
    """中文按 2 个单位、其它按 1 个，与项目既有计费口径一致。"""
    return sum(2 if "\u4e00" <= char <= "\u9fff" else 1 for char in text)


def estimate_cost(rows: list[dict[str, Any]]) -> dict[str, Any]:
    units = sum(billed_units(str(row.get("text") or "")) for row in rows)
    return {"units": units, "cny": round(units / 10000 * PRICE_PER_10K, 5)}


def rate_multiplier(rate: Any) -> float:
    """rate 是倍率（0.5–2.0）；兼容旧的 SAPI 整数刻度（-10..10）。"""
    try:
        value = float(rate)
    except (TypeError, ValueError):
        return 1.0
    if value > 2.0:
        value = 1.0 + value * 0.05
    if value <= 0:
        return 1.0
    return max(0.5, min(2.0, value))


def build_payload(text: str, voice: str, rate: Any = 1.0, pitch: float = 1.0,
                  volume: int = 100, model: str = DEFAULT_MODEL,
                  sample_rate: int = 48000, seed: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "input": {
            "text": text,
            "voice": voice,
            "format": "wav",
            "sample_rate": sample_rate,
            "volume": int(volume),
            "pitch": max(0.5, min(2.0, float(pitch))),
            "language_hints": ["zh"],
        },
    }
    multiplier = rate_multiplier(rate)
    if abs(multiplier - 1.0) > 0.01:
        payload["input"]["rate"] = round(multiplier, 3)
    if seed is not None:
        payload["input"]["seed"] = int(seed)
    return payload


def synthesize(project: Path, rows: list[dict[str, Any]], config: dict[str, Any],
               progress=None) -> list[dict[str, Any]]:
    """逐句合成；失败只标记该句，不影响其它句子。返回更新后的 rows。"""
    key, workspace = credentials()
    region = str(config.get("region") or DEFAULT_REGION)
    model = str(config.get("tts_model") or DEFAULT_MODEL)
    url_endpoint = endpoint(workspace, region)
    target_dir = Path(project) / "05_generation" / "audio" / "voice"
    target_dir.mkdir(parents=True, exist_ok=True)

    for index, row in enumerate(rows, start=1):
        text = str(row.get("text") or "")
        if not text:
            continue
        voice = str(row.get("voice") or DEFAULT_VOICE)
        if progress:
            progress(f"云端配音 {index}/{len(rows)}：{row.get('shot_id')} · {voice}")
        ticks = float(row.get("rate") or 0)
        payload = build_payload(text, voice, rate=1.0 + ticks * 0.05, seed=index + 100)
        try:
            response = requests.post(
                url_endpoint,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload, timeout=120,
            )
            data = response.json() if response.content else {}
            audio = (data.get("output") or {}).get("audio") or {}
            url = audio.get("url", "")
            if response.status_code != 200 or not url:
                raise RuntimeError(f"HTTP {response.status_code}: {json.dumps(data, ensure_ascii=False)[:400]}")
            target = target_dir / f"{row['shot_id']}-cloud.wav"
            with requests.get(url, stream=True, timeout=180) as download:
                download.raise_for_status()
                with target.open("wb") as handle:
                    for chunk in download.iter_content(1024 * 256):
                        if chunk:
                            handle.write(chunk)
            row.update({
                "provider": "dashscope",
                "voice": voice,
                "audio_path": str(target.relative_to(project)).replace("\\", "/"),
                "status": "已生成",
                "request_id": str(data.get("request_id", "")),
                "cost_note": f"{billed_units(text)} units ≈ ¥{billed_units(text) / 10000 * PRICE_PER_10K:.5f}",
                "notes": f"云端 TTS {model} · {VOICE_CATALOG.get(voice, voice)}",
            })
        except Exception as exc:  # noqa: BLE001
            row.update({
                "provider": "dashscope",
                "status": "失败",
                "notes": f"云端配音失败：{type(exc).__name__}: {exc}"[:300],
            })
    return rows


def probe_voice(voice: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """用 1 个字的极短文本验证音色可用（约 ¥0.0003），返回状态与音频路径。"""
    config = config or {}
    key, workspace = credentials()
    payload = build_payload("嗯", voice, seed=1)
    response = requests.post(
        endpoint(workspace, str(config.get("region") or DEFAULT_REGION)),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload, timeout=120,
    )
    data = response.json() if response.content else {}
    audio = (data.get("output") or {}).get("audio") or {}
    return {
        "ok": response.status_code == 200 and bool(audio.get("url")),
        "http": response.status_code,
        "voice": voice,
        "label": VOICE_CATALOG.get(voice, ""),
        "url": audio.get("url", ""),
        "response": json.dumps(data, ensure_ascii=False)[:400] if not audio.get("url") else "ok",
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
