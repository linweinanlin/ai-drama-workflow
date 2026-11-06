"""外围适配器：原始 Tkinter 源文件按字节保留，不修改旧工作流内核。

Tk 主循环独占子进程；Qt 通过 Windows 原生窗口句柄挂载到堆叠页。
只在本进程内修改应用品牌常量与已生成 Label 文案，不替换生成/质检/渲染函数。
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def emit(kind, **data):
    print("@@LEGACY " + json.dumps({"type": kind, **data}, ensure_ascii=False), flush=True)


def enable_dpi_awareness():
    """让 Tk 子进程按真实 DPI 渲染。

    不声明 DPI 感知时，系统会把 96 DPI 的整窗位图拉伸到 125%/150%，文字发虚。
    """
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except (AttributeError, OSError):
        pass



def resolve_shot_video(project, episode, shot):
    """旧内核只认 05_generation/videos/<集>/<镜>.mp4。

    旧 wan3 管线的逐镜视频实际落在 06_edit/renders/（带版本后缀），并且已经写进
    分镜表的 output_path 列；不认这一列就会出现“状态已完成、分镜却没画面”。
    """
    if not project:
        return None
    shot_id = str(shot.get("shot_id") or "").strip()
    recorded = str(shot.get("output_path") or "").strip()
    if recorded:
        candidate = Path(recorded)
        if not candidate.is_absolute():
            candidate = Path(project) / candidate
        if candidate.is_file():
            return candidate
    video = Path(project) / "05_generation" / "videos" / episode / f"{shot_id}.mp4"
    if video.is_file():
        return video
    renders = Path(project) / "06_edit" / "renders"
    if shot_id and renders.is_dir():
        matches = [path for path in renders.glob(f"{shot_id}*.mp4") if path.is_file()]
        if matches:
            return max(matches, key=lambda path: path.stat().st_mtime)
    return None


def _ffmpeg_bin(legacy):
    try:
        found = legacy.batch_video_engine.find_ffmpeg()
        if found:
            return found
    except Exception:  # noqa: BLE001
        pass
    return "ffmpeg"


def install_kernel_fixes(legacy, storyboard):
    """在适配器层修正旧内核的两处硬编码，legacy_core 的字节保持原样。

    - 取图/打开视频：按 output_path → videos/<集> → 06_edit/renders 的顺序找素材；
    - 成片候选：只允许本集文件，不再把导出目录里最新的 mp4 当成本集成片。
    """
    import subprocess

    def shot_video(self, shot):
        return resolve_shot_video(self.app.project_path, self.app._selected_generation_episode(), shot)

    def thumb_path(self, shot):
        project = self.app.project_path
        if not project:
            return None
        shot_id = shot.get("shot_id", "")
        cache_dir = Path(project) / "03_storyboard" / "thumbs"
        cache = cache_dir / f"{shot_id}-first.jpg"
        video = shot_video(self, shot)
        if video is not None:
            try:
                if not cache.exists() or cache.stat().st_mtime < video.stat().st_mtime:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    subprocess.run(
                        [_ffmpeg_bin(legacy), "-y", "-hide_banner", "-loglevel", "error", "-ss", "0.2",
                         "-i", str(video), "-frames:v", "1", "-vf", "scale=270:-1", str(cache)],
                        capture_output=True, timeout=30, creationflags=0x08000000,
                    )
                if cache.exists():
                    return cache
            except (OSError, subprocess.SubprocessError):
                pass
        for key in ("prev_last_frame", "next_first_frame", "scene_refs", "identity_refs"):
            for item in str(shot.get(key) or "").split("|"):
                candidate = Path(project) / item.strip()
                if candidate.is_file() and candidate.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                    return candidate
        return None

    def open_file(self, shot_id, kind):
        project = self.app.project_path
        if not project:
            return
        shot = self.cards.get(shot_id, {})
        if kind == "video":
            video = shot_video(self, shot)
            if video is not None:
                self.app._open_path(video)
            else:
                from tkinter import messagebox

                messagebox.showinfo("分镜", "这一镜还没有生成视频。")
            return
        path = thumb_path(self, shot)
        if path:
            self.app._open_path(path)

    def final_video_candidates(self):
        if not self.project_path:
            return []
        episode = self._selected_generation_episode()
        exports_dir = self.project_path / "06_edit" / "exports"
        candidates = [
            exports_dir / f"{episode}-auto-final-subtitled.mp4",
            exports_dir / f"{episode}-auto-final-synced.mp4",
            exports_dir / f"{episode}-auto-final-with-audio.mp4",
            exports_dir / f"{episode}-auto-final.mp4",
        ]
        for row in reversed(self.release_rows):
            relative = row.get("video_path", "")
            if not relative:
                continue
            owner = re.match(r"(EP\d{3})", Path(relative).name)
            if owner and owner.group(1) != episode:
                continue
            candidates.append(self.project_path / relative)
        if exports_dir.is_dir():
            # 老集没有 auto-final 命名（如 EP001-final-performance-lipsync-v6.mp4），按本集前缀收口。
            candidates.extend(sorted(
                (path for path in exports_dir.glob(f"{episode}-*.mp4") if "final" in path.name.lower()),
                key=lambda path: path.stat().st_mtime, reverse=True,
            ))
            candidates.extend(sorted(
                exports_dir.glob(f"{episode}-*.mp4"),
                key=lambda path: path.stat().st_mtime, reverse=True,
            ))
        unique = []
        for path in candidates:
            if path.exists() and path not in unique:
                unique.append(path)
        return unique

    storyboard.STATUS_STYLE.update({
        "APPROVED": ("已通过", "#49d17d"),
        "REVIEW": ("待确认", "#f4b860"),
    })
    storyboard.StoryboardPanel._shot_video = shot_video
    storyboard.StoryboardPanel._thumb_path = thumb_path
    storyboard.StoryboardPanel._open_file = open_file
    legacy.StudioApp._final_video_candidates = final_video_candidates


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    enable_dpi_awareness()
    # 与原入口保留完全相同的 worker 分发，旧 UI 生成的命令仍调用旧执行器。
    sys.path.insert(0, str(ROOT / "legacy_core"))
    import short_drama_studio as legacy
    if "--batch-worker" in sys.argv:
        args = sys.argv[1:]
        args.remove("--batch-worker")
        return legacy.batch_video_engine.main(args)
    import storyboard_panel as storyboard

    install_kernel_fixes(legacy, storyboard)
    legacy.APP_NAME = "AI 漫剧自动化工作流"
    legacy.CONFIG_PATH = ROOT / "legacy_app_config.json"
    commands = queue.Queue()

    class MountedStudio(legacy.StudioApp):
        def _build_sidebar(self):
            super()._build_sidebar()
            # 只修改品牌标签；原模块完整的项目选择、功能菜单和交互保持原样。
            replacements = {"AI SHORT DRAMA": "AI DRAMA WORKFLOW", "自动化工作台": "漫剧工作流"}
            def visit(widget):
                if widget.winfo_class() in ("Label", "TLabel"):
                    text = str(widget.cget("text"))
                    if text in replacements:
                        widget.configure(text=replacements[text])
                    elif text.startswith("LOCAL STUDIO"):
                        widget.configure(text=f"AI DRAMA WORKFLOW · v{legacy.APP_VERSION}")
                for child in widget.winfo_children():
                    visit(child)
            visit(self.sidebar)

    app = MountedStudio()
    app.title("AI 漫剧自动化工作流")
    app.update_idletasks()
    if os.name == "nt":
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        hwnd = user32.GetAncestor(app.winfo_id(), 2)  # GA_ROOT，得到 Tk 包装顶层 HWND。
        emit("ready", hwnd=int(hwnd), title=app.title())
    else:
        emit("ready", hwnd=0, title=app.title())

    def read_commands():
        for line in sys.stdin:
            try:
                commands.put(json.loads(line))
            except ValueError:
                pass

    threading.Thread(target=read_commands, daemon=True).start()

    def poll():
        while not commands.empty():
            item = commands.get()
            if item.get("command") == "close":
                if not app.busy:
                    app.on_close()  # 仍调用原有关闭与保存逻辑。
                    return
                emit("state", busy=True)
            elif item.get("command") == "describe":
                texts = []
                def collect(widget):
                    if widget.winfo_class() in ("Label", "TLabel"):
                        texts.append(str(widget.cget("text")))
                    for child in widget.winfo_children():
                        collect(child)
                collect(app.sidebar)
                emit("description", title=app.title(), labels=texts, project=str(app.project_path), geometry=app.winfo_geometry())
        emit("state", busy=bool(app.busy))
        app.after(800, poll)

    app.after(200, poll)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
