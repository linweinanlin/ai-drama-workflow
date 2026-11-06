from __future__ import annotations

import csv
import html
import json
import re
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree


SHOT_FIELDS = [
    "episode", "shot_id", "scene_id", "script_ref", "order", "duration_s", "shot_size", "camera",
    "transition", "characters", "action", "dialogue_or_vo", "dialogue_text", "speaker", "emotion",
    "emotion_arc", "emotion_line", "line_tc", "start_state", "end_state", "prev_last_frame",
    "next_first_frame", "identity_refs", "scene_refs", "prop_refs", "audio_route", "risk_level",
    "status", "output_path", "notes",
]
SCENE_PATTERN = re.compile(r"^(?:#+\s*)?(?:第?\s*([0-9一二三四五六七八九十百零两]+)\s*场|场景?\s*[：:]?\s*(\d+)|SCENE\s*(\d+)|(?:内景|外景|INT\.?|EXT\.?)\b)", re.I)
DIALOGUE_PATTERN = re.compile(r"^([\u4e00-\u9fffA-Za-z0-9·_（）()]{1,16})\s*[：:]\s*(.+)$")
CAST_PATTERN = re.compile(r"^(?:人物|角色|出场人物|人物表)\s*[：:]\s*(.+)$")
NON_CAST_META = {"时间", "地点", "人物", "场景", "备注", "片名", "集数"}
CHINESE_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def chinese_to_int(value: str) -> int | None:
    """把“三”“十二”“二十一”这类中文数字转成整数，失败返回 None。"""
    if not value:
        return None
    if value.isdigit():
        return int(value)
    total = 0
    current = 0
    for char in value:
        if char in CHINESE_DIGITS:
            current = CHINESE_DIGITS[char]
        elif char == "十":
            total += (current or 1) * 10
            current = 0
        elif char == "百":
            total += (current or 1) * 100
            current = 0
        else:
            return None
    return total + current


def read_script(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".markdown", ".csv"}:
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return path.read_text(encoding=encoding)
            except UnicodeDecodeError:
                continue
        raise RuntimeError("无法识别剧本文本编码。")
    if suffix == ".docx":
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs = []
        for paragraph in root.findall(".//w:p", namespace):
            text = "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace)).strip()
            if text:
                paragraphs.append(text)
        return "\n".join(paragraphs)
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
        except ImportError:
            pdftotext = shutil.which("pdftotext")
            if not pdftotext:
                raise RuntimeError("当前环境无法读取 PDF；请改用 TXT、MD 或 DOCX。")
            result = subprocess.run([pdftotext, "-layout", str(path), "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
            if result.returncode:
                raise RuntimeError("PDF 文本提取失败。")
            return result.stdout
    raise RuntimeError("支持的剧本格式：TXT、MD、DOCX、PDF。")


def emotion_for(text: str) -> tuple[str, str, str]:
    if any(word in text for word in ("哭", "泪", "失去", "对不起", "离开")):
        return "悲伤克制", "平稳→下降", "眼眶湿润，呼吸变浅，句尾轻收"
    if any(word in text for word in ("滚", "闭嘴", "凭什么", "怒", "砸", "吼")):
        return "愤怒", "上升→爆发", "眉心收紧，下颌绷住，语速加快并重读关键词"
    if any(word in text for word in ("什么", "怎么", "不可能", "竟然", "？", "?")):
        return "震惊警觉", "平稳→上升", "瞳孔聚焦，短暂停顿后加重疑问句尾"
    if any(word in text for word in ("笑", "喜欢", "爱", "谢谢", "放心")):
        return "温柔", "平稳→轻微上升", "眉眼放松，嘴角轻动，语速自然偏慢"
    return "克制自然", "平稳推进", "视线明确，呼吸自然，避免夸张表情"


def parse_script(text: str, episode: str) -> list[dict[str, str]]:
    lines = [re.sub(r"^[\-*•]+\s*", "", line.strip()) for line in text.replace("\r", "").split("\n")]
    scene_number = 1
    scene_title = "未标注场景"
    cast: list[str] = []
    shots: list[dict[str, str]] = []
    for line_number, line in enumerate(lines, 1):
        if not line:
            continue
        cast_match = CAST_PATTERN.match(line)
        if cast_match:
            cast = [name.strip() for name in re.split(r"[、,，/｜|\s]+", cast_match.group(1)) if name.strip()]
            continue
        scene_match = SCENE_PATTERN.match(line)
        if scene_match:
            number = next((value for value in scene_match.groups() if value), None)
            parsed = chinese_to_int(number) if number else None
            scene_number = parsed if parsed else scene_number + (1 if shots else 0)
            scene_title = line
            cast = []
            continue
        dialogue_match = DIALOGUE_PATTERN.match(line)
        if dialogue_match and dialogue_match.group(1).lower() in NON_CAST_META:
            dialogue_match = None
        if dialogue_match:
            speaker, dialogue = dialogue_match.groups()
            others = [name for name in cast if name != speaker]
            target = "、".join(others) if others else "对方"
            action = f"{speaker}面向{target}说出台词"
            characters = speaker
            audio_route = "原生有声"
            risk = "高"
        else:
            speaker, dialogue = "无", "无台词"
            action = line
            characters = "、".join(cast) if cast else "按剧本动作"
            audio_route = "静音"
            risk = "中"
        emotion, emotion_arc, emotion_line = emotion_for(line)
        order = len(shots) + 1
        shot_id = f"{episode}-S{order:03d}"
        length = len(dialogue if dialogue != "无台词" else action)
        duration = max(3, min(12, round((length / (4 if dialogue != "无台词" else 8)) + 2)))
        shots.append({
            "episode": episode, "shot_id": shot_id, "scene_id": f"SC{scene_number:02d}",
            "script_ref": f"{scene_title}/原文第{line_number}行", "order": str(order), "duration_s": str(duration),
            "shot_size": "近景" if dialogue != "无台词" else "中景", "camera": "轻微前推" if dialogue != "无台词" else "固定机位",
            "transition": "连续承接", "characters": characters,
            "action": action, "dialogue_or_vo": "" if dialogue == "无台词" else dialogue,
            "dialogue_text": dialogue, "speaker": speaker, "emotion": emotion, "emotion_arc": emotion_arc,
            "emotion_line": emotion_line, "line_tc": "自动对齐" if dialogue != "无台词" else "",
            "start_state": "承接上一镜末态", "end_state": "形成下一镜可见承接点",
            "prev_last_frame": "", "next_first_frame": "", "identity_refs": "", "scene_refs": "",
            "prop_refs": "", "audio_route": audio_route, "risk_level": risk, "status": "DRAFT",
            "output_path": "", "notes": "由上传剧本自动识别；生成前请在可视化分镜中审核",
        })
    if not shots:
        raise RuntimeError("没有从剧本中识别到可生成的动作或对白。")
    for index, shot in enumerate(shots):
        if index > 0:
            shot["prev_last_frame"] = f"03_storyboard/frame-chain/{shots[index - 1]['shot_id']}-last.png"
        if index < len(shots) - 1:
            shot["next_first_frame"] = f"03_storyboard/frame-chain/{shots[index + 1]['shot_id']}-first.png"
    return shots


def render_storyboard(project: Path, episode: str, shots: list[dict[str, str]], source_name: str) -> Path:
    scene_count = len({shot["scene_id"] for shot in shots})
    dialogue_count = sum(1 for shot in shots if shot.get("dialogue_text") not in {"", "无台词"})
    total_seconds = sum(int(shot["duration_s"]) for shot in shots)
    unbind = sum(1 for shot in shots if not (shot.get("identity_refs") or "").strip() and not (shot.get("scene_refs") or "").strip())
    cards = []
    for shot in shots:
        dialogue = "无台词" if shot["dialogue_text"] == "无台词" else f"{shot['speaker']}：{shot['dialogue_text']}"
        scene_text = (shot.get("script_ref") or "").split("/")[0]
        cards.append(f"""
        <article class="card">
          <div class="frame"><span>{html.escape(shot['shot_size'])}</span><b>{html.escape(shot['shot_id'])}</b><small>{html.escape(shot['scene_id'])}</small></div>
          <div class="body"><p class="meta">{html.escape(scene_text)} · 角色 {html.escape(shot.get('characters') or '按剧本动作')}</p>
          <h3>{html.escape(shot['action'])}</h3><p class="dialogue">{html.escape(dialogue)}</p>
          <p>{html.escape(shot['emotion'])} · {html.escape(shot['emotion_arc'])}</p>
          <footer>{shot['duration_s']} 秒 · {html.escape(shot['camera'])} · {html.escape(shot['risk_level'])}风险</footer></div>
        </article>""")
    warning = ""
    if unbind:
        warning = (
            f'<p class="warn">注意：{unbind} 个镜头还没有绑定身份/场景参考图，'
            "直接批量生成可能带来人物或场景漂移；请在审核时补齐 04_assets 参考。</p>"
        )
    document = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{episode} 可视化分镜</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#09101f;color:#edf3ff;font-family:'Microsoft YaHei UI',sans-serif}}header{{position:sticky;top:0;background:#101a30dd;backdrop-filter:blur(12px);padding:24px 5vw;border-bottom:1px solid #2b3d63;z-index:2}}h1{{margin:0 0 8px}}header p,.body p{{color:#9dafca}}main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:18px;padding:28px 5vw}}.card{{background:#141f36;border:1px solid #2c4068;border-radius:14px;overflow:hidden;box-shadow:0 10px 24px #0005}}.frame{{aspect-ratio:9/16;max-height:300px;background:linear-gradient(150deg,#263b62,#10182b 65%);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;color:#65e1d2}}.frame b{{font-size:22px;color:white}}.frame span,.frame small{{padding:5px 10px;border:1px solid #4d6695;border-radius:99px}}.body{{padding:16px}}.meta{{margin:0 0 8px;color:#7f90ad!important;font-size:12px}}h3{{font-size:15px;line-height:1.55;margin:0 0 10px}}.dialogue{{min-height:48px;color:#fff!important;border-left:3px solid #68d9ca;padding-left:10px}}footer{{color:#f2b65c;font-size:12px;border-top:1px solid #2c4068;padding-top:12px}}.warn{{background:#3a2a12;border:1px solid #6b4a1c;color:#f7c877!important;padding:10px 14px;border-radius:10px}}</style></head>
    <body><header><h1>{episode} 可视化分镜</h1><p>来源：{html.escape(source_name)} · {scene_count} 场 / {len(shots)} 镜 / {dialogue_count} 句台词 · 预计 {total_seconds} 秒。请先审核镜头、角色和对白，再在工作台批量生成。</p>{warning}</header><main>{''.join(cards)}</main></body></html>"""
    target = project / "03_storyboard" / f"{episode}-visual-storyboard.html"
    target.write_text(document, encoding="utf-8")
    return target


def import_script(project: Path, source: Path, episode: str, overwrite: bool = False) -> dict[str, object]:
    project = project.resolve(); episode = episode.upper()
    text = read_script(source)
    shots = parse_script(text, episode)
    script_target = project / "02_script" / f"{episode}-script.md"
    shotlist_target = project / "03_storyboard" / f"{episode}-shotlist.csv"
    existing = [path for path in (script_target, shotlist_target) if path.exists()]
    if existing and not overwrite:
        raise FileExistsError("目标集已有剧本或镜头表。")
    if existing:
        backup = project / "09_automation" / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
        backup.mkdir(parents=True, exist_ok=True)
        for path in existing:
            shutil.copy2(path, backup / path.name)
    script_target.parent.mkdir(parents=True, exist_ok=True)
    script_target.write_text(f"# {episode} 上传剧本\n\n> 原始文件：{source.name}\n\n{text.strip()}\n", encoding="utf-8")
    shotlist_target.parent.mkdir(parents=True, exist_ok=True)
    with shotlist_target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SHOT_FIELDS)
        writer.writeheader(); writer.writerows(shots)
    storyboard = render_storyboard(project, episode, shots, source.name)
    config_path = project / "09_automation" / "video-provider.json"
    config = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.exists() else {}
    config["selected_episode"] = episode
    config.setdefault("episode_duration_seconds", sum(int(shot["duration_s"]) for shot in shots))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "episode": episode,
        "shots": len(shots),
        "scenes": len({shot["scene_id"] for shot in shots}),
        "dialogues": sum(1 for shot in shots if shot["dialogue_text"] != "无台词"),
        "seconds": sum(int(shot["duration_s"]) for shot in shots),
        "storyboard": storyboard,
        "shotlist": shotlist_target,
        "script": script_target,
    }
