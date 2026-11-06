from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import threading

import requests


if sys.stdout is None:
    try:
        sys.stdout = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    except OSError:
        pass
if sys.stderr is None:
    try:
        sys.stderr = os.fdopen(os.dup(2), "w", encoding="utf-8", buffering=1)
    except OSError:
        pass
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"}
ACTIVE = {"PENDING", "RUNNING", "PROCESSING", "QUEUED"}
JOB_FIELDS = [
    "job_id", "episode", "shot_id", "stage", "provider", "model_id", "prediction_id", "status",
    "submitted_at", "last_checked_at", "source_paths", "output_url", "local_path", "cost_note",
    "retry_of", "review_result", "notes",
]
STYLE_PRESETS = {
    "都市悬疑真人电影": "真人电影化都市悬疑，低饱和青灰调，克制运镜，真实皮肤与布料，冷暖光对比，紧张但不浮夸。",
    "都市爱情清透": "真人都市爱情短剧，清透自然肤色，柔和日光，精致生活场景，情绪细腻，镜头轻盈。",
    "二维动态漫": "高质量二维动态漫画，线稿稳定，赛璐璐上色，角色造型统一，分层景深与自然镜头运动。",
    "国风水墨漫": "东方国风水墨动画，细腻工笔人物，宣纸肌理，含蓄色彩，飘逸但保持角色一致。",
    "古装电影质感": "真人古装电影质感，考究服化道，柔和自然光，东方构图，真实织物与环境细节。",
    "赛博科幻": "电影级赛博科幻，霓虹冷光，未来城市材质，强空间层次，人物面部保持真实稳定。",
    "温暖家庭现实主义": "真人家庭现实主义，温暖自然光，生活化陈设，克制表演，手持感轻微且稳定。",
}
NEGATIVE_LIBRARY = (
    "禁止：低质量、画面残缺、多余手指、肢体畸形、人物变形、面部崩坏、镜像反转、"
    "物体凭空出现或消失、道具数量变化、光线方向矛盾、画面乱码文字、时代与商标穿帮、"
    "身份漂移、人物瞬移、水印与字幕板。"
)


def find_ffmpeg() -> str:
    """从 PATH、常见安装目录和桌面寻找 ffmpeg。

    从资源管理器启动的窗口程序不一定继承完整 PATH，因此不能只依赖 shutil.which。
    """
    candidates: list[Path] = []
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(Path(found))
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        programs = Path(local_appdata) / "Programs" / "ffmpeg"
        if programs.exists():
            candidates.extend(sorted(programs.glob("*/bin/ffmpeg.exe"), reverse=True))
    candidates.extend([
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"),
        Path.home() / "Desktop" / "ffmpeg.exe",
        Path.home() / "ffmpeg.exe",
    ])
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return ""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {} if default is None else default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def max_shot_limit(config: dict[str, Any]) -> int:
    """单镜时长上限：实测 4–8 秒可提交、9 秒被拒，默认 8，可在配置里覆盖。"""
    try:
        value = int(config.get("max_shot_duration_seconds") or 8)
    except (TypeError, ValueError):
        value = 8
    return max(2, min(30, value))


def min_shot_limit(config: dict[str, Any]) -> int:
    """单镜时长下限：4 秒是实测可用的底线，2–3 秒未验证；默认 4，可下调。"""
    try:
        value = int(config.get("min_shot_duration_seconds") or 4)
    except (TypeError, ValueError):
        value = 4
    return max(2, min(30, value))


def plan_durations(shots: list[dict[str, str]], config: dict[str, Any]) -> dict[str, int]:
    """把「目标总时长」按镜头占比分配成逐镜秒数（2 ~ 上限）。界面与执行共用同一套算法。"""
    if not shots:
        return {}
    limit = max_shot_limit(config)
    floor = min(limit, min_shot_limit(config))
    bases = [max(0.1, float(shot.get("duration_s") or 5)) for shot in shots]
    natural_total = max(1, int(round(sum(bases))))
    try:
        requested = int(config.get("episode_duration_seconds") or natural_total)
    except (TypeError, ValueError):
        requested = natural_total
    target = max(floor * len(shots), min(limit * len(shots), requested))
    raw = [target * value / sum(bases) for value in bases]
    values = [max(floor, min(limit, int(value))) for value in raw]
    while sum(values) < target:
        candidates = [index for index, value in enumerate(values) if value < limit]
        if not candidates:
            break
        index = max(candidates, key=lambda item: raw[item] - values[item])
        values[index] += 1
    while sum(values) > target:
        candidates = [index for index, value in enumerate(values) if value > floor]
        if not candidates:
            break
        index = max(candidates, key=lambda item: values[item] - raw[item])
        values[index] -= 1
    return {shot["shot_id"]: values[index] for index, shot in enumerate(shots)}


class BatchEngine:
    def __init__(self, project: Path, episode: str, interval: int = 15) -> None:
        self.project = project.resolve()
        self.episode = episode.upper()
        self.interval = max(5, interval)
        self.auto = self.project / "09_automation"
        self.config_path = self.auto / "video-provider.json"
        self.config = read_json(self.config_path, {})
        legacy_config = read_json(self.auto / "aliyun-wan3-config.json", {})
        if not self.config.get("assets") and legacy_config.get("assets"):
            self.config["assets"] = legacy_config["assets"]
        self.config["api_key"] = os.getenv("DASHSCOPE_API_KEY", "").strip()
        self.config["workspace_id"] = os.getenv("DASHSCOPE_WORKSPACE_ID", "").strip()
        self.state_dir = self.auto / "video-state" / self.episode
        self.request_dir = self.auto / "video-requests" / self.episode
        self.output_dir = self.project / "05_generation" / "videos" / self.episode
        self.upload_cache = self.auto / "upload-cache.json"
        self.ledger = self.project / "05_generation" / "generation-jobs.csv"
        self.shots = self._load_shots()
        self.duration_by_shot = self._duration_plan()
        self.ffmpeg = find_ffmpeg()
        self.voice_route = str(self.config.get("voice_route") or "native")
        self.max_concurrent = max(1, min(6, int(self.config.get("max_concurrent") or 1)))
        self.max_prompt_chars = max(300, int(self.config.get("max_prompt_chars") or 900))
        self._ledger_lock = threading.Lock()
        self._cache_model_profile()
        self.performance_blocks: dict[str, str] = {}
        self.performance_locks: dict[str, str] = {}
        self._load_performance()

    def _load_performance(self) -> None:
        """读取 04_assets/performance/<集>-performance.json（口型/表情/锁定后缀）。"""
        path = self.project / "04_assets" / "performance" / f"{self.episode}-performance.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return
        for item in data.get("shots", []):
            shot_id = item.get("shot")
            if not shot_id:
                continue
            block = str(item.get("prompt_block") or "").strip()
            if block:
                self.performance_blocks[shot_id] = block
            lock = str(item.get("lock") or "").strip()
            if lock:
                self.performance_locks[shot_id] = lock

    def _cache_model_profile(self) -> None:
        """模型能力档案只解析一次：按内容指纹缓存到 model-profile.cache.json。"""
        source = self.project / "05_generation" / "model-profile.md"
        cache = self.project / "05_generation" / "model-profile.cache.json"
        if not source.exists():
            return
        try:
            text = source.read_text(encoding="utf-8-sig")
        except OSError:
            return
        digest = str(abs(hash(text)))
        if cache.exists():
            cached = read_json(cache, {})
            if cached.get("digest") == digest:
                return
        sections = [line.strip("# ").strip() for line in text.splitlines() if line.startswith("#")]
        cache.write_text(json.dumps({
            "digest": digest,
            "chars": len(text),
            "sections": sections,
            "updated_at": now_iso(),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _duration_plan(self) -> dict[str, int]:
        self.max_shot_duration = max_shot_limit(self.config)
        return plan_durations(self.shots, self.config)

    def _load_shots(self) -> list[dict[str, str]]:
        path = self.project / "03_storyboard" / f"{self.episode}-shotlist.csv"
        if path.exists():
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        else:
            legacy_path = self.auto / "episodes" / f"{self.episode}.json"
            legacy = read_json(legacy_path, {})
            rows = []
            for source in legacy.get("shots", []):
                asset_key = str(source.get("asset_key", ""))
                asset_path = str(self.config.get("assets", {}).get(asset_key, ""))
                rows.append({
                    "episode": self.episode, "shot_id": str(source.get("shot_id", "")),
                    "order": str(source.get("order", "")), "duration_s": str(source.get("duration", 5)),
                    "scene_id": "", "action": "根据本镜台词与剧情自然表演，保持连续短剧节奏",
                    "dialogue_text": str(source.get("text", "")),
                    "dialogue_or_vo": str(source.get("text", "")),
                    "speaker": str(source.get("speaker_label") or source.get("speaker") or ""),
                    "identity_refs": asset_path if asset_key != "scene" else "",
                    "scene_refs": asset_path if asset_key == "scene" else "",
                    "prop_refs": "", "prev_last_frame": "", "next_first_frame": "",
                    "start_state": "承接上一镜末态", "end_state": "形成下一镜可见承接点",
                    "emotion": "随剧情自然变化", "emotion_arc": "连续过渡", "emotion_line": "真实、克制、有呼吸感",
                })
            if not rows:
                raise RuntimeError(f"缺少当前集镜头数据：既没有 {path.name}，也没有 {legacy_path.name}")
        if not rows:
            raise RuntimeError(f"镜头表为空：{path}")
        return sorted(rows, key=lambda row: int(row.get("order") or 999999))

    def require_config(self) -> None:
        if not self.config_path.exists():
            raise RuntimeError(f"缺少生成配置：{self.config_path}")
        if not self.config.get("api_key") or not self.config.get("workspace_id"):
            raise RuntimeError("缺少 DASHSCOPE_API_KEY 或 DASHSCOPE_WORKSPACE_ID 环境变量。")
        if not self.config.get("model"):
            raise RuntimeError("video-provider.json 缺少 model。")

    def endpoint(self) -> str:
        region = self.config.get("region", "cn-beijing")
        workspace = self.config["workspace_id"]
        return f"https://{workspace}.{region}.maas.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis"

    def state_path(self, shot_id: str) -> Path:
        return self.state_dir / f"{shot_id}.json"

    def output_path(self, shot_id: str) -> Path:
        return self.output_dir / f"{shot_id}.mp4"

    def final_path(self) -> Path:
        return self.project / "06_edit" / "exports" / f"{self.episode}-auto-final.mp4"

    def _upload(self, path: Path) -> str:
        cache = read_json(self.upload_cache, {})
        key = str(path.resolve())
        fingerprint = f"{path.stat().st_size}:{path.stat().st_mtime_ns}"
        cached = cache.get(key, {})
        if cached.get("fingerprint") == fingerprint and cached.get("url") and time.time() - float(cached.get("uploaded_epoch", 0)) < 20 * 3600:
            return str(cached["url"])
        response = requests.get(
            "https://dashscope.aliyuncs.com/api/v1/uploads",
            headers={"Authorization": f"Bearer {self.config['api_key']}"},
            params={"action": "getPolicy", "model": self.config["model"]}, timeout=30,
        )
        if response.status_code != 200:
            raise RuntimeError(f"获取上传凭证失败 HTTP {response.status_code}: {response.text[:500]}")
        policy = response.json()["data"]
        object_key = f"{policy['upload_dir']}/{path.name}"
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as handle:
            uploaded = requests.post(policy["upload_host"], files={
                "OSSAccessKeyId": (None, policy["oss_access_key_id"]), "Signature": (None, policy["signature"]),
                "policy": (None, policy["policy"]), "x-oss-object-acl": (None, policy["x_oss_object_acl"]),
                "x-oss-forbid-overwrite": (None, policy["x_oss_forbid_overwrite"]), "key": (None, object_key),
                "success_action_status": (None, "200"), "file": (path.name, handle, mime),
            }, timeout=120)
        if uploaded.status_code != 200:
            raise RuntimeError(f"参考素材上传失败 HTTP {uploaded.status_code}: {uploaded.text[:500]}")
        url = f"oss://{object_key}"
        cache[key] = {"url": url, "fingerprint": fingerprint, "uploaded_epoch": time.time()}
        write_json(self.upload_cache, cache)
        return url

    def _slim_prompt(self, prompt: str) -> str:
        """去重并按字数上限收敛：保留开头锁定信息与结尾负向约束。"""
        import re as _re

        seen: set[str] = set()
        parts: list[str] = []
        for chunk in _re.split(r"(?<=。)", prompt):
            text = chunk.strip()
            if not text or text in seen:
                continue
            seen.add(text)
            parts.append(text)
        merged = " ".join(parts)
        if len(merged) <= self.max_prompt_chars:
            return merged
        negative = next((part for part in parts if part.startswith("禁止")), "")
        budget = self.max_prompt_chars - len(negative) - 2
        kept: list[str] = []
        used = 0
        for part in parts:
            if part.startswith("禁止"):
                continue
            if used + len(part) > budget:
                break
            kept.append(part)
            used += len(part)
        return (" ".join(kept) + " " + negative).strip()

    def _prepare_voice(self) -> dict[str, Any] | None:
        """外部 TTS 路线：批量生成前先把逐镜配音做好。"""
        import timings
        import voice_engine

        start = time.perf_counter()
        try:
            result = voice_engine.synthesize(
                self.project, self.episode, self.shots, self.config,
                progress=lambda message: print(message, flush=True),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"配音失败：{exc}", flush=True)
            timings.record(self.project, self.episode, "voice", time.perf_counter() - start, {"status": "failed"})
            raise
        done = sum(1 for row in result.get("rows", []) if row.get("status") == "已完成")
        timings.record(self.project, self.episode, "voice", time.perf_counter() - start, {"lines": done})
        print(f"配音完成：{done} 句，音轨 {result.get('track') or '未生成'}", flush=True)
        try:
            voice_engine.qc(self.project, self.episode, self.shots)
        except Exception as exc:  # noqa: BLE001
            print(f"配音质检失败（不影响成片）：{exc}", flush=True)
        return result

    def _paths(self, value: str) -> list[Path]:
        """把素材字段解析成真实存在的本地文件。

        支持三种写法：项目相对路径、绝对路径、以及旧项目的 `@别名`（在
        video-provider.json / aliyun-wan3-config.json 的 assets 里登记）。
        """
        assets = self.config.get("assets") or {}
        paths: list[Path] = []
        for item in (value or "").split("|"):
            item = item.strip()
            if not item:
                continue
            if item.startswith("@"):
                mapped = str(assets.get(item[1:], "") or "").strip()
                if not mapped:
                    continue
                item = mapped
            path = Path(item)
            if not path.is_absolute():
                path = self.project / item
            if path.exists():
                paths.append(path)
        return paths

    @staticmethod
    def scene_label(shot: dict[str, str]) -> str:
        reference = (shot.get("script_ref") or "").split("/")[0].strip()
        return reference or str(shot.get("scene_id") or "")

    def build_prompt(self, shot: dict[str, str], bindings: list[str]) -> str:
        style_name = str(self.config.get("style_preset", "都市悬疑真人电影"))
        style_prompt = str(self.config.get("custom_style_prompt", "")).strip() or STYLE_PRESETS.get(
            style_name, STYLE_PRESETS["都市悬疑真人电影"]
        )
        dialogue = (shot.get("dialogue_text") or shot.get("dialogue_or_vo") or "").strip()
        scene_id = str(shot.get("scene_id") or "").strip()
        characters = (shot.get("characters") or "").strip()
        speaker = (shot.get("speaker") or "").strip()
        if not speaker and characters and characters != "按剧本动作":
            speaker = characters
        speaker = speaker or "角色"
        has_dialogue = bool(dialogue) and dialogue != "无台词"
        perf_block = self.performance_blocks.get(shot.get("shot_id", ""))
        perf_lock = self.performance_locks.get(shot.get("shot_id", ""))
        if perf_block:
            audio = perf_block
            if has_dialogue:
                audio += f" 台词原文（逐字一致，按上嘴型念）：{speaker}：{dialogue}。"
        elif has_dialogue:
            audio = f"{speaker}只说：'{dialogue}'。逐字一致，口型同步，句尾闭嘴。"
        else:
            audio = "无台词。保持自然，人物保持表演，背景保持自然，不要说话。"
        action = (shot.get("action") or "").strip().rstrip("。.；;，,")
        scene_label = self.scene_label(shot)
        if scene_label and scene_label != scene_id:
            scene_line = f"场景：{scene_label}（{scene_id}）。" if scene_id else f"场景：{scene_label}。"
        else:
            scene_line = f"场景：{scene_id}。" if scene_id else ""
        if characters and characters != "按剧本动作":
            scene_line += f"出场角色：{characters}。"
        emotion_bits = [f"情绪：{value}" for value in [shot.get("emotion")] if value and str(value).strip()]
        emotion_bits += [f"变化：{value}" for value in [shot.get("emotion_arc")] if value and str(value).strip()]
        emotion_bits += [f"表演：{value}" for value in [shot.get("emotion_line")] if value and str(value).strip()]
        emotion_line = "；".join(emotion_bits) + "。" if emotion_bits else ""
        parts = [
            *bindings,
            style_prompt,
            f"竖屏9:16短剧镜头，{shot.get('shot_size') or '中景'}，{shot.get('camera') or '固定机位'}。",
            scene_line,
            f"动作与画面：{action}。",
            f"从‘{shot.get('start_state', '')}’自然过渡到‘{shot.get('end_state', '')}’，转场方式：{shot.get('transition') or '连续承接'}。",
            emotion_line,
            audio,
            perf_lock or "",
            NEGATIVE_LIBRARY,
        ]
        return " ".join(part for part in parts if part)

    def compile_request(self, shot: dict[str, str]) -> dict[str, Any]:
        media: list[dict[str, str]] = []
        bindings: list[str] = []
        first_frames = self._paths(shot.get("prev_last_frame", ""))
        last_frames = self._paths(shot.get("next_first_frame", ""))
        if first_frames or last_frames:
            if first_frames:
                media.append({"type": "first_frame", "url": self._upload(first_frames[0])})
            if last_frames:
                media.append({"type": "last_frame", "url": self._upload(last_frames[0])})
            bindings.append("首尾帧承接，位置、朝向、手持物、光线连续。")
        else:
            refs = self._paths("|".join([shot.get("identity_refs", ""), shot.get("scene_refs", ""), shot.get("prop_refs", "")]))
            for index, path in enumerate(dict.fromkeys(refs), 1):
                media.append({"type": "reference_image", "url": self._upload(path)})
                bindings.append(f"图{index}锁参考，保持一致。")
        payload: dict[str, Any] = {
            "model": self.config["model"], "input": {"prompt": self._slim_prompt(self.build_prompt(shot, bindings))},
            "parameters": {
                "resolution": self.config.get("resolution", "720P"), "ratio": self.config.get("ratio", "9:16"),
                "duration": self.duration_by_shot[shot["shot_id"]],
                "audio": bool(self.config.get("audio", True)) and self.voice_route != "external_tts",
                "prompt_extend": True, "watermark": False,
            },
        }
        if media:
            payload["input"]["media"] = media
        return payload

    def _ledger_update(self, shot: dict[str, str], state: dict[str, Any]) -> None:
        with self._ledger_lock:
            self._ledger_update_locked(shot, state)

    def _ledger_update_locked(self, shot: dict[str, str], state: dict[str, Any]) -> None:
        rows: list[dict[str, str]] = []
        if self.ledger.exists():
            with self.ledger.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        task_id = str(state.get("task_id", ""))
        target = next((row for row in rows if row.get("prediction_id") == task_id), None)
        if target is None:
            target = next((row for row in rows if row.get("shot_id") == shot["shot_id"] and not row.get("prediction_id")), None)
        if target is None:
            target = {field: "" for field in JOB_FIELDS}
            target["job_id"] = f"AUTO-{datetime.now().strftime('%Y%m%d%H%M%S')}-{shot['shot_id']}"
            rows.append(target)
        target.update({
            "episode": self.episode, "shot_id": shot["shot_id"], "stage": "video", "provider": "aliyun-bailian",
            "model_id": str(self.config["model"]), "prediction_id": task_id, "status": str(state.get("status", "")),
            "submitted_at": str(state.get("submitted_at", "")), "last_checked_at": str(state.get("updated_at", "")),
            "source_paths": "|".join(filter(None, [shot.get("identity_refs", ""), shot.get("scene_refs", ""), shot.get("prev_last_frame", "")])),
            "output_url": str(state.get("output_url", "")), "local_path": str(state.get("output", "")),
            "notes": f"桌面工作台自动批量生成；配音路线 {self.voice_route}；已有 task_id 只轮询，不重复提交。",
        })
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=JOB_FIELDS, extrasaction="ignore")
            writer.writeheader(); writer.writerows(rows)

    def _error_path(self, shot_id: str) -> Path:
        return self.auto / "video-errors" / self.episode / f"{shot_id}.json"

    def _record_error(self, shot: dict[str, str], manifest: Path, payload: dict[str, Any], status: int, data: dict[str, Any]) -> None:
        """提交失败时把请求与响应落盘。接口对失败任务不给 task_id，这里留证据便于复盘。"""
        try:
            write_json(self._error_path(shot["shot_id"]), {
                "shot_id": shot["shot_id"],
                "http_status": status,
                "at": now_iso(),
                "response": data,
                "request_manifest": str(manifest.relative_to(self.project)).replace("\\", "/"),
                "parameters": payload.get("parameters", {}),
                "media_count": len(payload.get("input", {}).get("media", [])),
                "prompt": payload.get("input", {}).get("prompt", ""),
            })
        except OSError:
            pass

    def submit(self, shot: dict[str, str], retry_failed: bool = False) -> dict[str, Any]:
        path = self.state_path(shot["shot_id"])
        if path.exists():
            state = read_json(path, {})
            status = state.get("status", "")
            if status in ACTIVE or status == "SUCCEEDED":
                print(f"{shot['shot_id']}: 续用任务 {state.get('task_id')} ({status})，不重复提交。", flush=True)
                return state
            if status in TERMINAL and not retry_failed:
                raise RuntimeError(f"{shot['shot_id']} 已是 {status}；若要再次计费，需显式允许重试失败任务。")
        payload = self.compile_request(shot)
        manifest = self.request_dir / f"{shot['shot_id']}.json"
        write_json(manifest, payload)
        response = requests.post(self.endpoint(), headers={
            "Authorization": f"Bearer {self.config['api_key']}", "Content-Type": "application/json",
            "X-DashScope-Async": "enable", "X-DashScope-OssResourceResolve": "enable",
        }, json=payload, timeout=60)
        data = response.json() if response.content else {}
        task_id = data.get("output", {}).get("task_id", "")
        if response.status_code != 200 or not task_id:
            self._record_error(shot, manifest, payload, response.status_code, data)
            raise RuntimeError(f"创建任务失败 HTTP {response.status_code}: {json.dumps(data, ensure_ascii=False)[:900]}")
        state = {
            "provider": "aliyun-bailian", "model": self.config["model"], "episode": self.episode,
            "shot_id": shot["shot_id"], "task_id": task_id, "status": data["output"].get("task_status", "PENDING"),
            "submitted_at": now_iso(), "updated_at": now_iso(),
            "request_manifest": str(manifest.relative_to(self.project)).replace("\\", "/"),
            "output": str(self.output_path(shot["shot_id"]).relative_to(self.project)).replace("\\", "/"), "response": data,
        }
        write_json(path, state); self._ledger_update(shot, state)
        stale_error = self._error_path(shot["shot_id"])
        if stale_error.exists():
            stale_error.unlink()
        print(f"{shot['shot_id']}: 已提交并保存 task_id={task_id}", flush=True)
        return state

    @staticmethod
    def _video_url(data: dict[str, Any]) -> str:
        output = data.get("output", {})
        for key in ("video_url", "url", "output_url"):
            if output.get(key): return str(output[key])
        for item in output.get("results", []) if isinstance(output.get("results"), list) else []:
            for key in ("video_url", "url", "output_url"):
                if isinstance(item, dict) and item.get(key): return str(item[key])
        return ""

    def refresh(self, shot: dict[str, str]) -> dict[str, Any]:
        path = self.state_path(shot["shot_id"]); state = read_json(path, {})
        url = f"https://{self.config['workspace_id']}.{self.config.get('region', 'cn-beijing')}.maas.aliyuncs.com/api/v1/tasks/{state['task_id']}"
        response = requests.get(url, headers={"Authorization": f"Bearer {self.config['api_key']}"}, timeout=30)
        data = response.json() if response.content else {}
        if response.status_code != 200:
            raise RuntimeError(f"查询任务失败 HTTP {response.status_code}: {json.dumps(data, ensure_ascii=False)[:700]}")
        state.update({"status": data.get("output", {}).get("task_status", "UNKNOWN"), "updated_at": now_iso(), "response": data})
        if state["status"] == "SUCCEEDED":
            target = self.output_path(shot["shot_id"]); video_url = self._video_url(data)
            previous_url = str(state.get("output_url") or "")
            state["output_url"] = video_url
            # 重跑同一镜时旧文件已存在，不能因为 target.exists() 就跳过下载；
            # 只有 URL 与上次相同且成片已在位，才算已取回。
            if video_url and (video_url != previous_url or not target.exists()):
                take_dir = self.output_path(shot["shot_id"]).parent / "takes" / shot["shot_id"]
                take_dir.mkdir(parents=True, exist_ok=True)
                take = take_dir / f"{state['task_id']}.mp4"
                if not take.exists():
                    temp = take.with_suffix(".part.mp4")
                    with requests.get(video_url, stream=True, timeout=300) as download:
                        download.raise_for_status()
                        with temp.open("wb") as handle:
                            for chunk in download.iter_content(1024 * 1024):
                                if chunk:
                                    handle.write(chunk)
                    temp.replace(take)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(take, target)
                state["output"] = str(target.relative_to(self.project)).replace("\\", "/")
        write_json(path, state); self._ledger_update(shot, state)
        return state

    def wait(self, shot: dict[str, str]) -> dict[str, Any]:
        while True:
            state = self.refresh(shot)
            print(f"{shot['shot_id']}: {state['status']} / {state['task_id']}", flush=True)
            if state["status"] in TERMINAL: return state
            time.sleep(self.interval)

    def batch(self, retry_failed: bool = False) -> None:
        import timings

        self.require_config()
        started = time.perf_counter()
        if self.voice_route == "external_tts":
            self._prepare_voice()
        total = len(self.shots)
        quota = self.max_concurrent
        print(f"开始批量：{total} 镜，并发 {quota}，配音路线 {self.voice_route}", flush=True)

        def run_one(indexed: tuple[int, dict[str, str]]) -> dict[str, Any]:
            index, shot = indexed
            print(f"[{index}/{total}] {shot['shot_id']}", flush=True)
            state = self.submit(shot, retry_failed)
            if state.get("status") != "SUCCEEDED":
                state = self.wait(shot)
            if state.get("status") != "SUCCEEDED":
                raise RuntimeError(f"{shot['shot_id']} 失败，批次已安全停止。")
            return state

        items = list(enumerate(self.shots, 1))
        if quota <= 1:
            for item in items:
                run_one(item)
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=quota) as pool:
                futures = {pool.submit(run_one, item): item for item in items}
                try:
                    for future in as_completed(futures):
                        future.result()
                except Exception:
                    for pending in futures:
                        pending.cancel()
                    raise
        timings.record(self.project, self.episode, "batch", time.perf_counter() - started,
                       {"shots": total, "concurrency": quota})
        self.assemble()

    def resume(self) -> None:
        """继续未完成的批次：已有 task_id 只轮询，缺任务才提交，全部成功后自动合片。"""
        self.require_config()
        pending = 0
        for shot in self.shots:
            state = read_json(self.state_path(shot["shot_id"]), {})
            if state.get("status") not in {"SUCCEEDED", "FAILED", "CANCELED"}:
                pending += 1
        if not pending:
            print("所有镜头都已完成，直接合片。", flush=True)
            self.assemble()
            return
        print(f"发现 {pending} 个未完成镜头，继续批次；已成功的镜头不会重复提交。", flush=True)
        self.batch()

    def plan(self) -> None:
        """只输出生成计划，不调用云端、不上传素材。"""
        total = sum(self.duration_by_shot.values())
        print(
            f"生成计划：{self.episode} / {len(self.shots)} 镜 / 合计 {total} 秒 / "
            f"{self.config.get('resolution', '720P')} / {self.config.get('ratio', '9:16')} / "
            f"风格 {self.config.get('style_preset', '')} / 声音 {'开' if self.config.get('audio', True) else '关'}",
            flush=True,
        )
        for index, shot in enumerate(self.shots, 1):
            assets: list[str] = []
            if self._paths(shot.get("prev_last_frame", "")) or self._paths(shot.get("next_first_frame", "")):
                assets.append("首尾帧(帧链)")
            refs = self._paths("|".join([shot.get("identity_refs", ""), shot.get("scene_refs", ""), shot.get("prop_refs", "")]))
            assets.extend(path.name for path in dict.fromkeys(refs))
            duration = self.duration_by_shot[shot["shot_id"]]
            print(f"[{index:02d}/{len(self.shots)}] {shot['shot_id']} {duration}s 素材：{'、'.join(assets) or '无（纯文生视频）'}", flush=True)
            print(f"      {self.build_prompt(shot, [])}", flush=True)

    def assemble(self) -> Path:
        import timings

        started = time.perf_counter()
        target = self._assemble_impl()
        timings.record(self.project, self.episode, "assemble", time.perf_counter() - started,
                       {"output": str(target.relative_to(self.project))})
        return target

    def _assemble_impl(self) -> Path:
        clips = [self.output_path(shot["shot_id"]) for shot in self.shots]
        missing = [path.name for path in clips if not path.exists()]
        if missing: raise RuntimeError(f"缺少 {len(missing)} 个镜头，不能合片：{', '.join(missing[:5])}")
        if not self.ffmpeg: raise RuntimeError("未找到 ffmpeg，无法自动合片。请把 ffmpeg.exe 放到 PATH、桌面或 C:\\ffmpeg\\bin。")
        build = self.auto / "build" / self.episode; build.mkdir(parents=True, exist_ok=True)
        concat = build / "concat.txt"
        concat_lines = []
        for path in clips:
            escaped = str(path).replace("'", "'\\''")
            concat_lines.append(f"file '{escaped}'\n")
        concat.write_text("".join(concat_lines), encoding="utf-8")
        target = self.final_path(); target.parent.mkdir(parents=True, exist_ok=True); temp = target.with_suffix(".part.mp4")
        result = subprocess.run([self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
            "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2:black,fps=30,format=yuv420p",
            "-af", "aresample=48000", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(temp)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode: raise RuntimeError("合片失败：" + result.stderr[-1000:])
        voice_track = self.project / "05_generation" / "audio" / f"{self.episode}-voice-track.wav"
        if voice_track.exists() and self.ffmpeg:
            mixed = target.with_suffix(".with-audio.mp4")
            remux = subprocess.run([
                self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(temp), "-i", str(voice_track),
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-ar", "48000", "-ac", "1", "-shortest", "-movflags", "+faststart", str(mixed),
            ], capture_output=True, text=True, encoding="utf-8", errors="replace")
            if remux.returncode == 0 and mixed.exists():
                mixed.replace(target)
                temp.unlink(missing_ok=True)
                print(f"已混入配音音轨：{voice_track.name}", flush=True)
            else:
                temp.replace(target)
                print("配音混入失败，已输出静音成片：" + remux.stderr[-300:], flush=True)
        else:
            temp.replace(target)
        print(f"自动合片完成：{target}", flush=True)
        return target

    def doctor(self) -> int:
        checks = {
            "项目生成配置": self.config_path.exists(), "DASHSCOPE_API_KEY": bool(self.config.get("api_key")),
            "DASHSCOPE_WORKSPACE_ID": bool(self.config.get("workspace_id")), "镜头表": bool(self.shots), "ffmpeg": bool(self.ffmpeg),
        }
        for name, ok in checks.items(): print(f"[{'OK' if ok else '缺失'}] {name}")
        identity_declared = sum(1 for shot in self.shots if (shot.get("identity_refs") or "").strip())
        scene_declared = sum(1 for shot in self.shots if (shot.get("scene_refs") or "").strip())
        identity = sum(1 for shot in self.shots if self._paths(shot.get("identity_refs", "")))
        scenes = sum(1 for shot in self.shots if self._paths(shot.get("scene_refs", "")))
        frames = sum(1 for shot in self.shots if self._paths(shot.get("prev_last_frame", "")) or self._paths(shot.get("next_first_frame", "")))
        dialogue = sum(1 for shot in self.shots if (shot.get("dialogue_text") or "").strip() not in {"", "无台词"})
        print(
            f"[信息] 模型：{self.config.get('model', '未配置')}；选中集：{self.episode}；镜头数：{len(self.shots)}；"
            f"有台词镜头：{dialogue}；目标时长：{sum(self.duration_by_shot.values())} 秒；画质：{self.config.get('resolution', '720P')}；"
            f"风格：{self.config.get('style_preset', '都市悬疑真人电影')}"
        )
        print(
            f"[信息] 素材可解析：身份 {identity}/{len(self.shots)}（登记 {identity_declared}），"
            f"场景 {scenes}/{len(self.shots)}（登记 {scene_declared}），帧链 {frames}/{len(self.shots)}"
        )
        audio_on = bool(self.config.get("audio", True))
        print(f"[信息] 声音：{'开（尝试带原生音轨）' if audio_on else '关（生成的镜头没有音轨，需要后期配音）'}")
        print(f"[信息] 单镜时长：{min_shot_limit(self.config)}–{self.max_shot_duration} 秒（下限 4 秒是实测底线）")
        if not audio_on:
            print("[提示] 声音开关关闭：这一批镜头不会带音轨，成片只能后期配音；要原生声音请到“视频生成设置”勾选。")
        print(f"[信息] ffmpeg：{self.ffmpeg or '未找到'}")
        if identity_declared > identity or scene_declared > scenes:
            print("[提示] 有素材登记但文件不存在（或 @别名 未在 assets 里配置），这些镜头会退化为纯文生视频。")
        elif identity < len(self.shots) or scenes < len(self.shots):
            print("[提示] 身份/场景参考图未全量绑定：批量生成会退化为纯文生视频，人物与场景可能漂移。")
        return 0 if all(checks.values()) else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI短剧桌面工作台批量视频引擎")
    parser.add_argument("--project", required=True); parser.add_argument("--episode", default="EP001"); parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("command", choices=["doctor", "plan", "batch", "resume", "assemble"]); parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args(argv)
    try:
        engine = BatchEngine(Path(args.project), args.episode, args.interval)
        if args.command == "doctor": return engine.doctor()
        if args.command == "plan": engine.plan()
        elif args.command == "batch": engine.batch(args.retry_failed)
        elif args.command == "resume": engine.resume()
        elif args.command == "assemble": engine.assemble()
        return 0
    except Exception as exc:
        if sys.stderr is not None:
            print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
