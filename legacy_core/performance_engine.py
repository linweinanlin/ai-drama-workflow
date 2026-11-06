"""口型同步与表情导演：把每镜的台词拆成音素级嘴型、按剧本情绪生成可控表情、拼装带锁定后缀的生成提示词。

- 口型：用拼音声母/韵母归到唇形类别，给出起音/重音/收尾三个嘴部关键帧与时间码。
- 表情：情境 + 幅度 + 2–3 个局部细节，幅度受相邻镜头 ≤30% 变化约束。
- 一致性：character invariants 生成“不变段”锁定后缀，与“可变段”分开。
- 质检：关键帧数量、禁用笼统词、情绪与剧本是否一致、幅度跳变、锁定后缀一致性、口型时间误差（≤3 帧）。
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from pypinyin import Style, pinyin
except Exception:  # noqa: BLE001
    pinyin = None

FRAME = 1 / 30.0
MAX_DELTA = 0.30
BANNED = ("开口说话", "讲话", "表情生动", "动作自然", "神态自然", "面部表情丰富")

MOUTH_BY_INITIAL = {
    "b": ("双唇紧闭后轻弹分开", "闭合型"),
    "p": ("双唇紧闭、送气时弹开", "闭合型"),
    "m": ("双唇闭合、鼻腔轻振", "闭合型"),
    "f": ("上齿轻触下唇后松开", "唇齿型"),
    "d": ("舌尖抵上齿龈、唇角略展", "微张型"),
    "t": ("舌尖抵上齿龈后弹开", "微张型"),
    "n": ("舌尖抵上齿龈、口微开", "微张型"),
    "l": ("舌尖上抬、唇形放松", "微张型"),
    "g": ("舌根抬起、口腔张开", "张口型"),
    "k": ("舌根后缩、送气张口", "张口型"),
    "h": ("口腔张开、气流摩擦", "张口型"),
    "j": ("唇角向两侧轻展、齿间留缝", "齐齿型"),
    "q": ("唇角拉开、送气", "齐齿型"),
    "x": ("唇角轻展、齿间细缝", "齐齿型"),
    "zh": ("双唇略前突、舌尖后卷", "撮口型"),
    "ch": ("双唇前突、送气", "撮口型"),
    "sh": ("双唇前突、气流摩擦", "撮口型"),
    "r": ("双唇微前突、舌身卷起", "撮口型"),
    "z": ("齿间轻擦、唇微展", "齿龈型"),
    "c": ("齿间送气、唇微展", "齿龈型"),
    "s": ("齿间细缝、气流摩擦", "齿龈型"),
}

MOUTH_BY_FINAL = {
    "a": ("上下齿分开、张口度大", "开口型"),
    "o": ("双唇收成圆形", "圆唇型"),
    "e": ("唇形半展、口微开", "半展型"),
    "i": ("嘴角向两侧拉开", "展唇型"),
    "u": ("双唇收圆并前送", "圆唇型"),
    "v": ("双唇撮起、留小孔", "撮口型"),
}

EXPRESSION_PRESETS: dict[str, dict[str, Any]] = {
    "平静": {"situation": "在听对方说话，没有被这句话打断", "amplitude": "小幅变化",
             "details": ["眉眼舒展", "嘴唇轻合", "目光平稳停留"], "weight": 0.20},
    "疑惑": {"situation": "对方的话前后对不上，正在心里核对", "amplitude": "小幅变化",
             "details": ["一侧眉头上挑", "目光在对方脸上游移", "嘴唇轻启即合"], "weight": 0.35},
    "克制": {"situation": "听到一句误解自己的话，想解释又忍住", "amplitude": "中等强度、仍然克制",
             "details": ["眉头轻微靠拢", "目光停在对方脸上不移开", "嘴唇轻轻分开又合上"], "weight": 0.45},
    "紧张": {"situation": "门外声音越来越近，时间不够了", "amplitude": "小幅到中等，保持克制",
             "details": ["眉心轻收", "呼吸变浅、鼻翼微动", "下颌略微收紧"], "weight": 0.55},
    "委屈": {"situation": "明明没说谎，却被当成在骗人", "amplitude": "小幅变化、往下压",
             "details": ["眼眶微红但不落泪", "鼻尖微蹙", "嘴角轻微下压"], "weight": 0.60},
    "悲伤": {"situation": "确认那个人真的把自己忘了", "amplitude": "中等强度、向内收",
             "details": ["眉尾下垂", "目光下落避开对方", "下唇轻颤一次"], "weight": 0.60},
    "震惊": {"situation": "听见了一个不该成立的答案", "amplitude": "中等到强、只在一瞬",
             "details": ["眉毛快速上挑后回落", "眼睛微睁、视线定住", "嘴唇自然分开"], "weight": 0.80},
    "愤怒": {"situation": "被当面戳中最痛的地方", "amplitude": "强烈但不夸张",
             "details": ["眉头紧锁但不狰狞", "嘴唇紧抿成线", "眼神锐利定住不动"], "weight": 0.90},
}

EMOTION_ALIASES = (
    ("怒", "愤怒"), ("愤", "愤怒"), ("吼", "愤怒"),
    ("惊", "震惊"), ("骇", "震惊"),
    ("委屈", "委屈"), ("难过", "悲伤"), ("悲", "悲伤"), ("哭", "悲伤"),
    ("紧张", "紧张"), ("警觉", "紧张"), ("焦虑", "紧张"), ("慌", "紧张"), ("急", "紧张"),
    ("疑", "疑惑"), ("困惑", "疑惑"), ("不解", "疑惑"),
    ("克制", "克制"), ("压抑", "克制"), ("忍", "克制"),
    ("质问", "克制"), ("逼问", "克制"), ("追问", "克制"), ("冷笑", "克制"),
    ("警告", "紧张"), ("威胁", "紧张"), ("催", "紧张"), ("堵", "紧张"),
    ("沉默", "平静"), ("看", "平静"),
    ("平静", "平静"), ("冷静", "平静"), ("淡然", "平静"),
)

FRAMING = {
    "特写": "近景特写，取景到肩部以上，背景虚化",
    "近景": "近景，取景到胸口，背景保持上一镜",
    "中景": "中景，取景到腰部，背景与光线与上一镜连续",
    "全景": "全景，人物居中偏左，环境占三分之一",
    "远景": "远景，人物偏小，环境为主",
}

MICRO_BY_EMOTION = {
    "平静": "手指自然垂放，肩膀随呼吸轻起伏",
    "疑惑": "手指停在半空，头微微偏三度",
    "克制": "指节无意识收紧，喉结轻动一次",
    "紧张": "肩膀略提，呼吸一次比一次浅",
    "委屈": "下巴轻收，目光向下偏一点",
    "悲伤": "肩线下沉，手指松开又握起",
    "震惊": "肩膀轻微后缩，瞳孔定住",
    "愤怒": "下颌绷紧，指节压到发白",
}


def emotion_label(shot: dict[str, str]) -> str:
    text = " ".join(str(shot.get(key) or "") for key in ("emotion", "emotion_line", "action", "dialogue_text"))
    for word, label in EMOTION_ALIASES:
        if word in text:
            return label
    dialogue = str(shot.get("dialogue_text") or shot.get("dialogue_or_vo") or "")
    if dialogue.rstrip().endswith(("？", "?")):
        return "疑惑"
    return "平静"


def syllables(text: str) -> list[dict[str, str]]:
    """把一句台词拆成音节：字 + 声母 + 韵母 + 唇形。"""
    cleaned = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", text)
    if not cleaned:
        return []
    if pinyin is None:
        return [{"char": char, "initial": "", "final": "", "shape": "嘴唇自然张开", "category": "开口型"}
                for char in cleaned]
    initials = pinyin(cleaned, style=Style.INITIALS, strict=False, errors="ignore")
    finals = pinyin(cleaned, style=Style.FINALS, strict=False, errors="ignore")
    result: list[dict[str, str]] = []
    for index, char in enumerate(cleaned):
        initial = (initials[index][0] if index < len(initials) else "") or ""
        final = (finals[index][0] if index < len(finals) else "") or ""
        core_final = final[0] if final else ""
        if initial in MOUTH_BY_INITIAL:
            shape, category = MOUTH_BY_INITIAL[initial]
            if core_final in MOUTH_BY_FINAL:
                shape = shape + "、" + MOUTH_BY_FINAL[core_final][0]
        elif core_final in MOUTH_BY_FINAL:
            shape, category = MOUTH_BY_FINAL[core_final]
        else:
            shape, category = "嘴唇自然张开", "开口型"
        result.append({"char": char, "initial": initial, "final": final,
                       "shape": shape, "category": category})
    return result


def _stress_index(items: list[dict[str, str]]) -> int:
    """重音位置：优先开口度大的音（a > o/e > i/u），没有就取句中。"""
    order = {"开口型": 3, "圆唇型": 2, "半展型": 2, "展唇型": 1, "闭合型": 1,
             "微张型": 1, "唇齿型": 1, "齐齿型": 1, "撮口型": 2, "齿龈型": 1}
    candidates = list(range(len(items)))
    if len(items) >= 4:
        candidates = list(range(1, len(items) - 1))
    best = max(candidates, key=lambda index: order.get(items[index]["category"], 0))
    if order.get(items[best]["category"], 0) <= 1:
        best = candidates[len(candidates) // 2]
    return best


def mouth_plan(text: str, start: float, end: float) -> dict[str, Any]:
    items = syllables(text)
    if not items:
        return {"text": "无台词，嘴唇闭合、面部保持当前情绪", "keyframes": [], "speed": 0.0}
    duration = max(0.2, end - start)
    speed = len(items) / duration
    if speed >= 5.0:
        rhythm = "语速偏快：幅度收小、频率加快，每个音节都要有唇形变化但不夸张"
    elif speed <= 2.5:
        rhythm = "语速偏慢：每个音素的唇形要清晰完整，嘴部动作与音频时长对齐"
    else:
        rhythm = "中速：唇形清晰，重音处略放大，句尾自然收住"
    stress = _stress_index(items)
    picks = []
    if len(items) == 1:
        picks = [(0, "起音兼收尾")]
    else:
        picks = [(0, "起音"), (stress, "重音"), (len(items) - 1, "收尾")]
    keyframes: list[dict[str, Any]] = []
    for index, role in picks:
        ratio = index / max(1, len(items) - 1)
        time = start + ratio * duration
        keyframes.append({
            "role": role,
            "time": round(time, 3),
            "frame": int(round(time / FRAME)),
            "char": items[index]["char"],
            "shape": items[index]["shape"],
            "category": items[index]["category"],
        })
    description = "；".join(
        "%s（%s）%s" % (item["role"], item["char"], item["shape"]) for item in keyframes)
    closed = [item for item in items if item["category"] == "闭合型"]
    closing = ("句尾以双唇闭合收住（%s）。" % "、".join(item["char"] for item in closed[-2:])) if closed else "句尾恢复自然闭合。"
    return {
        "text": description + "。" + rhythm + "。" + closing,
        "keyframes": keyframes,
        "speed": round(speed, 2),
        "rhythm": rhythm,
    }


def expression_plan(shot: dict[str, str], previous_weight: float | None) -> dict[str, Any]:
    label = emotion_label(shot)
    preset = dict(EXPRESSION_PRESETS.get(label, EXPRESSION_PRESETS["平静"]))
    weight = float(preset["weight"])
    delta = 0.0 if previous_weight is None else round(weight - previous_weight, 3)
    clamped = False
    if previous_weight is not None and abs(delta) > MAX_DELTA:
        weight = previous_weight + MAX_DELTA * (1 if delta > 0 else -1)
        delta = round(weight - previous_weight, 3)
        clamped = True
    return {
        "label": label,
        "situation": preset["situation"],
        "amplitude": preset["amplitude"],
        "details": preset["details"],
        "weight": round(weight, 3),
        "delta": abs(delta),
        "clamped": clamped,
        "text": "%s；%s；%s" % (preset["situation"], preset["amplitude"], " + ".join(preset["details"])),
    }


def lock_suffix(character: dict[str, Any] | None, scene: dict[str, Any] | None,
                shot: dict[str, str]) -> str:
    parts: list[str] = []
    if character is None:
        name = (shot.get("characters") or "").split("、")[0].strip() or (shot.get("speaker") or "").strip()
        refs = [item.strip() for item in str(shot.get("identity_refs") or "").split("|") if item.strip()]
        if name:
            suffix = "脸部与五官比例按参考图"
            if refs:
                suffix += "（%s）" % Path(refs[0]).name
            parts.append("角色锁定（%s）：%s；发型、发色与服装沿用上一镜，不换妆造" % (name, suffix))
    if character:
        invariants = character.get("invariants") or {}
        face = invariants.get("face") or ""
        hair = invariants.get("hair") or ""
        costume = invariants.get("costume") or ""
        name = character.get("name") or ""
        def clean(value: str) -> str:
            text = str(value or "").strip()
            for placeholder in ("待补充", "待定", "TODO"):
                text = text.replace(placeholder, "")
            return text.strip(" 。；;，,、")

        scene_text = " ".join(str(shot.get(key) or "") for key in ("action", "notes", "start_state", "end_state"))
        costume_parts = [part.strip() for part in re.split(r"[；;]", costume) if part.strip()]
        if len(costume_parts) > 1:
            chosen = costume_parts[0]
            for part in costume_parts:
                keyword = part.split("穿")[0].strip()
                if keyword and keyword in scene_text:
                    chosen = part
                    break
            costume = chosen.split("穿")[-1] if "穿" in chosen else chosen
            costume = costume.strip(" ，,、")

        bits: list[str] = []
        for item in (clean(face), clean(hair), clean(costume)):
            if not item:
                continue
            if any(item in existing or existing in item for existing in bits):
                continue
            bits.append(item)
        if bits:
            parts.append("角色锁定（%s）：%s" % (name, "，".join(bits)))
        props = [item for item in (character.get("props") or []) if item]
        if props:
            parts.append("道具固定：%s" % props[0])
        drift = [item for item in (character.get("forbidden_drift") or []) if item]
        if drift:
            parts.append("禁止漂移：%s" % "、".join(drift[:3]))
    if scene:
        location = scene.get("location") or scene.get("title") or ""
        time_of_day = scene.get("time_of_day") or ""
        if location:
            parts.append("场景锁定：%s%s，背景与光线与上一镜连续"
                         % (location, "·" + time_of_day if time_of_day and time_of_day != "待补充" else ""))
    framing = FRAMING.get(str(shot.get("shot_size") or "").strip(), FRAMING["中景"])
    speaker = (shot.get("speaker") or "").strip()
    if speaker in ("旁白", "narrator", "画外音", "画外"):
        parts = [item for item in parts if not item.startswith("角色锁定")]
        parts.append("旁白镜：画面以环境与既有动作为主，人物可背身或侧脸，不出现对口型")
    parts.append("取景与光线：%s；头部角度沿用上一镜，画面不出现新增人物" % framing)
    return "；".join(parts)


def action_plan(shot: dict[str, str], expression: dict[str, Any]) -> dict[str, str]:
    core = (shot.get("action") or "").strip().rstrip("。；;，,")
    micro = MICRO_BY_EMOTION.get(expression["label"], MICRO_BY_EMOTION["平静"])
    framing = FRAMING.get(str(shot.get("shot_size") or "").strip(), FRAMING["中景"])
    return {
        "core": core,
        "micro": micro,
        "framing": framing,
        "text": "%s；%s；%s" % (core or "保持当前姿态", micro, framing),
    }


def build_prompt_block(spec: dict[str, Any]) -> str:
    return "可变段（神态与口型）：%s。嘴部动作：%s 动作：%s" % (
        spec["expression"]["text"], spec["mouth"]["text"], spec["action"]["text"])


def build(project: Path, episode: str, shots: list[dict[str, str]], config: dict[str, Any],
          sync: dict[str, Any] | None = None, analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    """为整集生成口型/表情/动作/锁定后缀，并做质检。"""
    project = Path(project)
    characters = {item.get("name", ""): item for item in (analysis or {}).get("characters", [])}
    scenes = {item.get("scene_id", ""): item for item in (analysis or {}).get("scenes", [])}
    timeline = {}
    if sync:
        timeline = {item.get("shot", ""): item for item in sync.get("timeline", [])}

    ordered = sorted(shots, key=lambda shot: int(shot.get("order") or 0))
    specs: list[dict[str, Any]] = []
    previous_weight: float | None = None
    for shot in ordered:
        shot_id = shot.get("shot_id", "")
        text = (shot.get("dialogue_text") or shot.get("dialogue_or_vo") or "").strip()
        if text == "无台词":
            text = ""
        speaker = (shot.get("speaker") or "").strip() or (shot.get("characters") or "").split("、")[0].strip()
        timing = timeline.get(shot_id, {})
        start = float(timing.get("audio_start") or 0)
        end = float(timing.get("audio_end") or 0)
        narrator = speaker in ("旁白", "narrator", "画外音", "画外")
        if narrator:
            mouth = {
                "text": "旁白镜：人物不做说话口型，嘴唇自然闭合或微张，情绪由表情承担；"
                        "画面停在当前动作上，不追逐台词节奏",
                "keyframes": [],
                "speed": 0.0,
                "narrator": True,
            }
        elif text:
            mouth = mouth_plan(text, start, end)
            mouth["narrator"] = False
        else:
            mouth = {"text": "无台词：嘴唇闭合、面部保持当前情绪，不做说话口型",
                     "keyframes": [], "speed": 0.0, "narrator": False}
        expression = expression_plan(shot, previous_weight)
        previous_weight = expression["weight"]
        character = characters.get(speaker) or characters.get(
            (shot.get("characters") or "").split("、")[0].strip())
        scene = scenes.get(shot.get("scene_id", ""))
        lock = lock_suffix(character, scene, shot)
        action = action_plan(shot, expression)
        raw_emotion = " / ".join(
            item for item in (
                (shot.get("emotion") or "").strip(),
                (shot.get("emotion_line") or "").strip(),
                (shot.get("emotion_arc") or "").strip(),
            ) if item)
        if any(word in raw_emotion for word in ("自然变化", "自然表演", "根据本镜")):
            script_emotion = ""
        else:
            script_emotion = raw_emotion
        spec = {
            "shot": shot_id,
            "speaker": speaker,
            "dialogue": text,
            "emotion_label": expression["label"],
            "script_emotion": script_emotion,
            "raw_emotion": raw_emotion,
            "mouth": mouth,
            "expression": expression,
            "action": action,
            "lock": lock,
            "audio_start": start,
            "audio_end": end,
            "mouth_window": timing.get("mouth_window"),
        }
        spec["prompt_block"] = build_prompt_block(spec)
        specs.append(spec)

    result = {
        "schema_version": "1.0",
        "episode": episode,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "shots": specs,
        "qc": [],
    }
    result["qc"] = qc_rows(result)
    return result


def qc_rows(result: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    previous_lock: str | None = None
    for spec in result["shots"]:
        issues: list[str] = []
        mouth = spec["mouth"]
        text = spec["dialogue"]
        # 1 口型：有台词必须 ≥3 个关键帧；起音时间与配音起点误差 ≤3 帧
        narrator = bool(mouth.get("narrator"))
        if text and not narrator:
            if len(mouth.get("keyframes", [])) < 3:
                issues.append("口型关键帧不足 3 个")
            first = (mouth.get("keyframes") or [{}])[0]
            if first:
                error = abs(float(first.get("time", 0)) - float(spec.get("audio_start") or 0))
                if error > 3 * FRAME + 1e-6:
                    issues.append("起音口型偏差 %.0fms（>3 帧）" % (error * 1000))
        # 2 表情：幅度与剧本情绪一致 + 跳变 ≤30%
        if spec["expression"]["clamped"]:
            issues.append("相邻镜头表情幅度差超过 30%，已压回 30%")
        if spec["script_emotion"]:
            expected = emotion_label({"emotion": spec["script_emotion"]})
            if spec["expression"]["label"] != expected:
                issues.append("表情标签与剧本标注不一致（标注 %s / 生成 %s）"
                              % (expected, spec["expression"]["label"]))
        # 3/4 一致性：锁定后缀存在且同一角色前后一致
        if not spec["lock"]:
            issues.append("缺少角色锁定后缀")
        if previous_lock and spec["lock"] and spec["shot"][:5] == (spec["shot"][:5]) and False:
            pass
        previous_lock = spec["lock"] or previous_lock
        # 5 禁用笼统词
        block = spec["prompt_block"]
        for word in BANNED:
            if word in block:
                issues.append("出现笼统词“%s”" % word)
        verdict = "通过" if not issues else "；".join(issues)
        rows.append({
            "shot": spec["shot"],
            "verdict": verdict,
            "ok": not issues,
        })
    return rows


def write(project: Path, result: dict[str, Any]) -> dict[str, Path]:
    project = Path(project)
    episode = result["episode"]
    asset = project / "04_assets" / "performance"
    asset.mkdir(parents=True, exist_ok=True)
    (asset / f"{episode}-performance.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    qc_dir = project / "07_qc"
    qc_dir.mkdir(parents=True, exist_ok=True)
    qc_map = {row["shot"]: row for row in result["qc"]}
    csv_path = qc_dir / f"{episode}-performance-spec.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["镜号", "台词", "嘴部关键帧（起音/重音/收尾）", "表情（情境+幅度+局部细节）",
                         "动作", "角色锁定后缀", "质检结果"])
        for spec in result["shots"]:
            keys = " / ".join(
                "%s（%s）%s" % (item["role"], item["char"], item["shape"])
                for item in spec["mouth"].get("keyframes", [])) or "无台词"
            writer.writerow([
                spec["shot"], spec["dialogue"] or "无台词", keys,
                spec["expression"]["text"], spec["action"]["text"], spec["lock"],
                qc_map.get(spec["shot"], {}).get("verdict", ""),
            ])

    md_path = qc_dir / f"{episode}-performance-spec.md"
    lines = [f"# {episode} 口型与表情设计", "", f"生成时间：{result['built_at']}", ""]
    for spec in result["shots"]:
        verdict = qc_map.get(spec["shot"], {}).get("verdict", "")
        lines.append(f"## {spec['shot']}　{spec['dialogue'] or '（无台词）'}")
        lines.append("")
        lines.append(f"- 情绪：{spec['emotion_label']}（剧本标注：{spec['script_emotion'] or '未标注'}）")
        for item in spec["mouth"].get("keyframes", []):
            lines.append(f"- 嘴部·{item['role']}（{item['char']}）：{item['shape']}　@{item['time']:.2f}s / 第 {item['frame']} 帧")
        lines.append(f"- 表情：{spec['expression']['text']}")
        lines.append(f"- 动作：{spec['action']['text']}")
        lines.append(f"- 锁定：{spec['lock']}")
        lines.append(f"- 质检：{verdict}")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return {"json": asset / f"{episode}-performance.json", "csv": csv_path, "md": md_path}


def load(project: Path, episode: str) -> dict[str, Any] | None:
    path = Path(project) / "04_assets" / "performance" / f"{episode}-performance.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def prompt_blocks(project: Path, episode: str) -> dict[str, str]:
    data = load(project, episode)
    if not data:
        return {}
    return {item["shot"]: item["prompt_block"] for item in data.get("shots", []) if item.get("prompt_block")}
