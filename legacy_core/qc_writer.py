"""把自动质检结果写回 07_qc/<集>-qc.md（标记块，重复运行只更新不叠加）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

BEGIN = "<!-- VOICE-QC:BEGIN -->"
END = "<!-- VOICE-QC:END -->"


def append_voice_section(project: Path, episode: str, report: dict[str, Any]) -> Path | None:
    path = Path(project) / "07_qc" / f"{episode}-qc.md"
    if not path.parent.exists():
        return None
    text_ok = report.get("passed") == report.get("total")
    voice_ok = bool(report.get("voice_consistent"))
    voice_map = report.get("voice_map") or {}
    mapping = "、".join(f"{speaker}={names[0]}" for speaker, names in voice_map.items()) or "（原生音轨，无独立配音）"
    lines = [
        BEGIN,
        "## 配音质检（自动 + 人工）",
        "",
        f"- [{'x' if text_ok else ' '}] 台词与剧本逐字一致：{report.get('passed')}/{report.get('total')}",
        f"- [{'x' if voice_ok else ' '}] 声线全集一致：{mapping}",
        "- [ ] 口型与对白逐句对齐（人工看回放）",
        "- [ ] 表演与情绪贴合台词（人工审）",
        END,
    ]
    block = "\n".join(lines)
    text = path.read_text(encoding="utf-8-sig") if path.exists() else f"# {episode} 质检单\n"
    if BEGIN in text and END in text:
        head = text.split(BEGIN)[0]
        tail = text.split(END, 1)[1] if END in text else ""
        text = head + block + tail
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    path.write_text(text, encoding="utf-8")
    return path
