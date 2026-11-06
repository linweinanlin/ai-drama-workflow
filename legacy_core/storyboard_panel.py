"""分镜墙：带缩略图的卡片画布，支持拖拽排序、场景分组、首尾帧连线与详情弹窗。"""
from __future__ import annotations

import csv
import json
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Any

from PIL import Image, ImageTk

CARD_W = 214
CARD_H = 392
THUMB_W = 132
THUMB_H = 235
GAP_X = 26
GAP_Y = 56
STATUS_STYLE = {
    "DRAFT": ("待生成", "#6f7f9e"),
    "PENDING": ("待生成", "#6f7f9e"),
    "SUBMITTED": ("生成中", "#f4b860"),
    "QUEUED": ("生成中", "#f4b860"),
    "RUNNING": ("生成中", "#f4b860"),
    "PROCESSING": ("生成中", "#f4b860"),
    "SUCCEEDED": ("已完成", "#49d17d"),
    "COMPLETED": ("已完成", "#49d17d"),
    "FAILED": ("返工", "#f26d78"),
    "CANCELED": ("返工", "#f26d78"),
    "RETRY": ("返工", "#f26d78"),
}
SCENE_COLORS = ["#1b2741", "#182338", "#1e2b3f", "#16202f"]


class StoryboardPanel(tk.Frame):
    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, bg=app.BG)
        self.app = app
        self.images: dict[str, ImageTk.PhotoImage] = {}
        self.cards: dict[str, dict[str, Any]] = {}
        self.positions: dict[str, tuple[float, float]] = {}
        self.voice: dict[str, dict[str, str]] = {}
        self.drag: dict[str, Any] | None = None
        self._build()
        self.refresh()

    # ---------- 骨架 ----------
    def _build(self) -> None:
        app = self.app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        bar = tk.Frame(self, bg=app.BG)
        bar.grid(row=0, column=0, sticky="ew", pady=(8, 10))
        ttk.Label(bar, text="分镜墙", style="Header.TLabel").pack(side="left")
        self.status_label = tk.Label(bar, text="", bg=app.BG, fg=app.MUTED, font=("Microsoft YaHei UI", 9))
        self.status_label.pack(side="left", padx=(12, 0))
        self._mini(bar, "刷新", self.refresh).pack(side="right")
        self._mini(bar, "打开可视化分镜页", lambda: self.app.open_selected_storyboard()).pack(side="right", padx=(0, 8))

        holder = tk.Frame(self, bg=app.BG)
        holder.grid(row=1, column=0, sticky="nsew")
        self.canvas = tk.Canvas(holder, bg=app.BG, highlightthickness=0, bd=0, width=1)
        y_scroll = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        x_scroll = ttk.Scrollbar(holder, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        y_scroll.pack(side="right", fill="y")
        x_scroll.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Enter>", lambda _e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

    def _mini(self, parent, text: str, command) -> tk.Button:
        return tk.Button(
            parent, text=text, command=command, bg=self.app.PANEL_2, fg=self.app.TEXT,
            activebackground=self.app.BLUE, activeforeground=self.app.TEXT,
            relief="flat", bd=0, cursor="hand2", padx=10, pady=5,
            font=("Microsoft YaHei UI", 9),
        )

    def _wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    # ---------- 刷新 ----------
    def refresh(self) -> None:
        app = self.app
        self.canvas.delete("all")
        self.images.clear()
        self.cards.clear()
        self.positions.clear()
        shots = list(app.shots)
        if not shots:
            self.canvas.create_text(360, 120, text="这一集还没有分镜。先上传剧本生成分镜。",
                                    fill=app.MUTED, font=("Microsoft YaHei UI", 11))
            self.status_label.configure(text="无分镜", fg=app.AMBER)
            return
        states = self.app._shot_states(self.app._selected_generation_episode())
        for shot in shots:
            state = states.get(shot.get("shot_id", ""), {})
            if state.get("status"):
                shot["_state_status"] = str(state["status"])
        self.voice = self._load_voice(shots)
        self.status_label.configure(text=f"{len(shots)} 个镜头 · 拖拽卡片可调整顺序 · 点击看大图",
                                    fg=app.MUTED)
        scenes = self._scene_names()
        self._draw_legend(scenes, shots)
        per_row = 4
        x0, y0 = 40.0, 74.0
        layout: list[tuple[dict[str, str], float, float]] = []
        for index, shot in enumerate(shots):
            column = index % per_row
            row = index // per_row
            x = x0 + column * (CARD_W + GAP_X)
            y = y0 + row * (CARD_H + GAP_Y)
            layout.append((shot, x, y))
            self.cards[shot.get("shot_id", "")] = shot
            self.positions[shot.get("shot_id", "")] = (x, y)
        rows = (len(shots) + per_row - 1) // per_row
        height = y0 + rows * (CARD_H + GAP_Y) + 40
        width = x0 + per_row * (CARD_W + GAP_X) + 40
        self.canvas.configure(scrollregion=(0, 0, max(width, 1000), max(height, 400)))
        # 先画连线、后画卡片：连线始终位于卡片下层，不会压住卡片文字或标签
        self._draw_connectors()
        for shot, x, y in layout:
            self._draw_card(shot, x, y, scenes)

    def _scene_names(self) -> dict[str, str]:
        names: dict[str, str] = {}
        project = self.app.project_path
        if not project:
            return names
        path = project / "02_script" / f"{self.app._selected_generation_episode()}-analysis.json"
        if not path.exists():
            return names
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            return names
        for scene in data.get("scenes", []):
            location = scene.get("location") or ""
            time_of_day = scene.get("time_of_day") or ""
            if location and location != "待补充":
                names[scene.get("scene_id", "")] = f"{location}{(' · ' + time_of_day) if time_of_day and time_of_day != '待补充' else ''}"
        return names

    def _draw_legend(self, scenes: dict[str, str], shots: list[dict[str, str]]) -> None:
        app = self.app
        ids: list[str] = []
        for shot in shots:
            scene_id = shot.get("scene_id", "")
            if scene_id and scene_id not in ids:
                ids.append(scene_id)
        x = 40.0
        self.canvas.create_text(x, 30, anchor="w", text="场景：", fill=app.MUTED,
                                font=("Microsoft YaHei UI", 9, "bold"))
        x += 52
        for scene_id in ids:
            color = SCENE_COLORS[abs(hash(scene_id)) % len(SCENE_COLORS)]
            self.canvas.create_rectangle(x, 20, x + 16, 38, fill=color, outline=app.CYAN)
            label = f"{scene_id} {scenes.get(scene_id, '')}".strip()
            self.canvas.create_text(x + 22, 29, anchor="w", text=label, fill=app.MUTED,
                                    font=("Microsoft YaHei UI", 9))
            width = sum(12 if ord(ch) > 127 else 7 for ch in label)
            x += 22 + width + 30

    def _load_voice(self, shots: list[dict[str, str]]) -> dict[str, dict[str, str]]:
        project = self.app.project_path
        if not project:
            return {}
        ledger = project / "05_generation" / "audio" / f"{self.app._selected_generation_episode()}-voice-jobs.csv"
        if not ledger.exists():
            return {}
        with ledger.open("r", encoding="utf-8-sig", newline="") as handle:
            return {row.get("shot_id", ""): row for row in csv.DictReader(handle) if row.get("shot_id")}

    # ---------- 卡片 ----------
    def _draw_card(self, shot: dict[str, str], x: float, y: float, scenes: dict[str, str] | None = None) -> None:
        app = self.app
        shot_id = shot.get("shot_id", "")
        status_key = str(shot.get("_state_status") or shot.get("status") or "DRAFT").upper()
        status_text, status_color = STATUS_STYLE.get(status_key, ("待生成", "#6f7f9e"))
        scene_id = shot.get("scene_id", "")
        scene_color = SCENE_COLORS[abs(hash(scene_id)) % len(SCENE_COLORS)]
        tag = f"card:{shot_id}"

        self.canvas.create_rectangle(x, y, x + CARD_W, y + CARD_H, fill=scene_color,
                                     outline=status_color if status_key in ("FAILED", "RETRY") else app.BORDER,
                                     width=2 if status_key in ("FAILED", "RETRY") else 1, tags=tag)
        self.canvas.create_rectangle(x, y, x + CARD_W, y + 5, fill=scene_color, outline="", tags=tag)
        thumb = self._thumb(shot)
        if thumb is not None:
            self.images[shot_id] = thumb
            self.canvas.create_image(x + CARD_W / 2, y + 10 + THUMB_H / 2, image=thumb, tags=tag)
        else:
            self.canvas.create_rectangle(x + (CARD_W - THUMB_W) / 2, y + 10,
                                         x + (CARD_W + THUMB_W) / 2, y + 10 + THUMB_H,
                                         fill="#101a2c", outline=app.BORDER, tags=tag)
            self.canvas.create_text(x + CARD_W / 2, y + 10 + THUMB_H / 2 - 10, text="待生成首帧",
                                    fill=app.MUTED, font=("Microsoft YaHei UI", 10), tags=tag)
            self.canvas.create_text(x + CARD_W / 2, y + 10 + THUMB_H / 2 + 12, text="🖼", fill=app.MUTED,
                                    font=("Segoe UI Emoji", 16), tags=tag)

        ty = y + THUMB_H + 18
        self.canvas.create_text(x + 12, ty, anchor="w", text=shot_id,
                                fill=app.TEXT, font=("Segoe UI", 10, "bold"), tags=tag)
        self.canvas.create_text(x + CARD_W - 12, ty, anchor="e", text=status_text,
                                fill=status_color, font=("Microsoft YaHei UI", 9, "bold"), tags=tag)
        scene_label = (scenes or {}).get(scene_id, scene_id)
        badge_text = f"{scene_id} {scene_label}".strip()[:20]
        badge_font = tkfont.Font(root=self, family="Microsoft YaHei UI", size=8)
        while badge_font.measure(badge_text) + 14 > CARD_W - 12 and len(badge_text) > 4:
            badge_text = badge_text[:-1]
        badge_w = badge_font.measure(badge_text) + 14
        self.canvas.create_rectangle(x + 6, y + 7, x + 6 + badge_w, y + 26, fill="#0b1020",
                                     outline="", tags=tag)
        self.canvas.create_text(x + 12, y + 17, anchor="w", text=badge_text,
                                fill=app.TEXT, font=badge_font, tags=tag)
        ty += 20
        size = shot.get("shot_size") or "—"
        camera = shot.get("camera") or "—"
        duration = shot.get("duration_s") or "—"
        self.canvas.create_text(x + 12, ty, anchor="w", text=f"{size} · {camera} · {duration}s",
                                fill=app.MUTED, font=("Microsoft YaHei UI", 8), tags=tag)
        ty += 18
        action = (shot.get("action") or "").strip()
        self.canvas.create_text(x + 12, ty, anchor="nw", text=action[:40] + ("…" if len(action) > 40 else ""),
                                fill=app.TEXT, font=("Microsoft YaHei UI", 9), width=CARD_W - 24,
                                tags=tag)
        ty += 44
        dialogue = (shot.get("dialogue_text") or shot.get("dialogue_or_vo") or "").strip()
        speaker = (shot.get("speaker") or "").strip()
        if dialogue and dialogue != "无台词":
            line = f"{speaker}：{dialogue}" if speaker and speaker != "无" else dialogue
            self.canvas.create_text(x + 12, ty, anchor="nw", text=line[:26] + ("…" if len(line) > 26 else ""),
                                    fill=app.CYAN, font=("Microsoft YaHei UI", 8), width=CARD_W - 24, tags=tag)
        else:
            self.canvas.create_text(x + 12, ty, anchor="nw", text="无台词",
                                    fill=app.MUTED, font=("Microsoft YaHei UI", 8), tags=tag)
        ty += 34
        emotion = shot.get("emotion") or ""
        risk = shot.get("risk_level") or ""
        self.canvas.create_text(x + 12, ty, anchor="w", text=f"{emotion} · {risk}风险",
                                fill=app.MUTED, font=("Microsoft YaHei UI", 8), tags=tag)
        voice = self.voice.get(shot_id)
        if voice:
            self.canvas.create_text(x + 12, ty + 16, anchor="w", text="🔊 已配音",
                                    fill=app.GREEN, font=("Microsoft YaHei UI", 8, "bold"), tags=tag)
        self.cards[shot_id] = shot
        self.positions[shot_id] = (x, y)

    def _thumb(self, shot: dict[str, str]) -> ImageTk.PhotoImage | None:
        path = self._thumb_path(shot)
        if not path:
            return None
        try:
            image = Image.open(path)
            image.thumbnail((THUMB_W, THUMB_H))
            if image.mode != "RGB":
                image = image.convert("RGB")
            return ImageTk.PhotoImage(image)
        except Exception:  # noqa: BLE001
            return None

    def _thumb_path(self, shot: dict[str, str]) -> Path | None:
        project = self.app.project_path
        if not project:
            return None
        shot_id = shot.get("shot_id", "")
        episode = self.app._selected_generation_episode()
        cache_dir = project / "03_storyboard" / "thumbs"
        cache = cache_dir / f"{shot_id}-first.jpg"
        video = project / "05_generation" / "videos" / episode / f"{shot_id}.mp4"
        if video.exists():
            try:
                if not cache.exists() or cache.stat().st_mtime < video.stat().st_mtime:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    subprocess.run(
                        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "0.2",
                         "-i", str(video), "-frames:v", "1", "-vf", "scale=270:-1", str(cache)],
                        capture_output=True, timeout=30, creationflags=0x08000000,
                    )
                if cache.exists():
                    return cache
            except (OSError, subprocess.SubprocessError):
                pass
        for key in ("prev_last_frame", "next_first_frame", "scene_refs", "identity_refs"):
            for item in str(shot.get(key) or "").split("|"):
                candidate = project / item.strip()
                if candidate.is_file() and candidate.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                    return candidate
        return None

    # ---------- 连线 ----------
    def _draw_connectors(self) -> None:
        app = self.app
        order = [shot.get("shot_id") for shot in self.app.shots]
        for left_id, right_id in zip(order, order[1:]):
            if left_id not in self.positions or right_id not in self.positions:
                continue
            lx, ly = self.positions[left_id]
            rx, ry = self.positions[right_id]
            if abs(ly - ry) < 1:
                self.canvas.create_line(lx + CARD_W, ly + CARD_H / 2, rx, ry + CARD_H / 2,
                                        fill=app.BORDER, width=2, arrow="last", arrowshape=(8, 10, 4))
                self.canvas.create_text((lx + CARD_W + rx) / 2, ly + CARD_H / 2 - 12,
                                        text=self.cards.get(right_id, {}).get("transition") or "承接",
                                        fill=app.MUTED, font=("Microsoft YaHei UI", 8))
            else:
                sx, sy = lx + CARD_W / 2, ly + CARD_H
                ex, ey = rx + CARD_W / 2, ry - 8
                mid_y = max(sy, ey) + GAP_Y / 2
                self.canvas.create_line(sx, sy, sx, mid_y, ex, mid_y, ex, ey,
                                        fill=app.BORDER, width=2, arrow="last", arrowshape=(8, 10, 4))
                self.canvas.create_text((sx + ex) / 2, mid_y - 8,
                                        text=self.cards.get(right_id, {}).get("transition") or "承接",
                                        fill=app.MUTED, font=("Microsoft YaHei UI", 8))

    # ---------- 交互 ----------
    def _hit_card(self, x: float, y: float) -> str | None:
        for shot_id, (cx, cy) in self.positions.items():
            if cx <= x <= cx + CARD_W and cy <= y <= cy + CARD_H:
                return shot_id
        return None

    def _on_press(self, event) -> None:
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        shot_id = self._hit_card(x, y)
        if shot_id:
            self.drag = {"shot_id": shot_id, "x": x, "y": y, "moved": False}

    def _on_motion(self, event) -> None:
        if not self.drag:
            return
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        if abs(x - self.drag["x"]) > 6 or abs(y - self.drag["y"]) > 6:
            self.drag["moved"] = True
            self.canvas.configure(cursor="fleur")

    def _on_release(self, event) -> None:
        if not self.drag:
            return
        self.canvas.configure(cursor="")
        drag = self.drag
        self.drag = None
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        target = self._hit_card(x, y)
        if not drag["moved"]:
            if drag["shot_id"]:
                self._open_detail(drag["shot_id"])
            return
        if target and target != drag["shot_id"]:
            self._reorder(drag["shot_id"], target)

    def _reorder(self, moving: str, target: str) -> None:
        project = self.app.project_path
        episode = self.app._selected_generation_episode()
        if not project:
            return
        path = project / "03_storyboard" / f"{episode}-shotlist.csv"
        if not path.exists():
            return
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            rows = list(reader)
        order = [row.get("shot_id") for row in rows]
        if moving not in order or target not in order:
            return
        order.remove(moving)
        order.insert(order.index(target), moving)
        lookup = {row.get("shot_id"): row for row in rows}
        for index, shot_id in enumerate(order, start=1):
            lookup[shot_id]["order"] = str(index)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows([lookup[shot_id] for shot_id in order])
        self.app.log(f"分镜顺序已保存：{moving} → {target} 之前")
        self.app.load_project()

    # ---------- 详情 ----------
    def _open_detail(self, shot_id: str) -> None:
        app = self.app
        shot = self.cards.get(shot_id)
        if not shot:
            return
        window = tk.Toplevel(self)
        window.title(f"分镜详情 · {shot_id}")
        window.geometry("980x780")
        window.configure(bg=app.BG)
        window.transient(self.winfo_toplevel())
        left = tk.Frame(window, bg=app.BG)
        left.pack(side="left", fill="y", padx=(18, 10), pady=18)
        big_path = self._thumb_path(shot)
        if big_path:
            try:
                image = Image.open(big_path)
                image.thumbnail((380, 660))
                photo = ImageTk.PhotoImage(image.convert("RGB"))
                label = tk.Label(left, image=photo, bg=app.BG)
                label.image = photo
                label.pack()
            except Exception:  # noqa: BLE001
                tk.Label(left, text="图片加载失败", bg=app.BG, fg=app.MUTED).pack()
        else:
            placeholder = tk.Frame(left, bg=app.PANEL, width=380, height=560,
                                   highlightbackground=app.BORDER, highlightthickness=1)
            placeholder.pack()
            tk.Label(placeholder, text="待生成首帧", bg=app.PANEL, fg=app.MUTED,
                     font=("Microsoft YaHei UI", 12)).place(relx=0.5, rely=0.5, anchor="center")

        right = tk.Frame(window, bg=app.BG)
        right.pack(side="left", fill="both", expand=True, padx=(10, 18), pady=18)
        tk.Label(right, text=shot_id, bg=app.BG, fg=app.TEXT,
                 font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        status_key = str(shot.get("status") or "DRAFT").upper()
        status_text, status_color = STATUS_STYLE.get(status_key, ("待生成", "#6f7f9e"))
        tk.Label(right, text=status_text, bg=app.PANEL_2, fg=status_color, padx=10, pady=3,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w", pady=(4, 10))

        pairs = [
            ("场景", shot.get("scene_id")),
            ("景别 / 机位", f"{shot.get('shot_size')} · {shot.get('camera')}"),
            ("时长", f"{shot.get('duration_s')} 秒"),
            ("转场", shot.get("transition")),
            ("角色", shot.get("characters")),
            ("说话人", shot.get("speaker")),
            ("台词", shot.get("dialogue_text") or shot.get("dialogue_or_vo")),
            ("情绪 / 弧线", f"{shot.get('emotion')} · {shot.get('emotion_arc')}"),
            ("风险", shot.get("risk_level")),
            ("画面动作", shot.get("action")),
            ("入场", shot.get("start_state")),
            ("末态", shot.get("end_state")),
        ]
        for label, value in pairs:
            row = tk.Frame(right, bg=app.BG)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, bg=app.BG, fg=app.MUTED, width=10, anchor="w",
                     font=("Microsoft YaHei UI", 9)).pack(side="left")
            tk.Label(row, text=str(value or "—"), bg=app.BG, fg=app.TEXT, anchor="w", justify="left",
                     wraplength=470, font=("Microsoft YaHei UI", 9)).pack(side="left")

        voice = self.voice.get(shot_id)
        if voice:
            box = tk.Frame(right, bg=app.PANEL, highlightbackground=app.BORDER, highlightthickness=1)
            box.pack(fill="x", pady=(8, 4))
            tk.Label(box, text="配音登记", bg=app.PANEL, fg=app.CYAN,
                     font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w", padx=10, pady=(6, 2))
            for key, label in (("speaker", "说话人"), ("text", "台词"), ("emotion", "情绪"),
                              ("rate", "语速"), ("audio_path", "音频"), ("align", "对齐方式")):
                tk.Label(box, text=f"{label}：{voice.get(key, '—')}", bg=app.PANEL, fg=app.MUTED,
                         anchor="w", wraplength=520, justify="left",
                         font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=10)
            tk.Frame(box, height=6, bg=app.PANEL).pack()

        buttons = tk.Frame(right, bg=app.BG)
        buttons.pack(fill="x", pady=(10, 0))
        self._mini(buttons, "打开视频", lambda: self._open_file(shot_id, "video")).pack(side="left")
        self._mini(buttons, "打开参考图", lambda: self._open_file(shot_id, "ref")).pack(side="left", padx=6)
        self._mini(buttons, "本镜上下文", lambda: self._show_context(shot_id)).pack(side="left", padx=6)
        self._mini(buttons, "关闭", window.destroy).pack(side="right")

    def _open_file(self, shot_id: str, kind: str) -> None:
        project = self.app.project_path
        if not project:
            return
        episode = self.app._selected_generation_episode()
        shot = self.cards.get(shot_id, {})
        if kind == "video":
            video = project / "05_generation" / "videos" / episode / f"{shot_id}.mp4"
            if video.exists():
                self.app._open_path(video)
            else:
                messagebox.showinfo("分镜", "这一镜还没有生成视频。")
            return
        path = self._thumb_path(shot)
        if path:
            self.app._open_path(path)

    def _show_context(self, shot_id: str) -> None:
        import context_pack

        project = self.app.project_path
        if not project:
            return
        episode = self.app._selected_generation_episode()
        shot = self.cards.get(shot_id, {})
        payload = context_pack.shot_context(project, episode, shot, self.app._video_provider_config())
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        window = tk.Toplevel(self)
        window.title(f"本镜上下文 · {shot_id}")
        window.geometry("720x600")
        window.configure(bg=self.app.BG)
        tk.Label(window, text=f"{shot_id} 只携带这些上下文（{len(text)} 字符）", bg=self.app.BG,
                 fg=self.app.MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=14, pady=8)
        box = tk.Text(window, bg="#080d19", fg="#c8d3ea", relief="flat", font=("Cascadia Mono", 9),
                      wrap="word", padx=12, pady=10)
        box.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        box.insert("1.0", text)
        box.configure(state="disabled")
