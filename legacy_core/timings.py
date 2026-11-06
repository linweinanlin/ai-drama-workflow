"""步骤计时：每一步耗时落盘，界面直接读，用来显示“哪一步慢、从哪继续”。"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def _path(project: Path) -> Path:
    target = project / "09_automation" / "timings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def load(project: Path) -> dict[str, Any]:
    path = _path(project)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def record(project: Path, episode: str, step: str, seconds: float, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    data = load(project)
    bucket = data.setdefault(episode, {})
    bucket[step] = {
        "seconds": round(seconds, 3),
        "at": datetime.now().isoformat(timespec="seconds"),
        **(extra or {}),
    }
    _path(project).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return bucket[step]


class step_timer:
    """with step_timer(project, episode, "analysis") as timer: ... ; timer.last 秒数"""

    def __init__(self, project: Path, episode: str, step: str, extra: dict[str, Any] | None = None) -> None:
        self.project = project
        self.episode = episode
        self.step = step
        self.extra = extra or {}
        self.last = 0.0
        self._started = 0.0

    def __enter__(self) -> "step_timer":
        self._started = time.perf_counter()
        return self

    def __exit__(self, *exc_info) -> None:
        self.last = time.perf_counter() - self._started
        try:
            record(self.project, self.episode, self.step, self.last, self.extra)
        except OSError:
            pass


def summary(project: Path, episode: str) -> str:
    bucket = load(project).get(episode, {})
    if not bucket:
        return "暂无计时"
    labels = {
        "import": "上传识别", "analysis": "剧本分析", "storyboard": "分镜", "plan": "生成预览",
        "voice": "配音", "batch": "批量生成", "assemble": "合片",
    }
    parts = []
    for key, label in labels.items():
        if key in bucket:
            parts.append(f"{label} {bucket[key].get('seconds', 0):.1f}s")
    return " · ".join(parts)
