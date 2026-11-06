"""剧本分析面板：角色卡、五段结构、情绪曲线、关系图、场景道具。"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

import analysis_engine

STAGE_LABELS = [
    ("hook", "钩子", "#f26d78"),
    ("setup", "建立", "#5d8cff"),
    ("escalation", "升级", "#f4b860"),
    ("reversal", "反转", "#33d6c9"),
    ("cliffhanger", "悬念", "#b06cff"),
]


class AnalysisPanel(tk.Frame):
    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, bg=app.BG)
        self.app = app
        self.data: dict[str, Any] | None = None
        self._build()
        self.refresh()

    # ---------- 骨架 ----------
    def _build(self) -> None:
        app = self.app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        bar = tk.Frame(self, bg=app.BG)
        bar.grid(row=0, column=0, sticky="ew", pady=(8, 10))
        ttk.Label(bar, text="剧本分析", style="Header.TLabel").pack(side="left")
        self.status_label = tk.Label(
            bar, text="", bg=app.BG, fg=app.MUTED, font=("Microsoft YaHei UI", 9),
        )
        self.status_label.pack(side="left", padx=(12, 0))
        self._mini(bar, "重新分析", self.reanalyze).pack(side="right")
        self._mini(bar, "上传剧本", lambda: self.app.import_script_and_storyboard()).pack(side="right", padx=(0, 8))

        holder = tk.Frame(self, bg=app.BG)
        holder.grid(row=1, column=0, sticky="nsew")
        self.canvas = tk.Canvas(holder, bg=app.BG, highlightthickness=0, bd=0, width=1)
        scrollbar = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=app.BG)
        self.window_id = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self.window_id, width=event.width))
        self.canvas.bind("<Enter>", lambda _e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))

    def _wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _mini(self, parent, text: str, command) -> tk.Button:
        return tk.Button(
            parent, text=text, command=command, bg=self.app.PANEL_2, fg=self.app.TEXT,
            activebackground=self.app.BLUE, activeforeground=self.app.TEXT,
            relief="flat", bd=0, cursor="hand2", padx=10, pady=5,
            font=("Microsoft YaHei UI", 9), anchor="center",
        )

    def _panel(self) -> tk.Frame:
        return tk.Frame(self.body, bg=self.app.PANEL, highlightbackground=self.app.BORDER, highlightthickness=1)

    # ---------- 刷新 ----------
    def refresh(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        project = self.app.project_path
        episode = self.app._selected_generation_episode()
        if not project:
            self.data = None
            self._render_empty("还没有选择项目。")
            return
        try:
            self.data = analysis_engine.load_analysis(project, episode)
        except Exception:
            self.data = None
        if not self.data:
            self._render_empty("这一集还没有分析结果。先上传剧本，系统会自动解析并缓存。")
            return
        self._render(self.data)

    def reanalyze(self) -> None:
        project = self.app.project_path
        if not project:
            return
        episode = self.app._selected_generation_episode()
        try:
            self.data = analysis_engine.analyze(project, episode, force=True)
        except FileNotFoundError as exc:
            messagebox.showwarning("剧本分析", f"找不到剧本：\n{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("剧本分析", f"解析失败：{exc}")
            return
        self._render(self.data)
        self.status_label.configure(text="已重新解析并缓存", fg=self.app.CYAN)

    # ---------- 空状态 ----------
    def _render_empty(self, text: str) -> None:
        app = self.app
        panel = self._panel()
        panel.pack(fill="both", expand=True, pady=(6, 0))
        box = tk.Frame(panel, bg=app.PANEL)
        box.pack(expand=True, pady=60)
        tk.Label(box, text="🎬", bg=app.PANEL, fg=app.MUTED, font=("Segoe UI", 30)).pack()
        tk.Label(
            box, text=text, bg=app.PANEL, fg=app.MUTED, wraplength=460,
            justify="center", font=("Microsoft YaHei UI", 11),
        ).pack(pady=(10, 16))
        tk.Button(
            box, text="上传剧本并生成分镜", command=lambda: self.app.import_script_and_storyboard(),
            bg=app.CYAN, fg=app.TEXT, relief="flat", bd=0, padx=16, pady=8,
            font=("Microsoft YaHei UI", 10, "bold"), cursor="hand2",
        ).pack()
        self.status_label.configure(text="未解析", fg=app.MUTED)

    # ---------- 主渲染 ----------
    def _render(self, data: dict[str, Any]) -> None:
        app = self.app
        summary = data.get("summary", {})
        cached = data.get("_cached")
        self.status_label.configure(
            text=("读取缓存（剧本未变，未重新解析）" if cached else "本次解析并已缓存")
            + f" · {data.get('source', {}).get('name', '')}",
            fg=app.CYAN if cached else app.AMBER,
        )

        chips = tk.Frame(self.body, bg=app.BG)
        chips.pack(fill="x", pady=(0, 8))
        for label, value in (
            ("场景", summary.get("scene_count", 0)),
            ("镜头", summary.get("shot_count", 0)),
            ("台词", summary.get("dialogue_lines", 0)),
            ("预计", f"{summary.get('estimated_seconds', 0)} 秒"),
            ("角色", summary.get("character_count", 0)),
        ):
            chip = tk.Label(
                chips, text=f"{label} {value}", bg=app.PANEL_2, fg=app.TEXT,
                padx=12, pady=6, font=("Microsoft YaHei UI", 9),
            )
            chip.pack(side="left", padx=(0, 8))

        self._render_stage_bar(data)
        self._render_curve(data)
        self._render_beats(data)
        self._render_characters(data)
        self._render_relationships(data)
        self._render_scenes(data)

    # ---------- 五段结构 ----------
    def _render_stage_bar(self, data: dict[str, Any]) -> None:
        app = self.app
        panel = self._panel()
        panel.pack(fill="x", pady=(0, 10))
        tk.Label(panel, text="故事结构（钩子 → 建立 → 升级 → 反转 → 悬念）", bg=app.PANEL, fg=app.TEXT,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", padx=16, pady=(12, 8))
        canvas = tk.Canvas(panel, height=64, bg=app.PANEL, highlightthickness=0)
        canvas.pack(fill="x", padx=16, pady=(0, 14))
        canvas.update_idletasks()
        width = max(600, canvas.winfo_width())

        totals: dict[str, list[float]] = {}
        for beat in data.get("beats", []):
            stage = beat.get("stage") or beat.get("key") or "setup"
            span = totals.setdefault(stage, [float(beat.get("start", 0)), float(beat.get("end", 0))])
            span[0] = min(span[0], float(beat.get("start", 0)))
            span[1] = max(span[1], float(beat.get("end", 0)))
        duration = max([span[1] for span in totals.values()] + [1.0])
        x = 0.0
        for key, label, color in STAGE_LABELS:
            span = totals.get(key)
            part = (span[1] - span[0]) if span else duration * 0.06
            seg = max(64.0, width * part / duration)
            canvas.create_rectangle(x, 6, x + seg - 4, 40, fill=color if span else "#26314a", outline="")
            canvas.create_text(x + (seg - 4) / 2, 23, text=label, fill="#0b1020" if span else app.MUTED,
                               font=("Microsoft YaHei UI", 10, "bold"))
            canvas.create_text(x + 2, 54, anchor="w",
                               text=f"{span[0]:.0f}–{span[1]:.0f}s" if span else "—",
                               fill=app.MUTED if span else app.BORDER, font=("Segoe UI", 8))
            x += seg
        self._stage_canvas = canvas

    # ---------- 情绪曲线 ----------
    def _render_curve(self, data: dict[str, Any]) -> None:
        app = self.app
        panel = self._panel()
        panel.pack(fill="x", pady=(0, 10))
        tk.Label(panel, text="情绪曲线（横轴时间 / 纵轴强度，标出钩子、反转、悬念）", bg=app.PANEL,
                 fg=app.TEXT, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", padx=16, pady=(12, 4))
        canvas = tk.Canvas(panel, height=190, bg=app.PANEL, highlightthickness=0)
        canvas.pack(fill="x", padx=16, pady=(0, 14))
        canvas.update_idletasks()
        width = max(600, canvas.winfo_width())
        height = 160
        left, top = 40, 16
        plot_w, plot_h = width - 60, height - 40

        for step in range(5):
            y = top + plot_h * step / 4
            canvas.create_line(left, y, left + plot_w, y, fill="#22304d")
            canvas.create_text(left - 8, y, anchor="e", text=f"{1 - step * 0.25:.2f}",
                               fill=app.MUTED, font=("Segoe UI", 8))
        curve = data.get("emotion_curve", [])
        if curve:
            total = max(float(item.get("t", 0)) for item in curve) or 1.0
            points = []
            for item in curve:
                x = left + plot_w * float(item.get("t", 0)) / total
                y = top + plot_h * (1 - float(item.get("intensity", 0.5)))
                points.append((x, y, item))
            canvas.create_line([coord for x, y, _ in points for coord in (x, y)], fill=app.CYAN, width=2, smooth=True)
            labelled: set[str] = set()
            for x, y, item in points:
                canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=app.CYAN, outline="")
                beat = item.get("beat")
                if beat in ("hook", "reversal", "cliffhanger") and beat not in labelled:
                    labelled.add(beat)
                    label = {"hook": "钩子", "reversal": "反转", "cliffhanger": "悬念"}[beat]
                    canvas.create_line(x, y, x, top + plot_h, fill="#f4b860", dash=(2, 3))
                    canvas.create_text(x, top - 6, text=label, fill=app.AMBER,
                                       font=("Microsoft YaHei UI", 8, "bold"))
                canvas.create_text(x, top + plot_h + 12, text=item.get("shot", "").split("-")[-1],
                                   fill=app.MUTED, font=("Segoe UI", 7))

    # ---------- 节拍表 ----------
    def _render_beats(self, data: dict[str, Any]) -> None:
        app = self.app
        panel = self._panel()
        panel.pack(fill="x", pady=(0, 10))
        tk.Label(panel, text="节拍分段（可见事件 / 结束状态）", bg=app.PANEL, fg=app.TEXT,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", padx=16, pady=(12, 8))
        table = tk.Frame(panel, bg=app.PANEL)
        table.pack(fill="x", padx=16, pady=(0, 14))
        for index, beat in enumerate(data.get("beats", [])):
            row = tk.Frame(table, bg=app.PANEL if index % 2 == 0 else app.PANEL_2)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f"{beat.get('stage', '')}", bg=row["bg"], fg=app.CYAN, width=8, anchor="w",
                     font=("Microsoft YaHei UI", 9, "bold")).pack(side="left", padx=(10, 0), pady=6)
            tk.Label(row, text=f"{float(beat.get('start', 0)):.0f}–{float(beat.get('end', 0)):.0f}s",
                     bg=row["bg"], fg=app.MUTED, width=9, anchor="w", font=("Segoe UI", 9)).pack(side="left")
            events = " / ".join(beat.get("visible_events", [])[:2]) or "待补充"
            tk.Label(row, text=events, bg=row["bg"], fg=app.TEXT, anchor="w",
                     font=("Microsoft YaHei UI", 9), wraplength=420, justify="left").pack(side="left", padx=8)
            tk.Label(row, text=beat.get("end_state") or "—", bg=row["bg"], fg=app.MUTED, anchor="e",
                     font=("Microsoft YaHei UI", 8), wraplength=220, justify="right").pack(side="right", padx=10)
            self._mini(row, "编辑", lambda b=beat: self._edit_beat(b)).pack(side="right", padx=6, pady=3)

    def _edit_beat(self, beat: dict[str, Any]) -> None:
        window, fields = self._form(f"编辑节拍 · {beat.get('label', '')}", [
            ("可见事件（每行一条）", "text", "\n".join(beat.get("visible_events", []))),
            ("结束状态", "entry", beat.get("end_state", "")),
        ])
        if window is None:
            return
        if fields["确认"]:
            beat["visible_events"] = [line for line in fields["可见事件（每行一条）"].splitlines() if line.strip()]
            beat["end_state"] = fields["结束状态"]
            self._save()

    # ---------- 角色 ----------
    def _render_characters(self, data: dict[str, Any]) -> None:
        app = self.app
        panel = self._panel()
        panel.pack(fill="x", pady=(0, 10))
        head = tk.Frame(panel, bg=app.PANEL)
        head.pack(fill="x", padx=16, pady=(12, 8))
        tk.Label(head, text="角色清单", bg=app.PANEL, fg=app.TEXT,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        tk.Label(head, text="（点“编辑”修正剧情功能 / 视觉锁 / 禁漂移，保存后写入 analysis.json）",
                 bg=app.PANEL, fg=app.MUTED, font=("Microsoft YaHei UI", 8)).pack(side="left", padx=8)

        grid = tk.Frame(panel, bg=app.PANEL)
        grid.pack(fill="x", padx=16, pady=(0, 14))
        grid.grid_columnconfigure((0, 1), weight=1, uniform="chars")
        for index, character in enumerate(data.get("characters", [])):
            card = tk.Frame(grid, bg=app.PANEL_2, highlightbackground=app.BORDER, highlightthickness=1)
            card.grid(row=index // 2, column=index % 2, sticky="nsew", padx=6, pady=6)
            top = tk.Frame(card, bg=app.PANEL_2)
            top.pack(fill="x", padx=12, pady=(10, 4))
            tk.Label(top, text=character.get("name", ""), bg=app.PANEL_2, fg=app.TEXT,
                     font=("Microsoft YaHei UI", 13, "bold")).pack(side="left")
            tk.Label(top, text=f" {character.get('age', '')} ", bg=app.PANEL_2, fg=app.MUTED,
                     font=("Microsoft YaHei UI", 8)).pack(side="left", padx=6)
            self._mini(top, "编辑", lambda c=character: self._edit_character(c)).pack(side="right")
            tk.Label(card, text=character.get("function", ""), bg=app.PANEL_2, fg=app.CYAN,
                     font=("Microsoft YaHei UI", 9, "bold"), anchor="w").pack(fill="x", padx=12)
            invariants = character.get("invariants", {})
            for label, key in (("脸", "face"), ("发", "hair"), ("装", "costume")):
                tk.Label(card, text=f"{label}：{invariants.get(key, '待补充')}", bg=app.PANEL_2, fg=app.TEXT,
                         font=("Microsoft YaHei UI", 8), anchor="w", wraplength=430,
                         justify="left").pack(fill="x", padx=12, pady=1)
            props = "、".join(character.get("props", [])) or "待补充"
            tk.Label(card, text=f"道具：{props}", bg=app.PANEL_2, fg=app.MUTED, font=("Microsoft YaHei UI", 8),
                     anchor="w", wraplength=430, justify="left").pack(fill="x", padx=12, pady=(4, 1))
            tk.Label(card, text=f"说话方式：{character.get('speech_style', '待补充')}", bg=app.PANEL_2, fg=app.MUTED,
                     font=("Microsoft YaHei UI", 8), anchor="w", wraplength=430,
                     justify="left").pack(fill="x", padx=12, pady=1)
            drift = "、".join(character.get("forbidden_drift", [])) or "待补充"
            tk.Label(card, text=f"禁漂移：{drift}", bg=app.PANEL_2, fg=app.RED, font=("Microsoft YaHei UI", 8),
                     anchor="w", wraplength=430, justify="left").pack(fill="x", padx=12, pady=(1, 10))

    def _edit_character(self, character: dict[str, Any]) -> None:
        invariants = character.get("invariants", {})
        window, fields = self._form(f"编辑角色 · {character.get('name', '')}", [
            ("剧情功能", "entry", character.get("function", "")),
            ("年龄气质", "entry", character.get("age", "")),
            ("脸部不变量", "entry", invariants.get("face", "")),
            ("发型不变量", "entry", invariants.get("hair", "")),
            ("服装不变量", "entry", invariants.get("costume", "")),
            ("标志道具（顿号分隔）", "entry", "、".join(character.get("props", []))),
            ("说话方式", "entry", character.get("speech_style", "")),
            ("绝不出现的漂移（顿号分隔）", "entry", "、".join(character.get("forbidden_drift", []))),
        ])
        if window is None or not fields["确认"]:
            return
        character["function"] = fields["剧情功能"]
        character["age"] = fields["年龄气质"]
        character["invariants"] = {
            "face": fields["脸部不变量"],
            "hair": fields["发型不变量"],
            "costume": fields["服装不变量"],
        }
        character["props"] = [item.strip() for item in fields["标志道具（顿号分隔）"].replace(",", "、").split("、") if item.strip()]
        character["speech_style"] = fields["说话方式"]
        character["forbidden_drift"] = [item.strip() for item in fields["绝不出现的漂移（顿号分隔）"].replace(",", "、").split("、") if item.strip()]
        self._save()

    # ---------- 关系图 ----------
    def _render_relationships(self, data: dict[str, Any]) -> None:
        app = self.app
        panel = self._panel()
        panel.pack(fill="x", pady=(0, 10))
        tk.Label(panel, text="人物关系（连线标注关系与 初始 → 中段 → 最终）", bg=app.PANEL, fg=app.TEXT,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", padx=16, pady=(12, 4))
        canvas = tk.Canvas(panel, height=300, bg=app.PANEL, highlightthickness=0)
        canvas.pack(fill="x", padx=16, pady=(0, 8))
        canvas.update_idletasks()
        width = max(600, canvas.winfo_width())
        height = 280
        nodes = data.get("relationships", {}).get("nodes", [])
        edges = data.get("relationships", {}).get("edges", [])
        if not nodes:
            canvas.create_text(width / 2, 120, text="未识别到人物关系", fill=app.MUTED,
                               font=("Microsoft YaHei UI", 10))
            return
        import math

        center_x, center_y, radius = width / 2, height / 2, min(width, height) / 2 - 60
        positions = {}
        for index, node in enumerate(nodes):
            angle = -math.pi / 2 + 2 * math.pi * index / len(nodes)
            positions[node["id"]] = (center_x + radius * math.cos(angle), center_y + radius * math.sin(angle))
        for edge in edges:
            start = positions.get(edge.get("from"))
            end = positions.get(edge.get("to"))
            if not start or not end:
                continue
            canvas.create_line(*start, *end, fill=app.BORDER, width=2)
            mx, my = (start[0] + end[0]) / 2, (start[1] + end[1]) / 2
            canvas.create_text(mx, my - 10, text=edge.get("label", ""), fill=app.CYAN,
                               font=("Microsoft YaHei UI", 9, "bold"))
            detail = f"{edge.get('initial', '?')} → {edge.get('mid', '?')} → {edge.get('final', '?')}"
            canvas.create_text(mx, my + 8, text=detail, fill=app.MUTED, font=("Segoe UI", 8))
        for node in nodes:
            x, y = positions[node["id"]]
            canvas.create_oval(x - 34, y - 24, x + 34, y + 24, fill=app.PANEL_2, outline=app.CYAN, width=2)
            canvas.create_text(x, y - 4, text=node["label"], fill=app.TEXT, font=("Microsoft YaHei UI", 10, "bold"))
            canvas.create_text(x, y + 12, text=node.get("function", "")[:8], fill=app.MUTED, font=("Segoe UI", 7))

        legend = tk.Frame(panel, bg=app.PANEL)
        legend.pack(fill="x", padx=16, pady=(0, 12))
        for edge in edges:
            row = tk.Frame(legend, bg=app.PANEL)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f"{edge.get('from')} ↔ {edge.get('to')}　{edge.get('label')}", bg=app.PANEL,
                     fg=app.TEXT, anchor="w", font=("Microsoft YaHei UI", 9)).pack(side="left")
            tk.Label(row, text=f"{edge.get('initial')} → {edge.get('mid')} → {edge.get('final')}", bg=app.PANEL,
                     fg=app.MUTED, anchor="w", font=("Microsoft YaHei UI", 8)).pack(side="left", padx=10)
            self._mini(row, "编辑", lambda e=edge: self._edit_edge(e)).pack(side="right", pady=2)

    def _edit_edge(self, edge: dict[str, Any]) -> None:
        window, fields = self._form(f"编辑关系 · {edge.get('from')} ↔ {edge.get('to')}", [
            ("关系名称", "entry", edge.get("label", "")),
            ("初始", "entry", edge.get("initial", "")),
            ("中段", "entry", edge.get("mid", "")),
            ("最终", "entry", edge.get("final", "")),
        ])
        if window is None or not fields["确认"]:
            return
        edge["label"] = fields["关系名称"]
        edge["initial"] = fields["初始"]
        edge["mid"] = fields["中段"]
        edge["final"] = fields["最终"]
        self._save()

    # ---------- 场景与道具 ----------
    def _render_scenes(self, data: dict[str, Any]) -> None:
        app = self.app
        panel = self._panel()
        panel.pack(fill="x", pady=(0, 16))
        tk.Label(panel, text="场景与道具", bg=app.PANEL, fg=app.TEXT,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", padx=16, pady=(12, 8))
        for scene in data.get("scenes", []):
            row = tk.Frame(panel, bg=app.PANEL_2)
            row.pack(fill="x", padx=16, pady=3)
            tk.Label(row, text=scene.get("scene_id", ""), bg=app.PANEL_2, fg=app.CYAN, width=7, anchor="w",
                     font=("Microsoft YaHei UI", 9, "bold")).pack(side="left", padx=(10, 0), pady=8)
            tk.Label(row, text=scene.get("location", "待补充"), bg=app.PANEL_2, fg=app.TEXT, width=18, anchor="w",
                     font=("Microsoft YaHei UI", 9)).pack(side="left")
            tk.Label(row, text=scene.get("time_of_day", "待补充"), bg=app.PANEL_2, fg=app.MUTED, width=8, anchor="w",
                     font=("Microsoft YaHei UI", 9)).pack(side="left")
            tk.Label(row, text=f"{float(scene.get('start', 0)):.0f}–{float(scene.get('end', 0)):.0f}s",
                     bg=app.PANEL_2, fg=app.MUTED, width=10, anchor="w",
                     font=("Segoe UI", 9)).pack(side="left")
            props = "、".join(f"{item.get('name')}（{item.get('owner')}）" for item in scene.get("props", [])) or "待补充"
            tk.Label(row, text=f"道具：{props}", bg=app.PANEL_2, fg=app.TEXT, anchor="w",
                     font=("Microsoft YaHei UI", 8), wraplength=420, justify="left").pack(side="left", padx=8)
            self._mini(row, "编辑", lambda s=scene: self._edit_scene(s)).pack(side="right", padx=6, pady=4)

    def _edit_scene(self, scene: dict[str, Any]) -> None:
        window, fields = self._form(f"编辑场景 · {scene.get('scene_id', '')}", [
            ("地点", "entry", scene.get("location", "")),
            ("时段", "entry", scene.get("time_of_day", "")),
        ])
        if window is None or not fields["确认"]:
            return
        scene["location"] = fields["地点"]
        scene["time_of_day"] = fields["时段"]
        self._save()

    # ---------- 通用表单 ----------
    def _form(self, title: str, items: list[tuple[str, str, str]]) -> tuple[tk.Toplevel | None, dict[str, Any]]:
        app = self.app
        window = tk.Toplevel(self)
        window.title(title)
        window.geometry("620x520")
        window.configure(bg=app.BG)
        window.transient(self.winfo_toplevel())
        window.grab_set()
        body = tk.Frame(window, bg=app.BG)
        body.pack(fill="both", expand=True, padx=22, pady=18)
        body.grid_columnconfigure(1, weight=1)

        widgets: dict[str, Any] = {}
        for row, (label, kind, value) in enumerate(items):
            tk.Label(body, text=label, bg=app.BG, fg=app.MUTED, anchor="w",
                     font=("Microsoft YaHei UI", 9)).grid(row=row * 2, column=0, columnspan=2, sticky="w", pady=(8, 2))
            if kind == "entry":
                entry = tk.Entry(body, bg=app.PANEL_2, fg=app.TEXT, insertbackground=app.TEXT,
                                 relief="flat", font=("Microsoft YaHei UI", 10))
                entry.insert(0, value)
                entry.grid(row=row * 2 + 1, column=0, columnspan=2, sticky="ew", pady=(0, 4), ipady=4)
                widgets[label] = entry
            else:
                text = tk.Text(body, bg=app.PANEL_2, fg=app.TEXT, insertbackground=app.TEXT,
                               relief="flat", height=4, font=("Microsoft YaHei UI", 10))
                text.insert("1.0", value)
                text.grid(row=row * 2 + 1, column=0, columnspan=2, sticky="ew", pady=(0, 4))
                widgets[label] = text

        result: dict[str, Any] = {"确认": False}

        def confirm() -> None:
            result["确认"] = True
            for label, widget in widgets.items():
                result[label] = widget.get("1.0", "end").strip() if isinstance(widget, tk.Text) else widget.get().strip()
            window.destroy()

        buttons = tk.Frame(body, bg=app.BG)
        buttons.grid(row=len(items) * 2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        tk.Button(buttons, text="保存", command=confirm, bg=app.BLUE, fg=app.TEXT, relief="flat",
                  bd=0, padx=16, pady=6, font=("Microsoft YaHei UI", 10, "bold"), cursor="hand2").pack(side="left", padx=6)
        tk.Button(buttons, text="取消", command=window.destroy, bg=app.PANEL_2, fg=app.TEXT, relief="flat",
                  bd=0, padx=16, pady=6, font=("Microsoft YaHei UI", 10), cursor="hand2").pack(side="left")
        window.wait_window()
        return window, result

    def _save(self) -> None:
        project = self.app.project_path
        if not project or not self.data:
            return
        episode = self.app._selected_generation_episode()
        analysis_engine.save_analysis(project, episode, self.data)
        self.status_label.configure(text="已保存修改到 analysis.json", fg=self.app.GREEN)
        self._render(self.data)
