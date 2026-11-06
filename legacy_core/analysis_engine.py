"""剧本分析引擎：把剧本 + 系列圣经解析成结构化分析结果并缓存。

设计原则：
- 纯本地启发式，不调用任何模型（不产生费用、不需要 API Key）。
- 结果落盘到 ``02_script/analysis.json``，用剧本内容指纹做缓存：内容没变就不重算。
- 字段能确定就填，实在无法判断的写“待补充”，由人工在界面上修正。
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

ANALYSIS_SCHEMA = "1.0"
BEAT_KEYS = ("hook", "setup", "escalation", "reversal", "cliffhanger")
BEAT_LABELS = {
    "hook": "钩子（前 3 秒）",
    "setup": "建立",
    "escalation": "升级",
    "reversal": "反转",
    "cliffhanger": "悬念",
}
EMOTION_WEIGHTS = {
    "震惊警觉": 0.82, "愤怒": 0.9, "悲伤克制": 0.75, "温柔": 0.45, "克制自然": 0.5,
}
SCENE_PATTERN = re.compile(r"^(?:#+\s*)?(?:第?\s*([0-9一二三四五六七八九十百零两]+)\s*场|场景?\s*[：:]?\s*(\d+)|SCENE\s*(\d+)|(?:内景|外景|INT\.?|EXT\.?)\b)", re.I)
DIALOGUE_PATTERN = re.compile(r"^([\u4e00-\u9fffA-Za-z0-9·_（）()]{1,16})\s*[：:]\s*(.+)$")
TIME_BEAT_PATTERN = re.compile(r"^\*{0,2}\s*(\d+(?:\.\d+)?)\s*[–\-~至]\s*(\d+(?:\.\d+)?)\s*秒\s*[｜|]\s*(.+?)\s*\*{0,2}$")
CAST_PATTERN = re.compile(r"^(?:人物|角色|出场人物|人物表)\s*[：:]\s*(.+)$")
NON_CAST = {"时间", "地点", "人物", "场景", "备注", "片名", "集数"}
PROP_HINTS = ("录音笔", "手账", "机械表", "相机", "文件袋", "手机", "注射器", "监控", "照片", "眼镜", "戒指", "门铃")


def _read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, OSError):
            continue
    return ""


def fingerprint(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def analysis_path(project: Path, episode: str) -> Path:
    return project / "02_script" / f"{episode}-analysis.json"


def load_analysis(project: Path, episode: str) -> dict[str, Any] | None:
    path = analysis_path(project, episode)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        data["_cached"] = True
        return data
    return None


def save_analysis(project: Path, episode: str, data: dict[str, Any]) -> Path:
    path = analysis_path(project, episode)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["schema_version"] = ANALYSIS_SCHEMA
    data["episode"] = episode
    data["saved_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def analyze(project: Path, episode: str, force: bool = False) -> dict[str, Any]:
    """解析并返回分析结果；内容未变且有缓存时直接读缓存。"""
    script = project / "02_script" / f"{episode}-script.md"
    if not script.exists():
        raise FileNotFoundError(f"缺少剧本：{script}")
    digest = fingerprint(script)
    cached = load_analysis(project, episode)
    if (
        cached
        and not force
        and cached.get("source", {}).get("sha256") == digest
        and cached.get("schema_version") == ANALYSIS_SCHEMA
    ):
        cached["_cached"] = True
        return cached

    text = _read_text(script)
    shots = _load_shots(project, episode)
    bible = _parse_bible(project)
    characters = _build_characters(text, bible, shots)
    beats = _build_beats(text, shots)
    curve = _build_emotion_curve(shots, beats)
    relationships = _build_relationships(characters, shots, bible, text)
    scenes = _build_scenes(text, shots, characters)
    data: dict[str, Any] = {
        "schema_version": ANALYSIS_SCHEMA,
        "episode": episode,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": {
            "name": script.name,
            "sha256": digest,
            "chars": len(text),
        },
        "summary": {
            "scene_count": len(scenes),
            "shot_count": len(shots),
            "dialogue_lines": sum(1 for shot in shots if _has_dialogue(shot)),
            "estimated_seconds": round(sum(_duration(shot) for shot in shots), 1),
            "character_count": len(characters),
        },
        "characters": characters,
        "beats": beats,
        "emotion_curve": curve,
        "relationships": relationships,
        "scenes": scenes,
        "warnings": [],
    }
    if not characters:
        data["warnings"].append("没有识别到角色，可在界面上手动补充。")
    if not beats:
        data["warnings"].append("没有识别到节拍，已按比例均分，可在界面上调整。")
    data["_cached"] = False
    save_analysis(project, episode, data)
    return data


# ---------- 内部解析 ----------

def _load_shots(project: Path, episode: str) -> list[dict[str, str]]:
    path = project / "03_storyboard" / f"{episode}-shotlist.csv"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("shot_id")]
    return _with_timeline(rows)


def _with_timeline(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    rows.sort(key=lambda row: int(row.get("order") or 9999) if str(row.get("order", "")).isdigit() else 9999)
    cursor = 0.0
    for row in rows:
        row["_start"] = f"{cursor:.2f}"
        cursor += _duration(row)
        row["_end"] = f"{cursor:.2f}"
    return rows


def _duration(shot: dict[str, str]) -> float:
    try:
        return max(0.5, float(shot.get("duration_s") or 5))
    except (TypeError, ValueError):
        return 5.0


def _dialogue(shot: dict[str, str]) -> str:
    for key in ("dialogue_text", "dialogue_or_vo"):
        text = (shot.get(key) or "").strip()
        if text and text != "无台词":
            return text
    return ""


def _has_dialogue(shot: dict[str, str]) -> bool:
    return bool(_dialogue(shot))


def _speaker(shot: dict[str, str]) -> str:
    speaker = (shot.get("speaker") or "").strip()
    if speaker and speaker not in {"无", "无台词"}:
        return speaker
    dialogue = _dialogue(shot)
    match = DIALOGUE_PATTERN.match(dialogue)
    if match:
        return match.group(1).strip()
    people = [name.strip() for name in re.split(r"[、,，/｜|]+", shot.get("characters") or "") if name.strip()]
    return people[0] if people else ""


def _emotion(shot: dict[str, str]) -> str:
    emotion = (shot.get("emotion") or "").strip()
    if emotion:
        return emotion
    text = _dialogue(shot) or shot.get("action", "")
    if any(word in text for word in ("哭", "泪", "失去", "对不起", "离开")):
        return "悲伤克制"
    if any(word in text for word in ("滚", "闭嘴", "凭什么", "怒", "砸", "吼", "失败")):
        return "愤怒"
    if any(word in text for word in ("什么", "怎么", "不可能", "竟然", "？", "?")):
        return "震惊警觉"
    if any(word in text for word in ("笑", "喜欢", "爱", "谢谢", "放心")):
        return "温柔"
    return "克制自然"


def _parse_bible(project: Path) -> dict[str, dict[str, Any]]:
    """从 series-bible.md 抽取角色卡（剧情功能/性格/外貌锁/服装/道具/禁漂移）。"""
    path = project / "01_bible" / "series-bible.md"
    if not path.exists():
        return {}
    characters: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for raw in _read_text(path).splitlines():
        line = raw.strip()
        heading = re.match(r"^###\s*([^｜|]+)[｜|]\s*([^｜|]*)[｜|]?\s*(.*)$", line)
        if heading:
            name = heading.group(1).strip()
            current = {
                "name": name,
                "age": heading.group(2).strip(),
                "identity": heading.group(3).strip(),
                "raw": {},
            }
            characters[name] = current
            continue
        if line.startswith("##"):
            current = None
            continue
        if current is None:
            continue
        bullet = re.match(r"^-\s*([^：:]+)[：:]\s*(.+)$", line)
        if bullet:
            current["raw"][bullet.group(1).strip()] = bullet.group(2).strip()
    return characters


def _pick(raw: dict[str, str], *keys: str) -> str:
    for key in keys:
        for name, value in raw.items():
            if key in name:
                return value
    return ""


def _build_characters(text: str, bible: dict[str, dict[str, Any]], shots: list[dict[str, str]]) -> list[dict[str, Any]]:
    counts: dict[str, list[str]] = {}
    first_shot: dict[str, str] = {}
    for shot in shots:
        speaker = _speaker(shot)
        if speaker and _has_dialogue(shot):
            counts.setdefault(speaker, []).append(_dialogue(shot))
            first_shot.setdefault(speaker, shot.get("shot_id", ""))
    for line in text.splitlines():
        match = DIALOGUE_PATTERN.match(line.strip())
        if match and match.group(1).lower() not in NON_CAST:
            name = match.group(1)
            counts.setdefault(name, [])
            first_shot.setdefault(name, "")

    names = list(counts.keys())
    for name in bible:
        if name not in names and any(name in row.get("characters", "") for row in shots):
            names.append(name)
        elif name not in names and not shots:
            names.append(name)

    ordered = sorted(names, key=lambda name: (-len(counts.get(name, [])), names.index(name)))
    total_lines = max(1, sum(len(value) for value in counts.values()))
    result: list[dict[str, Any]] = []
    for index, name in enumerate(ordered):
        info = bible.get(name, {})
        raw = info.get("raw", {})
        lines = counts.get(name, [])
        share = len(lines) / total_lines
        if share >= 0.4 or index == 0:
            function_default = "主角"
        elif share >= 0.2:
            function_default = "主要配角"
        elif lines:
            function_default = "配角"
        else:
            function_default = "待补充"
        result.append({
            "name": name,
            "function": _pick(raw, "剧情功能") or function_default,
            "age": info.get("age") or _pick(raw, "年龄") or "待补充",
            "temperament": _pick(raw, "性格", "外在形象") or "待补充",
            "invariants": {
                "face": _pick(raw, "外貌锁", "视觉锁") or "待补充",
                "hair": _hair_from(_pick(raw, "外貌锁", "视觉锁")),
                "costume": _pick(raw, "基础服装", "服装") or "待补充",
            },
            "props": [item.strip() for item in re.split(r"[、，,；;]", _pick(raw, "标志道具")) if item.strip()],
            "speech_style": _pick(raw, "表演线索", "说话") or "待补充",
            "forbidden_drift": [item.strip(" 。") for item in re.split(r"[、，,；;]", _pick(raw, "绝不漂移", "禁止")) if item.strip()],
            "dialogue_lines": len(lines),
            "dialogue_share": round(share, 3),
            "first_shot": first_shot.get(name, ""),
            "source": "series-bible" if info else "script",
        })
    return result


def _build_beats(text: str, shots: list[dict[str, str]]) -> list[dict[str, Any]]:
    marked: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = TIME_BEAT_PATTERN.match(line.strip())
        if match:
            marked.append({
                "start": float(match.group(1)),
                "end": float(match.group(2)),
                "title": match.group(3),
            })
    total = sum(_duration(shot) for shot in shots) or (marked[-1]["end"] if marked else 60.0)
    beats: list[dict[str, Any]] = []
    if marked:
        for index, item in enumerate(marked):
            key = _beat_key(item["title"], index, len(marked))
            beats.append({
                "key": key,
                "stage": _stage_for(key, item["start"], item["end"], total, index, len(marked)),
                "label": f"{BEAT_LABELS[key]}｜{item['title']}" if key else item["title"],
                "start": item["start"],
                "end": item["end"],
                "shots": [shot["shot_id"] for shot in shots if float(shot["_start"]) < item["end"] and float(shot["_end"]) > item["start"]],
                "visible_events": [shot.get("action", "") for shot in shots
                                   if float(shot["_start"]) < item["end"] and float(shot["_end"]) > item["start"]][:4],
                "end_state": next((shot.get("end_state", "") for shot in shots
                                   if float(shot["_start"]) < item["end"] and float(shot["_end"]) > item["start"]), ""),
            })
        beats = _ensure_stages(beats)
        return beats

    if not shots:
        return []
    # 没有时间标记时按比例切五段
    boundaries = [0.0, 3.0, total * 0.28, total * 0.56, total * 0.82, total]
    for index, key in enumerate(BEAT_KEYS):
        start, end = boundaries[index], boundaries[index + 1]
        in_beat = [shot for shot in shots if float(shot["_start"]) < end and float(shot["_end"]) > start]
        beats.append({
            "key": key,
            "stage": key,
            "label": BEAT_LABELS[key],
            "start": round(start, 2),
            "end": round(end, 2),
            "shots": [shot["shot_id"] for shot in in_beat],
            "visible_events": [shot.get("action", "") for shot in in_beat][:4],
            "end_state": in_beat[-1].get("end_state", "") if in_beat else "",
        })
    return beats


def _beat_key(title: str, index: int, count: int) -> str:
    if any(word in title for word in ("钩子", "开场")):
        return "hook"
    if any(word in title for word in ("反转", "揭穿", "真相")):
        return "reversal"
    if any(word in title for word in ("悬念", "结尾", "收尾")):
        return "cliffhanger"
    if any(word in title for word in ("升级", "冲突", "逼近", "施压")):
        return "escalation"
    if index == 0:
        return "hook"
    if index == count - 1:
        return "cliffhanger"
    return "setup"


def _build_emotion_curve(shots: list[dict[str, str]], beats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    beat_by_shot: dict[str, str] = {}
    for beat in beats:
        for shot_id in beat.get("shots", []):
            beat_by_shot.setdefault(shot_id, beat.get("stage") or beat.get("key", ""))
    curve: list[dict[str, Any]] = []
    for shot in shots:
        emotion = _emotion(shot)
        intensity = EMOTION_WEIGHTS.get(emotion, 0.5)
        dialogue = shot.get("dialogue_text") or ""
        if any(mark in dialogue for mark in ("！", "!", "？", "?")):
            intensity += 0.08
        if len(dialogue) > 24:
            intensity += 0.05
        key = beat_by_shot.get(shot.get("shot_id", ""), "")
        if key in ("hook", "reversal", "cliffhanger"):
            intensity += 0.12
        curve.append({
            "t": round(float(shot["_start"]) + _duration(shot) / 2, 2),
            "shot": shot.get("shot_id", ""),
            "intensity": round(min(1.0, max(0.1, intensity)), 2),
            "emotion": emotion,
            "beat": key,
        })
    return curve


def _build_relationships(characters: list[dict[str, Any]], shots: list[dict[str, str]], bible: dict[str, dict[str, Any]], text: str = "") -> dict[str, Any]:
    nodes = [{"id": item["name"], "label": item["name"], "function": item["function"]} for item in characters]
    pair_hits: dict[tuple[str, str], list[dict[str, str]]] = {}
    for shot in shots:
        people = [name.strip() for name in re.split(r"[、,，/｜|]+", shot.get("characters") or "") if name.strip()]
        if len(people) < 2:
            continue
        for index, left in enumerate(people):
            for right in people[index + 1:]:
                pair = tuple(sorted((left, right)))
                pair_hits.setdefault(pair, []).append(shot)
    edges: list[dict[str, Any]] = []
    for (left, right), hits in pair_hits.items():
        label = _relationship_label(left, right, bible, text)
        tones = []
        for third in range(3):
            slices = hits[third * len(hits) // 3: (third + 1) * len(hits) // 3] if len(hits) >= 3 else hits
            emotions = [_emotion(shot) for shot in slices]
            texts = [(_dialogue(shot) or shot.get("action", "")) for shot in slices]
            tones.append(_tone(emotions, texts))
        edges.append({
            "from": left,
            "to": right,
            "label": label,
            "initial": tones[0],
            "mid": tones[1],
            "final": tones[2],
            "shots": [shot.get("shot_id", "") for shot in hits],
        })
    return {"nodes": nodes, "edges": edges}


def _relationship_label(left: str, right: str, bible: dict[str, dict[str, Any]], text: str = "") -> str:
    for name, other in ((left, right), (right, left)):
        raw = bible.get(name, {}).get("raw", {})
        relation = _pick(raw, "与主角关系", "关系")
        if relation and other in relation:
            return relation
        for key, value in raw.items():
            if "关系" in key and other in value:
                return value
    return _infer_relation(left, right, text) or "同场关系"


def _infer_relation(left: str, right: str, text: str) -> str:
    pairs = {(left, right), (right, left)}
    rules = [
        ("夫妻", ("妻子", "丈夫", "老婆", "老公", "结婚", "婚礼")),
        ("旧识", ("大学", "老友", "旧识", "认识多年")),
        ("上司与下属", ("周总", "上司", "老板", "下属")),
        ("调查者与当事人", ("刑警", "警察", "调查", "证人")),
    ]
    for label, words in rules:
        if any(word in text for word in words):
            return label
    return ""


def _tone(emotions: list[str], texts: list[str] | None = None) -> str:
    joined = "".join(texts or [])
    if any(word in joined for word in ("滚", "闭嘴", "凭什么", "威胁", "杀", "报警")):
        return "对峙"
    if any(word in joined for word in ("对不起", "谢谢", "相信", "一起", "放心")):
        return "和解"
    if any(word in joined for word in ("？", "?", "真的", "到底")):
        return "试探"
    if not emotions:
        return "待补充"
    if any(item in ("愤怒", "震惊警觉") for item in emotions):
        return "对峙"
    if any(item == "悲伤克制" for item in emotions):
        return "压抑"
    if any(item == "温柔" for item in emotions):
        return "靠近"
    return "试探"


def _build_scenes(text: str, shots: list[dict[str, str]], characters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_scene: dict[str, list[dict[str, str]]] = {}
    for shot in shots:
        by_scene.setdefault(shot.get("scene_id") or "SC01", []).append(shot)
    scene_titles: dict[str, str] = {}
    for shot in shots:
        reference = (shot.get("script_ref") or "").split("/")[0].strip()
        if not reference:
            alias = (shot.get("scene_refs") or "").split("|")[0].strip().lstrip("@")
            reference = alias
        scene_titles.setdefault(shot.get("scene_id") or "SC01", reference)
    bible_props: list[dict[str, str]] = []
    for item in characters:
        for prop in item.get("props", []):
            bible_props.append({"name": prop, "owner": item["name"]})
    headings = _scene_headings(text)
    scenes: list[dict[str, Any]] = []
    for scene_id, rows in by_scene.items():
        title = scene_titles.get(scene_id, "")
        location, time_of_day = _split_scene_title(title)
        number = int("".join(ch for ch in scene_id if ch.isdigit()) or 0)
        heading = headings.get(number)
        if heading:
            location = heading[0] or location
            time_of_day = heading[1] or time_of_day
        props: list[dict[str, str]] = []
        for shot in rows:
            for ref in re.split(r"[|｜]", shot.get("prop_refs") or ""):
                alias = ref.strip().lstrip("@")
                if alias:
                    props.append({"name": alias, "owner": (shot.get("characters") or "").split("|")[0] or "待补充"})
        for prop in bible_props:
            if any(prop["name"] in (shot.get("action") or "") for shot in rows):
                props.append({"name": prop["name"], "owner": prop["owner"]})
        seen: set[str] = set()
        unique_props = []
        for prop in props:
            key = (prop["name"], prop["owner"])
            if key not in seen:
                seen.add(key)
                unique_props.append(prop)
        scenes.append({
            "scene_id": scene_id,
            "location": location,
            "time_of_day": time_of_day,
            "title": title or scene_id,
            "shots": [shot.get("shot_id", "") for shot in rows],
            "start": float(rows[0]["_start"]),
            "end": float(rows[-1]["_end"]),
            "props": unique_props,
        })
    scenes.sort(key=lambda item: item["start"])
    return scenes


def _split_scene_title(title: str) -> tuple[str, str]:
    if not title:
        return "待补充", "待补充"
    parts = [part for part in re.split(r"[\s　]+", title) if part]
    location = " ".join(parts[1:]) if len(parts) > 1 else title
    time_of_day = "待补充"
    for keyword in ("清晨", "早晨", "上午", "中午", "下午", "黄昏", "傍晚", "夜", "深夜", "凌晨", "日"):
        if keyword in title:
            time_of_day = keyword
            break
    location = re.sub(r"^(内景|外景|INT\.?|EXT\.?)\s*", "", location)
    for keyword in ("清晨", "早晨", "上午", "中午", "下午", "黄昏", "傍晚", "深夜", "凌晨"):
        location = location.replace(keyword, "")
    return location.strip() or "待补充", time_of_day

def _stage_for(key: str, start: float, end: float, total: float, index: int = 0, count: int = 0) -> str:
    """把单个时间标记段归到五段结构（钩子/建立/升级/反转/悬念）。"""
    if count and index == count - 1 and key != "hook":
        return "cliffhanger"
    ratio = ((start + end) / 2) / max(1.0, total)
    if ratio < 0.05 or key == "hook":
        return "hook"
    if ratio > 0.88:
        return "cliffhanger"
    if key in ("escalation", "reversal"):
        return key
    return "escalation" if ratio >= 0.5 else "setup"


def _hair_from(text: str) -> str:
    if not text:
        return "待补充"
    for chunk in re.split(r"[；;，,]", text):
        if "发" in chunk:
            return chunk.strip()
    return "待补充"

def _scene_headings(text: str) -> dict[int, tuple[str, str]]:
    """解析剧本里的“### 场 1｜婚房门口与卧室｜清晨｜内/外”标题。"""
    headings: dict[int, tuple[str, str]] = {}
    pattern = re.compile(r"^#+\s*场?\s*(\d+)\s*[｜|]\s*([^｜|]+)[｜|]?\s*([^｜|]*)[｜|]?")
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if match:
            headings[int(match.group(1))] = (match.group(2).strip(), match.group(3).strip())
    return headings


def _ensure_stages(beats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """剧本标记可能缺某一段：按位置补齐，保证钩子/建立/升级/反转/悬念都出现。"""
    present = {beat.get("stage") for beat in beats}
    if set(BEAT_KEYS).issubset(present) or len(beats) < 3:
        return beats
    count = len(beats)
    for index, beat in enumerate(beats):
        if index == 0:
            beat["stage"] = "hook"
        elif index == count - 1:
            beat["stage"] = "cliffhanger"
        else:
            ratio = index / (count - 1)
            beat["stage"] = "setup" if ratio < 0.4 else ("escalation" if ratio < 0.75 else "reversal")
    return beats
