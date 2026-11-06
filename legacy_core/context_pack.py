"""单镜上下文打包：只把这一镜真正需要的信息取出来，公共描述用模板片段复用。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parent

# 公共片段只保留一份，编译时按需拼接，避免每镜重复塞全文。
COMMON_FRAGMENTS = {
    "negative": (
        "禁止：低质量、画面残缺、多余手指、肢体畸形、人物变形、面部崩坏、镜像反转、"
        "物体凭空出现或消失、道具数量变化、光线方向矛盾、画面乱码文字、时代与商标穿帮、"
        "身份漂移、人物瞬移、水印与字幕板。"
    ),
    "identity_binding": "图片{n}是已批准的身份参考，只定义脸、年龄、发型与固定服装，不得从图中继承场景。",
    "scene_binding": "图片{n}是已批准的场景参考，只定义空间、陈设、光线方向与色温，不得复制图中人物。",
    "prop_binding": "图片{n}是已批准的道具参考，只定义外形与材质，不改变数量。",
    "frame_binding": "首尾帧严格承接，角色位置、朝向、手持物和光线连续。",
    "speech_rule": "台词逐字一致，口型同步，句尾闭嘴；无台词镜头保持闭嘴，不添加旁白。",
}

SHOT_KEYS = (
    "shot_id", "scene_id", "order", "duration_s", "shot_size", "camera", "transition",
    "characters", "action", "start_state", "end_state", "dialogue_text", "speaker",
    "emotion", "emotion_arc", "emotion_line", "identity_refs", "scene_refs", "prop_refs",
    "prev_last_frame", "next_first_frame", "audio_route", "risk_level",
)


def shot_context(project: Path, episode: str, shot: dict[str, str], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回单镜上下文：本镜行 + 引用路径 + 片段引用，不含公共全文。"""
    config = config or {}
    project = Path(project)
    shot_row = {key: shot.get(key, "") for key in SHOT_KEYS if shot.get(key, "") not in (None, "")}

    def resolve(value: str) -> list[str]:
        paths: list[str] = []
        assets = config.get("assets") or {}
        for item in str(value or "").split("|"):
            item = item.strip()
            if not item:
                continue
            if item.startswith("@"):
                item = str(assets.get(item[1:], ""))
            if not item:
                continue
            candidate = project / item
            if candidate.exists():
                paths.append(item.replace("\\", "/"))
        return paths

    spec_ref = None
    spec_path = project / "05_generation" / f"{episode}-prompt-spec.md"
    if spec_path.exists():
        spec_ref = str(spec_path.relative_to(project)).replace("\\", "/")

    return {
        "episode": episode,
        "shot": shot_row,
        "assets": {
            "identity": resolve(shot.get("identity_refs", "")),
            "scene": resolve(shot.get("scene_refs", "")),
            "prop": resolve(shot.get("prop_refs", "")),
            "prev_last_frame": resolve(shot.get("prev_last_frame", "")),
            "next_first_frame": resolve(shot.get("next_first_frame", "")),
        },
        "prompt_spec_ref": spec_ref,
        "fragment_refs": ["COMMON_FRAGMENTS.negative", "COMMON_FRAGMENTS.identity_binding",
                          "COMMON_FRAGMENTS.scene_binding", "COMMON_FRAGMENTS.speech_rule"],
        "limits": {
            "max_prompt_chars": int(config.get("max_prompt_chars") or 900),
            "max_shot_duration_seconds": int(config.get("max_shot_duration_seconds") or 8),
        },
        "style": str(config.get("custom_style_prompt") or config.get("style_preset") or ""),
    }


def context_chars(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False))
