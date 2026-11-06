from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import analysis_engine
import analysis_panel
import delivery_panel
import batch_video_engine
import context_pack
import script_storyboard_engine
import storyboard_panel
import timings
import voice_engine


APP_NAME = "AI短剧自动化工作台"
APP_VERSION = "2.0.0"
DEFAULT_WORKFLOW_ROOT = Path(r"C:\Users\12568\Documents\Codex\2026-10-22\ai\outputs\ai-short-drama-workflow")
DEFAULT_DASHBOARD = Path(r"C:\Users\12568\Desktop\零点失忆-可视化工作流\index.html")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = runtime_dir() / "app-config.json"


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def load_config() -> dict:
    default = {
        "workflow_root": str(DEFAULT_WORKFLOW_ROOT),
        "active_project": "零点失忆",
        "dashboard_path": str(DEFAULT_DASHBOARD),
        "version": APP_VERSION,
    }
    saved = load_json(CONFIG_PATH, {})
    default.update(saved if isinstance(saved, dict) else {})
    return default


def save_config(config: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def open_local(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(path)
    if os.name == "nt":
        os.startfile(str(path))
    else:
        webbrowser.open(path.as_uri())


class StudioApp(tk.Tk):
    BG = "#0b1020"
    SIDEBAR = "#11182b"
    PANEL = "#151e33"
    PANEL_2 = "#1b2741"
    TEXT = "#f4f7ff"
    MUTED = "#91a0bd"
    CYAN = "#33d6c9"
    BLUE = "#5d8cff"
    GREEN = "#49d17d"
    AMBER = "#f4b860"
    RED = "#f26d78"
    BORDER = "#2b3858"

    def __init__(self) -> None:
        super().__init__()
        self.config_data = load_config()
        self.workflow_root = Path(self.config_data["workflow_root"])
        self.powershell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        self.project_path: Path | None = None
        self.project_data: dict = {}
        self.state_data: dict = {}
        self.shots: list[dict] = []
        self.jobs: list[dict] = []
        self.release_rows: list[dict] = []
        self.busy = False
        self.streaming = False
        self.generation_started_at: datetime | None = None
        self.generation_current = ""
        self.generation_total = 0

        self.title(f"{APP_NAME}  {APP_VERSION}")
        # 按实际屏幕算初始尺寸，避免在小屏上一打开就裁切右侧内容。
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        width = max(1024, min(1440, screen_width - 80))
        height = max(640, min(880, screen_height - 80))
        self.geometry(f"{width}x{height}+{max(0, (screen_width - width) // 2)}+{max(0, (screen_height - height) // 3)}")
        self.minsize(1024, 640)
        self.configure(bg=self.BG)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._configure_style()
        self._build_shell()
        self.refresh_projects(select_saved=True)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure("TLabel", background=self.BG, foreground=self.TEXT, font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", background=self.BG, foreground=self.MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Header.TLabel", background=self.BG, foreground=self.TEXT, font=("Microsoft YaHei UI", 22, "bold"))
        style.configure("Section.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("CardValue.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI", 23, "bold"))
        style.configure("CardName.TLabel", background=self.PANEL, foreground=self.MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("TNotebook", background=self.BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=self.SIDEBAR, foreground=self.MUTED, padding=(18, 10), font=("Microsoft YaHei UI", 10))
        style.map("TNotebook.Tab", background=[("selected", self.PANEL_2)], foreground=[("selected", self.TEXT)])
        style.configure("Treeview", background=self.PANEL, fieldbackground=self.PANEL, foreground=self.TEXT, rowheight=30, bordercolor=self.BORDER, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", background=self.PANEL_2, foreground=self.TEXT, relief="flat", font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Treeview", background=[("selected", self.BLUE)])
        style.configure("TCombobox", fieldbackground=self.PANEL_2, background=self.PANEL_2, foreground=self.TEXT, arrowcolor=self.TEXT, padding=6)
        style.map("TCombobox", fieldbackground=[("readonly", self.PANEL_2)], foreground=[("readonly", self.TEXT)])
        style.configure("Horizontal.TProgressbar", troughcolor=self.SIDEBAR, background=self.CYAN, bordercolor=self.SIDEBAR, lightcolor=self.CYAN, darkcolor=self.CYAN)

    def _build_shell(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.sidebar = tk.Frame(self, bg=self.SIDEBAR, width=282)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.pack_propagate(False)
        self.content = tk.Frame(self, bg=self.BG)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(1, weight=1)
        self._build_sidebar()
        self._build_header()
        self._build_tabs()
        self.log(f"PowerShell 运行时：{self.powershell}")

    def _build_sidebar(self) -> None:
        brand = tk.Frame(self.sidebar, bg=self.SIDEBAR)
        brand.pack(fill="x", padx=22, pady=(16, 10))
        tk.Label(brand, text="AI SHORT DRAMA", bg=self.SIDEBAR, fg=self.CYAN, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        tk.Label(brand, text="自动化工作台", bg=self.SIDEBAR, fg=self.TEXT, font=("Microsoft YaHei UI", 19, "bold")).pack(anchor="w", pady=(3, 0))
        tk.Label(brand, text=f"LOCAL STUDIO  ·  v{APP_VERSION}", bg=self.SIDEBAR, fg=self.MUTED, font=("Segoe UI", 8)).pack(anchor="w", pady=(5, 0))

        tk.Label(self.sidebar, text="当前项目", bg=self.SIDEBAR, fg=self.MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=22)
        project_row = tk.Frame(self.sidebar, bg=self.SIDEBAR)
        project_row.pack(fill="x", padx=22, pady=(6, 6))
        self.project_combo = ttk.Combobox(project_row, state="readonly")
        self.project_combo.pack(side="left", fill="x", expand=True)
        self.project_combo.bind("<<ComboboxSelected>>", lambda _e: self.select_project(self.project_combo.get()))
        self._mini_button(project_row, "刷新", self.refresh_current).pack(side="left", padx=(6, 0))
        self.generation_target_label = tk.Label(
            self.sidebar, text="生成目标：未设置", wraplength=235, justify="left",
            bg=self.SIDEBAR, fg=self.CYAN, font=("Microsoft YaHei UI", 8, "bold"),
        )
        self.generation_target_label.pack(anchor="w", padx=22, pady=(0, 4))

        # 底部固定：工作流路径 + 查看/设置类快捷入口（不进滚动区，永远可见）。
        footer = tk.Frame(self.sidebar, bg=self.SIDEBAR)
        footer.pack(side="bottom", fill="x", padx=22, pady=(6, 10))
        self.root_label = tk.Label(footer, text="", wraplength=230, justify="left", bg=self.SIDEBAR, fg=self.MUTED, font=("Microsoft YaHei UI", 8))
        self.root_label.pack(anchor="w")

        quick = tk.Frame(self.sidebar, bg=self.SIDEBAR)
        quick.pack(side="bottom", fill="x", padx=22, pady=(6, 0))
        quick.grid_columnconfigure((0, 1, 2), weight=1, uniform="quick")
        quick_caption = tk.Frame(quick, bg=self.SIDEBAR)
        quick_caption.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        tk.Label(
            quick_caption, text="③ 查看与设置", bg=self.SIDEBAR, fg=self.MUTED,
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(side="left")
        tk.Frame(quick_caption, height=1, bg=self.BORDER).pack(side="left", fill="x", expand=True, padx=(8, 0))
        quick_actions = [
            ("质检交付", self.goto_qc), ("打开成片", self.open_final_video), ("可视化看板", self.open_dashboard),
            ("新建项目", self.create_project), ("切换目录", self.choose_workflow_root), ("清空日志", self.clear_log),
        ]
        for index, (label, command) in enumerate(quick_actions):
            self._mini_button(quick, label, command).grid(
                row=1 + index // 3, column=index % 3, sticky="ew",
                padx=(0 if index % 3 == 0 else 3, 0 if index % 3 == 2 else 3), pady=1,
            )

        # 操作按钮放进可滚动画布，窗口变矮时不再被挤出可视区。
        self.sidebar_area = tk.Frame(self.sidebar, bg=self.SIDEBAR)
        self.sidebar_area.pack(fill="both", expand=True)
        self.sidebar_canvas = tk.Canvas(self.sidebar_area, bg=self.SIDEBAR, highlightthickness=0, bd=0, width=1)
        self.sidebar_scroll = ttk.Scrollbar(self.sidebar_area, orient="vertical", command=self.sidebar_canvas.yview)
        self.sidebar_canvas.configure(yscrollcommand=self.sidebar_scroll.set)
        self.sidebar_scroll.pack(side="right", fill="y")
        self.sidebar_canvas.pack(side="left", fill="both", expand=True)
        self.sidebar_body = tk.Frame(self.sidebar_canvas, bg=self.SIDEBAR)
        self.sidebar_window = self.sidebar_canvas.create_window((0, 0), window=self.sidebar_body, anchor="nw")
        self.sidebar_body.bind(
            "<Configure>",
            lambda _e: self.sidebar_canvas.configure(scrollregion=self.sidebar_canvas.bbox("all")),
        )
        self.sidebar_canvas.bind(
            "<Configure>",
            lambda event: self.sidebar_canvas.itemconfigure(self.sidebar_window, width=event.width),
        )
        self.sidebar_canvas.bind("<Enter>", lambda _e: self.sidebar_canvas.bind_all("<MouseWheel>", self._scroll_sidebar))
        self.sidebar_canvas.bind("<Leave>", lambda _e: self.sidebar_canvas.unbind_all("<MouseWheel>"))

        sections = [
            ("① 剧本与分镜", [
                ("上传剧本并生成分镜", self.import_script_and_storyboard, self.CYAN),
                ("打开选中集可视化分镜", self.open_selected_storyboard, self.PANEL_2),
                ("运行项目校验", self.run_validation, self.CYAN),
            ]),
            ("② 视频生成", [
                ("视频生成设置", self.open_video_generation_settings, self.BLUE),
                ("检测视频生成环境", self.check_generation_environment, self.PANEL_2),
                ("生成预览（不提交）", self.preview_generation_plan, self.PANEL_2),
                ("自动批量生成 · 付费", self.batch_generate_current_episode, self.RED),
                ("恢复未完成的生成", self.resume_batch_generation, self.AMBER),
                ("合成当前集视频", self.assemble_current_episode, self.GREEN),
            ]),
        ]
        for index, (title, items) in enumerate(sections):
            caption = tk.Frame(self.sidebar_body, bg=self.SIDEBAR)
            caption.pack(fill="x", padx=(22, 14), pady=(0 if index == 0 else 8, 2))
            tk.Label(
                caption, text=title, bg=self.SIDEBAR, fg=self.MUTED,
                font=("Microsoft YaHei UI", 8, "bold"), anchor="w",
            ).pack(anchor="w")
            for label, command, color in items:
                self._button(self.sidebar_body, label, command, color).pack(fill="x", padx=(22, 14), pady=1)

    def _scroll_sidebar(self, event) -> None:
        self.sidebar_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _scroll_pipeline(self, event) -> None:
        self.pipeline_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _button(self, parent, text: str, command, color: str) -> tk.Button:
        return tk.Button(
            parent, text=text, command=command, bg=color, fg=self.TEXT,
            activebackground=self.BLUE, activeforeground=self.TEXT,
            relief="flat", bd=0, cursor="hand2", padx=12, pady=5,
            font=("Microsoft YaHei UI", 9, "bold"), anchor="w",
        )

    def _mini_button(self, parent, text: str, command) -> tk.Button:
        return tk.Button(
            parent, text=text, command=command, bg=self.PANEL_2, fg=self.TEXT,
            activebackground=self.BLUE, activeforeground=self.TEXT,
            relief="flat", bd=0, cursor="hand2", padx=6, pady=5,
            font=("Microsoft YaHei UI", 8), anchor="center",
        )

    def _build_header(self) -> None:
        header = tk.Frame(self.content, bg=self.BG)
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(22, 12))
        header.grid_columnconfigure(0, weight=1)
        self.title_label = ttk.Label(header, text="AI 短剧生产控制中心", style="Header.TLabel")
        self.title_label.grid(row=0, column=0, sticky="w")
        self.subtitle_label = ttk.Label(header, text="正在读取项目…", style="Muted.TLabel")
        self.subtitle_label.grid(row=1, column=0, sticky="w", pady=(5, 0))
        self.status_badge = tk.Label(header, text="未加载", bg=self.PANEL_2, fg=self.MUTED, padx=13, pady=7, font=("Microsoft YaHei UI", 9, "bold"))
        self.status_badge.grid(row=0, column=1, rowspan=2, sticky="e")

        # 全局进度条：任何标签页都能看到当前生成进度。
        self.header_progress = tk.Frame(header, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1)
        self.header_progress.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.header_progress.grid_columnconfigure(1, weight=1)
        self.header_progress_label = tk.Label(
            self.header_progress, text="", bg=self.PANEL, fg=self.TEXT,
            font=("Microsoft YaHei UI", 9, "bold"), padx=12, pady=2, anchor="w",
        )
        self.header_progress_label.grid(row=0, column=0, sticky="w")
        self.header_progress_bar = ttk.Progressbar(self.header_progress, maximum=100)
        self.header_progress_bar.grid(row=0, column=1, sticky="ew", padx=6, pady=4)
        self.header_progress_pct = tk.Label(
            self.header_progress, text="0%", bg=self.PANEL, fg=self.CYAN,
            font=("Segoe UI", 10, "bold"), padx=14,
        )
        self.header_progress_pct.grid(row=0, column=2, sticky="e")
        self.header_progress.grid_remove()

    def _build_tabs(self) -> None:
        self.notebook = ttk.Notebook(self.content)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=28, pady=(0, 24))
        self.overview_tab = tk.Frame(self.notebook, bg=self.BG)
        self.pipeline_tab = tk.Frame(self.notebook, bg=self.BG)
        self.analysis_tab = tk.Frame(self.notebook, bg=self.BG)
        self.storyboard_tab = tk.Frame(self.notebook, bg=self.BG)
        self.execution_tab = tk.Frame(self.notebook, bg=self.BG)
        self.delivery_tab = tk.Frame(self.notebook, bg=self.BG)
        self.tasks_tab = tk.Frame(self.notebook, bg=self.BG)
        self.console_tab = tk.Frame(self.notebook, bg=self.BG)
        self.notebook.add(self.overview_tab, text="项目总览")
        self.notebook.add(self.pipeline_tab, text="生产流程")
        self.notebook.add(self.analysis_tab, text="剧本分析")
        self.notebook.add(self.storyboard_tab, text="分镜墙")
        self.notebook.add(self.execution_tab, text="生成执行")
        self.notebook.add(self.delivery_tab, text="质检交付")
        self.notebook.add(self.tasks_tab, text="任务台账")
        self.notebook.add(self.console_tab, text="运行日志")
        self._build_overview()
        self._build_pipeline()
        self.analysis_panel = analysis_panel.AnalysisPanel(self.analysis_tab, self)
        self.analysis_panel.pack(fill="both", expand=True)
        self.storyboard_panel = storyboard_panel.StoryboardPanel(self.storyboard_tab, self)
        self.storyboard_panel.pack(fill="both", expand=True)
        self._build_execution()
        self._build_delivery()
        self._build_tasks()
        self._build_console()

    def _panel(self, parent) -> tk.Frame:
        return tk.Frame(parent, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1)

    def _build_overview(self) -> None:
        self.overview_tab.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="cards")
        self.overview_tab.grid_rowconfigure(2, weight=1)
        self.card_vars = []
        for index, name in enumerate(("当前阶段", "生成任务", "已成功", "交付版本")):
            card = self._panel(self.overview_tab)
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 6, 0 if index == 3 else 6), pady=(8, 14))
            value = ttk.Label(card, text="—", style="CardValue.TLabel", wraplength=200)
            value.pack(anchor="w", padx=18, pady=(16, 2))
            ttk.Label(card, text=name, style="CardName.TLabel").pack(anchor="w", padx=18, pady=(0, 15))
            self.card_vars.append(value)

        next_panel = self._panel(self.overview_tab)
        next_panel.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(0, 14))
        ttk.Label(next_panel, text="下一步", style="Section.TLabel").pack(anchor="w", padx=20, pady=(17, 7))
        self.next_action = tk.Label(next_panel, text="", wraplength=620, justify="left", bg=self.PANEL, fg=self.TEXT, font=("Microsoft YaHei UI", 12))
        self.next_action.pack(anchor="w", padx=20, pady=(0, 12))
        row = tk.Frame(next_panel, bg=self.PANEL)
        row.pack(fill="x", padx=20, pady=(0, 18))
        self._button(row, "复制下一步", self.copy_next_action, self.BLUE).pack(side="left")
        self._button(row, "生成低 Token 上下文", self.generate_context, self.PANEL_2).pack(side="left", padx=10)
        self.overview_play_button = self._button(row, "播放成片", self.open_final_video, self.GREEN)
        self.overview_play_button.pack(side="left", padx=(0, 10))
        self._button(row, "质检交付", self.goto_qc, self.BLUE).pack(side="left")
        self._button(row, "打开所在文件夹", self.open_final_folder, self.PANEL_2).pack(side="left")

        details = self._panel(self.overview_tab)
        details.grid(row=2, column=0, columnspan=4, sticky="nsew")
        ttk.Label(details, text="项目参数", style="Section.TLabel").pack(anchor="w", padx=20, pady=(17, 8))
        self.meta_text = tk.Text(details, bg=self.PANEL, fg=self.TEXT, insertbackground=self.TEXT, relief="flat", font=("Cascadia Mono", 10), height=11, width=52, padx=16, pady=12)
        self.meta_text.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.meta_text.configure(state="disabled")

    def _build_pipeline(self) -> None:
        self.pipeline_tab.grid_columnconfigure(0, weight=1)
        self.pipeline_tab.grid_rowconfigure(1, weight=1)
        top = tk.Frame(self.pipeline_tab, bg=self.BG)
        top.grid(row=0, column=0, sticky="ew", pady=(8, 12))
        ttk.Label(top, text="标准生产流程", style="Header.TLabel").pack(side="left")
        self.progress_var = tk.DoubleVar(value=0)
        self.progress = ttk.Progressbar(top, variable=self.progress_var, maximum=100, length=260)
        self.progress.pack(side="right", padx=(10, 0))
        self.progress_label = ttk.Label(top, text="0%", style="Muted.TLabel")
        self.progress_label.pack(side="right")
        # 十个阶段在矮窗口下会超出可视区，放进可滚动画布保证都能看到。
        holder = tk.Frame(self.pipeline_tab, bg=self.BG)
        holder.grid(row=1, column=0, sticky="nsew")
        self.pipeline_canvas = tk.Canvas(holder, bg=self.BG, highlightthickness=0, bd=0, width=1)
        pipeline_scroll = ttk.Scrollbar(holder, orient="vertical", command=self.pipeline_canvas.yview)
        self.pipeline_canvas.configure(yscrollcommand=pipeline_scroll.set)
        pipeline_scroll.pack(side="right", fill="y")
        self.pipeline_canvas.pack(side="left", fill="both", expand=True)
        self.pipeline_body = tk.Frame(self.pipeline_canvas, bg=self.BG)
        self.pipeline_window = self.pipeline_canvas.create_window((0, 0), window=self.pipeline_body, anchor="nw")
        self.pipeline_body.bind(
            "<Configure>",
            lambda _e: self.pipeline_canvas.configure(scrollregion=self.pipeline_canvas.bbox("all")),
        )
        self.pipeline_canvas.bind(
            "<Configure>",
            lambda event: self.pipeline_canvas.itemconfigure(self.pipeline_window, width=event.width),
        )
        self.pipeline_canvas.bind("<Enter>", lambda _e: self.pipeline_canvas.bind_all("<MouseWheel>", self._scroll_pipeline))
        self.pipeline_canvas.bind("<Leave>", lambda _e: self.pipeline_canvas.unbind_all("<MouseWheel>"))

    def _build_execution(self) -> None:
        self.execution_tab.grid_columnconfigure(0, weight=1)
        self.execution_tab.grid_rowconfigure(4, weight=1)

        chips = self._panel(self.execution_tab)
        chips.grid(row=0, column=0, sticky="ew", pady=(8, 10))
        chip_row = tk.Frame(chips, bg=self.PANEL)
        chip_row.pack(fill="x", padx=14, pady=10)
        self.exec_chips: dict[str, tk.Label] = {}
        for name in ("集数", "镜头", "时长", "画质", "风格", "声音", "配音"):
            label = tk.Label(
                chip_row, text=f"{name} —", bg=self.PANEL_2, fg=self.TEXT,
                padx=12, pady=6, font=("Microsoft YaHei UI", 9),
            )
            label.pack(side="left", padx=(0, 8))
            self.exec_chips[name] = label

        progress_panel = self._panel(self.execution_tab)
        progress_panel.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.exec_percent = tk.Label(
            progress_panel, text="0%", bg=self.PANEL, fg=self.CYAN,
            font=("Segoe UI", 30, "bold"),
        )
        self.exec_percent.pack(side="left", padx=(20, 14), pady=10)
        detail = tk.Frame(progress_panel, bg=self.PANEL)
        detail.pack(side="left", fill="both", expand=True, padx=(0, 20), pady=12)
        self.exec_status = tk.Label(
            detail, text="尚未开始", bg=self.PANEL, fg=self.TEXT,
            font=("Microsoft YaHei UI", 11, "bold"), anchor="w",
        )
        self.exec_status.pack(fill="x")
        self.exec_bar = ttk.Progressbar(detail, maximum=100)
        self.exec_bar.pack(fill="x", pady=8)
        self.exec_meta = tk.Label(
            detail, text="", bg=self.PANEL, fg=self.MUTED,
            font=("Microsoft YaHei UI", 9), anchor="w", justify="left",
        )
        self.exec_meta.pack(fill="x")

        actions = tk.Frame(self.execution_tab, bg=self.BG)
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self._button(actions, "自动批量生成 · 付费", self.batch_generate_current_episode, self.RED).pack(side="left")
        self._button(actions, "生成预览", self.preview_generation_plan, self.PANEL_2).pack(side="left", padx=8)
        self._button(actions, "恢复未完成", self.resume_batch_generation, self.AMBER).pack(side="left")
        self._button(actions, "生成本集配音", self.generate_voice, self.BLUE).pack(side="left", padx=8)
        self._button(actions, "合成当前集", self.assemble_current_episode, self.GREEN).pack(side="left")
        self._button(actions, "刷新进度", self.refresh_execution, self.PANEL_2).pack(side="right")

        # 成片条：合成完成后直接在这里播放，不用再去翻 exports 目录。
        final_bar = self._panel(self.execution_tab)
        final_bar.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        final_inner = tk.Frame(final_bar, bg=self.PANEL)
        final_inner.pack(fill="x", padx=14, pady=10)
        self.final_label = tk.Label(
            final_inner, text="成片：检查中…", bg=self.PANEL, fg=self.MUTED,
            font=("Microsoft YaHei UI", 9), anchor="w", justify="left", wraplength=760,
        )
        self.final_label.pack(side="left", fill="x", expand=True)
        self.final_folder_button = self._mini_button(final_inner, "打开所在文件夹", self.open_final_folder)
        self.final_folder_button.pack(side="right", padx=(6, 0))
        self._button(final_inner, "去质检交付", self.goto_qc, self.BLUE).pack(side="right", padx=(8, 0))
        self.final_play_button = self._button(final_inner, "播放成片", self.open_final_video, self.GREEN)
        self.final_play_button.configure(padx=10, pady=4)
        self.final_play_button.pack(side="right", padx=(8, 0))

        columns = ("shot", "status", "duration", "task", "updated")
        self.exec_tree = ttk.Treeview(self.execution_tab, columns=columns, show="headings")
        labels = {"shot": "镜头", "status": "状态", "duration": "时长", "task": "task_id", "updated": "更新时间"}
        widths = {"shot": 130, "status": 110, "duration": 70, "task": 250, "updated": 160}
        for col in columns:
            self.exec_tree.heading(col, text=labels[col])
            self.exec_tree.column(col, width=widths[col], minwidth=60, anchor="w")
        self.exec_tree.tag_configure("done", foreground=self.GREEN)
        self.exec_tree.tag_configure("running", foreground=self.AMBER)
        self.exec_tree.tag_configure("failed", foreground=self.RED)
        self.exec_tree.grid(row=4, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(self.execution_tab, orient="vertical", command=self.exec_tree.yview)
        scrollbar.grid(row=4, column=1, sticky="ns")
        self.exec_tree.configure(yscrollcommand=scrollbar.set)

    def _build_tasks(self) -> None:
        self.tasks_tab.grid_columnconfigure(0, weight=1)
        self.tasks_tab.grid_rowconfigure(1, weight=1)
        top = tk.Frame(self.tasks_tab, bg=self.BG)
        top.grid(row=0, column=0, sticky="ew", pady=(8, 12))
        ttk.Label(top, text="生成任务台账", style="Header.TLabel").pack(side="left")
        self.task_filter = ttk.Combobox(top, state="readonly", width=16, values=["全部", "成功", "进行中", "失败"])
        self.task_filter.set("全部")
        self.task_filter.pack(side="right")
        self.task_filter.bind("<<ComboboxSelected>>", lambda _e: self.render_tasks())
        columns = ("job_id", "shot", "stage", "model", "status", "frame_chain", "scene_refs", "review")
        self.task_tree = ttk.Treeview(self.tasks_tab, columns=columns, show="headings")
        labels = {
            "job_id": "任务 ID", "shot": "镜头", "stage": "阶段", "model": "模型", "status": "状态",
            "frame_chain": "帧链（上一镜末帧）", "scene_refs": "场景批准图", "review": "审核",
        }
        widths = {
            "job_id": 150, "shot": 90, "stage": 62, "model": 110, "status": 70,
            "frame_chain": 150, "scene_refs": 150, "review": 78,
        }
        for col in columns:
            self.task_tree.heading(col, text=labels[col])
            self.task_tree.column(col, width=widths[col], minwidth=80, anchor="w")
        self.task_tree.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(self.tasks_tab, orient="vertical", command=self.task_tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.task_tree.configure(yscrollcommand=scrollbar.set)

    def _build_delivery(self) -> None:
        self.delivery_tab.grid_columnconfigure(0, weight=1)
        self.delivery_tab.grid_rowconfigure(0, weight=1)
        self.qc_panel = delivery_panel.QCPanel(self.delivery_tab, self)
        self.qc_panel.grid(row=0, column=0, sticky="nsew")

    def _build_console(self) -> None:
        self.console_tab.grid_columnconfigure(0, weight=1)
        self.console_tab.grid_rowconfigure(1, weight=1)
        top = tk.Frame(self.console_tab, bg=self.BG)
        top.grid(row=0, column=0, sticky="ew", pady=(8, 12))
        ttk.Label(top, text="运行日志", style="Header.TLabel").pack(side="left")
        self._button(top, "清空日志", self.clear_log, self.PANEL_2).pack(side="right")
        self.log_text = tk.Text(self.console_tab, bg="#080d19", fg="#c8d3ea", insertbackground=self.TEXT, relief="flat", font=("Cascadia Mono", 9), width=44, height=12, padx=14, pady=14)
        self.log_text.grid(row=1, column=0, sticky="nsew")

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def project_names(self) -> list[str]:
        folder = self.workflow_root / "projects"
        if not folder.exists():
            return []
        return sorted([p.name for p in folder.iterdir() if p.is_dir() and (p / "workflow-state.json").exists()])

    def refresh_projects(self, select_saved: bool = False) -> None:
        names = self.project_names()
        self.project_combo["values"] = names
        self.root_label.configure(text=f"工作流：{self.workflow_root.name}")
        target = self.config_data.get("active_project") if select_saved else self.project_combo.get()
        if target not in names and names:
            target = names[0]
        if target in names:
            self.project_combo.set(target)
            self.select_project(target)
        else:
            self.project_combo.set("")
            self.status_badge.configure(text="没有项目", fg=self.AMBER)
            self.log("未发现可用项目，请新建项目或选择工作流目录。")

    def select_project(self, name: str) -> None:
        path = self.workflow_root / "projects" / name
        if not path.exists():
            return
        self.project_path = path
        self.config_data["active_project"] = name
        self.generation_started_at = None
        self.generation_current = ""
        save_config(self.config_data)
        self.load_project()

    def load_project(self) -> None:
        if not self.project_path:
            return
        config = load_json(self.project_path / "00_brief" / "project-config.json", {})
        state = load_json(self.project_path / "workflow-state.json", {})
        self.project_data = config if isinstance(config, dict) else {}
        self.state_data = state if isinstance(state, dict) else {}
        selected_episode = self._configured_episode()
        self.loaded_episode = selected_episode
        self.shots = self._load_episode_shots(selected_episode)
        self.jobs = self._read_csv(self.project_path / "05_generation" / "generation-jobs.csv")
        self.release_rows = self._read_csv(self.project_path / "08_release" / "release-manifest.csv")
        self.render_all()
        if hasattr(self, "analysis_panel"):
            self.analysis_panel.refresh()
        if hasattr(self, "storyboard_panel"):
            self.storyboard_panel.refresh()
        self.log(f"已加载项目：{self.project_path.name}")

    def _load_episode_shots(self, episode: str) -> list[dict]:
        """优先读标准镜头表；老项目只有 09_automation/episodes/<集>.json 时也能显示。"""
        rows = self._read_csv(self.project_path / "03_storyboard" / f"{episode}-shotlist.csv")
        if rows:
            return rows
        legacy = load_json(self.project_path / "09_automation" / "episodes" / f"{episode}.json", {})
        shots = legacy.get("shots") if isinstance(legacy, dict) else None
        if not shots:
            return []
        converted: list[dict] = []
        core = str(legacy.get("core") or "")
        for index, shot in enumerate(shots, start=1):
            converted.append({
                "episode": episode,
                "shot_id": str(shot.get("shot_id", f"{episode}-S{index:03d}")),
                "order": str(shot.get("order", index)),
                "duration_s": str(shot.get("duration") or shot.get("duration_s") or 5),
                "scene_id": str(shot.get("scene_id", "")),
                "characters": str(shot.get("speaker_label") or shot.get("characters") or ""),
                "speaker": str(shot.get("speaker_label") or ""),
                "dialogue_text": str(shot.get("text") or ""),
                "dialogue_or_vo": str(shot.get("text") or ""),
                "action": core or "旧版剧集定义，无逐镜动作",
                "shot_size": "", "camera": "", "transition": "连续承接",
                "emotion": "", "emotion_arc": "", "emotion_line": "",
                "risk_level": "", "status": "DRAFT", "_legacy": "1",
            })
        return converted

    @staticmethod
    def _read_csv(path: Path) -> list[dict]:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle))
        except (FileNotFoundError, OSError, UnicodeError):
            return []

    def render_all(self) -> None:
        title = self.project_data.get("title") or (self.project_path.name if self.project_path else "未命名项目")
        stage = self.state_data.get("current_stage", "UNKNOWN")
        episode = self.state_data.get("current_episode", "—")
        self.title_label.configure(text=title)
        self.subtitle_label.configure(text=f"{self.project_data.get('genre', '未设置题材')}  ·  {self.project_data.get('target_platform', '未设置平台')}  ·  当前 {episode}")
        self.status_badge.configure(text=self._short_stage(stage), fg=self.CYAN)

        success = sum(1 for row in self.jobs if row.get("status", "").upper() == "SUCCEEDED")
        releases = sum(1 for row in self.release_rows if row.get("video_path"))
        self.card_vars[0].configure(text=self._short_stage(stage))
        self.card_vars[1].configure(text=str(len(self.jobs)))
        self.card_vars[2].configure(text=str(success))
        self.card_vars[3].configure(text=str(releases))
        self.next_action.configure(text=self.state_data.get("next_action", "尚未填写下一步"))
        final_path, _final_text = self._final_video_summary()
        self.overview_play_button.configure(
            text=f"播放 {self._selected_generation_episode()} 成片" if final_path else "播放成片",
            state="normal" if final_path else "disabled",
        )
        self._render_metadata()
        self.render_pipeline()
        self.render_tasks()
        self.render_delivery()
        self._render_generation_target()
        self.refresh_execution()

    @staticmethod
    def _short_stage(stage: str) -> str:
        mapping = {
            "IDEA": "立项", "LOCKED": "锁定", "SCRIPTED": "剧本", "STORYBOARDED": "分镜",
            "PILOT_REVIEW": "首镜审看", "PILOT_PASS": "首镜通过", "GENERATED": "生成", "EDITED": "剪辑", "QC_PASS": "质检",
            "FINAL_REVIEW": "终审", "RELEASED": "发布",
            "LOCAL_MOTION_COMIC_SEASON_VALIDATED": "动态漫验收",
            "LOCAL_MOTION_COMIC": "动态漫",
        }
        if stage in mapping:
            return mapping[stage]
        cleaned = stage.replace("LOCAL_", "").replace("_", " ").strip()
        return cleaned if len(cleaned) <= 8 else cleaned[:8] + "…"

    def _render_metadata(self) -> None:
        approvals = self.state_data.get("approvals", {})
        rows = [
            ("项目目录", str(self.project_path or "")),
            ("项目 ID", self.project_data.get("project_id", "—")),
            ("题材", self.project_data.get("genre", "—")),
            ("目标平台", self.project_data.get("target_platform", "—")),
            ("计划集数", self.project_data.get("episode_count", "—")),
            ("画幅", self.project_data.get("aspect_ratio", "—")),
            ("当前镜头", self.state_data.get("current_shot", "—")),
            ("最后任务", self.state_data.get("last_completed_job_id", "—")),
            ("审批", " / ".join(f"{key}:{'通过' if value else '待审'}" for key, value in approvals.items())),
            ("耗时", timings.summary(self.project_path, self._selected_generation_episode()) if self.project_path else "—"),
        ]
        self.meta_text.configure(state="normal")
        self.meta_text.delete("1.0", "end")
        for key, value in rows:
            self.meta_text.insert("end", f"{key:<12} {value}\n")
        self.meta_text.configure(state="disabled")

    def render_pipeline(self) -> None:
        for child in self.pipeline_body.winfo_children():
            child.destroy()
        steps = [
            ("01", "立项简报", "平台、题材、预算与禁区"),
            ("02", "系列圣经", "角色、关系、世界观与声音锁"),
            ("03", "分集剧本", "钩子、冲突、反转与悬念"),
            ("04", "镜头分解", "镜头表与可移植 Prompt Spec"),
            ("05", "代表镜头审看", "定妆、场景、表演可信度由人工审核"),
            ("06", "代表镜头通过", "身份、动作、构图和音画路线锁定"),
            ("07", "批量生成", "任务 ID、状态与本地素材"),
            ("08", "剪辑声音", "字幕、对白、口型、音效与混音"),
            ("09", "整集质检", "六项质量闸门与平台规格"),
            ("10", "发布复盘", "封面、文案、版本与数据回填"),
        ]
        done = self._step_completion()
        for idx, title, desc in steps:
            complete = done[int(idx) - 1]
            panel = self._panel(self.pipeline_body)
            panel.pack(fill="x", pady=5)
            marker = tk.Label(panel, text="完成" if complete else "待处理", bg=self.GREEN if complete else self.PANEL_2, fg=self.TEXT, width=7, pady=5, font=("Microsoft YaHei UI", 9, "bold"))
            marker.pack(side="left", padx=14, pady=12)
            tk.Label(panel, text=idx, bg=self.PANEL, fg=self.CYAN if complete else self.MUTED, font=("Segoe UI", 12, "bold"), width=4).pack(side="left")
            text = tk.Frame(panel, bg=self.PANEL)
            text.pack(side="left", fill="x", expand=True, pady=10)
            tk.Label(text, text=title, bg=self.PANEL, fg=self.TEXT, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
            tk.Label(text, text=desc, bg=self.PANEL, fg=self.MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(2, 0))
        percent = round(sum(done) / len(done) * 100)
        self.progress_var.set(percent)
        self.progress_label.configure(text=f"{percent}%")

    def _step_completion(self) -> list[bool]:
        if not self.project_path:
            return [False] * 10
        p = self.project_path
        has_script = any((p / "02_script").glob("*.md"))
        has_storyboard = any((p / "03_storyboard").glob("*.csv"))
        has_assets = any(path.is_file() for path in (p / "04_assets").rglob("*") if path.is_file())
        has_success = any(row.get("status", "").upper() == "SUCCEEDED" for row in self.jobs)
        has_export = any((p / "06_edit" / "exports").glob("*.mp4"))
        has_qc = any((p / "07_qc").glob("*.md")) or any((p / "07_qc").glob("*.json"))
        has_release = any(row.get("video_path") for row in self.release_rows)
        return [
            (p / "00_brief" / "project-brief.md").exists(),
            (p / "01_bible" / "series-bible.md").exists(),
            has_script,
            has_storyboard,
            bool(self.state_data.get("approvals", {}).get("pilot_performance")) or has_assets,
            bool(self.state_data.get("approvals", {}).get("pilot_shot")),
            has_success,
            has_export,
            has_qc,
            has_release,
        ]

    def render_tasks(self) -> None:
        for item in self.task_tree.get_children():
            self.task_tree.delete(item)
        selected = self.task_filter.get() or "全部"
        shot_lookup = {row.get("shot_id", ""): row for row in self.shots if row.get("shot_id")}
        rows = list(reversed(self.jobs[-200:]))
        for row in rows:
            status = row.get("status", "")
            if selected == "成功" and status.upper() != "SUCCEEDED":
                continue
            if selected == "进行中" and status.upper() not in {"STARTING", "QUEUED", "PENDING", "PROCESSING", "RUNNING"}:
                continue
            if selected == "失败" and status.upper() not in {"FAILED", "CREATE_FAILED", "DOWNLOAD_FAILED", "CANCELED"}:
                continue
            shot = shot_lookup.get(row.get("shot_id", ""), {})
            frame_chain = shot.get("prev_last_frame", "") or shot.get("next_first_frame", "")
            self.task_tree.insert("", "end", values=(
                row.get("job_id", ""), row.get("shot_id", ""), row.get("stage", ""),
                row.get("model_id", ""), status, frame_chain, shot.get("scene_refs", ""), row.get("review_result", ""),
            ))

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return ""

    def _quality_qc_path(self, episode: str) -> Path | None:
        if not self.project_path:
            return None
        target = self.project_path / "07_qc" / f"{episode}-qc.md"
        if target.exists():
            return target
        candidates = sorted((self.project_path / "07_qc").glob("*-qc.md"))
        return candidates[0] if candidates else None

    def _quality_statuses(self) -> list[tuple[str, str, str]]:
        columns = set(self.shots[0].keys()) if self.shots else set()
        episode = self._selected_generation_episode()

        def filled(fields: set[str]) -> bool:
            return bool(self.shots) and fields.issubset(columns) and all(
                all(str(row.get(field, "")).strip() for field in fields) for row in self.shots
            )

        dialogue_rows = [row for row in self.shots if row.get("dialogue_text", "").strip() not in {"", "无台词"}]
        voice_columns = {"dialogue_text", "speaker", "emotion_line", "line_tc"}.issubset(columns)
        voice_ready = voice_columns and all(row.get("speaker", "").strip() and row.get("emotion_line", "").strip() for row in dialogue_rows)

        frame_columns = {"prev_last_frame", "next_first_frame"}.issubset(columns)
        frame_ready = frame_columns and all(
            (index == 0 or row.get("prev_last_frame", "").strip()) and
            (index == len(self.shots) - 1 or row.get("next_first_frame", "").strip())
            for index, row in enumerate(self.shots)
        )

        scene_ready = filled({"scene_refs"})
        performance_ready = filled({"emotion", "emotion_arc", "emotion_line"})

        qc_path = self._quality_qc_path(episode)
        qc_text = self._read_text(qc_path) if qc_path else ""
        prompt_text = self._read_text(self.project_path / "05_generation" / f"{episode}-prompt-spec.md") if self.project_path else ""
        continuity_ready = "穿帮" in qc_text and "负向" in prompt_text

        script_ready = filled({"script_ref"})
        lock = self.state_data.get("script_lock")
        locked = bool(isinstance(lock, dict) and lock.get("locked"))
        if script_ready and locked:
            script_status = ("已锁定", self.GREEN)
        elif script_ready:
            script_status = ("待锁剧本", self.AMBER)
        else:
            script_status = ("待补充", self.AMBER)

        checks: list[tuple[str, str, str]] = [
            ("人声同步", "已登记" if voice_ready else "待补充", self.GREEN if voice_ready else self.AMBER),
            ("帧链衔接", "已登记" if frame_ready else "待补充", self.GREEN if frame_ready else self.AMBER),
            ("场景一致", "已登记" if scene_ready else "待补充", self.GREEN if scene_ready else self.AMBER),
            ("表演与情绪", "已登记" if performance_ready else "待补充", self.GREEN if performance_ready else self.AMBER),
            ("穿帮排查", "已登记" if continuity_ready else "待补充", self.GREEN if continuity_ready else self.AMBER),
            ("剧本一致", script_status[0], script_status[1]),
        ]
        return checks

    def render_delivery(self) -> None:
        if hasattr(self, "qc_panel"):
            self.qc_panel.refresh()

    def refresh_current(self) -> None:
        self.load_project()

    def copy_next_action(self) -> None:
        text = self.state_data.get("next_action", "")
        self.clipboard_clear()
        self.clipboard_append(text)
        self.log("下一步已复制到剪贴板。")

    def open_project_folder(self) -> None:
        self._open_path(self.project_path)

    def open_dashboard(self) -> None:
        path = Path(self.config_data.get("dashboard_path", ""))
        if not path.exists():
            chosen = filedialog.askopenfilename(title="选择可视化看板 index.html", filetypes=[("HTML", "*.html")])
            if not chosen:
                return
            path = Path(chosen)
            self.config_data["dashboard_path"] = str(path)
            save_config(self.config_data)
        self._open_path(path)

    def _final_video_candidates(self) -> list[Path]:
        """成片候选：当前集自动合片优先，其次发布清单，最后导出目录里最新的 mp4。"""
        if not self.project_path:
            return []
        episode = self._selected_generation_episode()
        exports_dir = self.project_path / "06_edit" / "exports"
        candidates: list[Path] = [
            exports_dir / f"{episode}-auto-final-subtitled.mp4",
            exports_dir / f"{episode}-auto-final-synced.mp4",
            exports_dir / f"{episode}-auto-final-with-audio.mp4",
            exports_dir / f"{episode}-auto-final.mp4",
        ]
        for row in reversed(self.release_rows):
            relative = row.get("video_path", "")
            if relative:
                candidates.append(self.project_path / relative)
        exports = self.project_path / "06_edit" / "exports"
        if exports.exists():
            candidates.extend(sorted(exports.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True))
        unique: list[Path] = []
        for path in candidates:
            if path.exists() and path not in unique:
                unique.append(path)
        return unique

    def _find_final_video(self) -> Path | None:
        candidates = self._final_video_candidates()
        return candidates[0] if candidates else None

    def _video_duration(self, path: Path) -> float | None:
        """用 ffprobe 读时长；按路径+修改时间缓存，避免每次刷新都起进程。"""
        try:
            stat = path.stat()
        except OSError:
            return None
        cache = getattr(self, "_duration_cache", None)
        if cache is None:
            cache = self._duration_cache = {}
        key = (str(path), stat.st_mtime_ns)
        if key in cache:
            return cache[key]
        ffmpeg = batch_video_engine.find_ffmpeg()
        probe = str(Path(ffmpeg).with_name("ffprobe.exe")) if ffmpeg else ""
        if not probe or not Path(probe).exists():
            probe = shutil.which("ffprobe") or ""
        duration: float | None = None
        if probe:
            try:
                result = subprocess.run(
                    [probe, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                    capture_output=True, text=True, timeout=15, creationflags=CREATE_NO_WINDOW,
                )
                if result.returncode == 0 and result.stdout.strip():
                    duration = float(result.stdout.strip())
            except (OSError, ValueError, subprocess.SubprocessError):
                duration = None
        cache[key] = duration
        return duration

    def _final_video_summary(self) -> tuple[Path | None, str]:
        path = self._find_final_video()
        if not path:
            return None, "还没有合成好的成片：跑完镜头后点“合成当前集”即可生成。"
        try:
            size_mb = path.stat().st_size / 1024 / 1024
        except OSError:
            size_mb = 0.0
        parts = [path.name, f"{size_mb:.1f} MB"]
        if "with-audio" in path.name:
            parts.append("含配音")
        duration = self._video_duration(path)
        if duration:
            parts.insert(1, f"{int(duration // 60)}:{int(duration % 60):02d}")
        try:
            relative = path.relative_to(self.project_path)
            location = str(relative)
        except ValueError:
            location = str(path)
        import re as _re

        owner = _re.match(r"(EP\d{3})", path.name)
        current = self._selected_generation_episode()
        if owner and owner.group(1) != current:
            parts.insert(0, f"（属于 {owner.group(1)}，本集还没有成片）")
        return path, f"{' · '.join(parts)}　（{location}）"

    def open_final_folder(self) -> None:
        path = self._find_final_video()
        if not path:
            messagebox.showwarning(APP_NAME, "当前项目还没有合成好的成片。")
            return
        self._open_path(path.parent)

    def goto_qc(self, episode: str | None = None) -> None:
        """跳到质检交付页；指定集数时先切到那一集（含之前生成过的集）。"""
        if not hasattr(self, "qc_panel"):
            return
        panel = self.qc_panel
        target = (episode or "").strip().upper()
        if target and target in panel._episodes():
            panel.episode = target
            panel.episode_var.set(target)
        self.notebook.select(self.delivery_tab)
        panel.refresh()
        self.log(f"打开质检交付：{panel.episode}")

    def open_final_video(self) -> None:
        path = self._find_final_video()
        if not path:
            messagebox.showwarning(APP_NAME, "当前项目还没有可播放的成片。")
            return
        self._open_path(path)

    def _open_path(self, path: Path | None) -> None:
        if not path:
            return
        try:
            open_local(Path(path))
            self.log(f"已打开：{path}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"无法打开：\n{path}\n\n{exc}")

    def choose_workflow_root(self) -> None:
        chosen = filedialog.askdirectory(title="选择 AI 短剧工作流目录", initialdir=str(self.workflow_root))
        if not chosen:
            return
        root = Path(chosen)
        if not (root / "scripts" / "New-ShortDramaProject.ps1").exists():
            messagebox.showerror(APP_NAME, "所选目录不是有效的 AI 短剧工作流目录。")
            return
        self.workflow_root = root
        self.config_data["workflow_root"] = str(root)
        save_config(self.config_data)
        self.refresh_projects(select_saved=False)

    def create_project(self) -> None:
        name = simpledialog.askstring("新建短剧项目", "请输入项目名称：", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        script = self.workflow_root / "scripts" / "New-ShortDramaProject.ps1"
        if not script.exists():
            messagebox.showerror(APP_NAME, f"缺少新建脚本：\n{script}")
            return
        self._run_background(
            [self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-ProjectName", name, "-OutputRoot", str(self.workflow_root / "projects")],
            f"创建项目：{name}",
            lambda: self._after_project_created(name),
        )

    def _after_project_created(self, name: str) -> None:
        self.refresh_projects(select_saved=False)
        if name in self.project_names():
            self.project_combo.set(name)
            self.select_project(name)
        messagebox.showinfo(APP_NAME, f"项目已创建：{name}\n下一步请填写立项简报。")

    def run_validation(self) -> None:
        if not self.project_path:
            return
        script = self.workflow_root / "scripts" / "Test-ShortDramaProject.ps1"
        self._run_background(
            [self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-ProjectPath", str(self.project_path), "-Episode", self._selected_generation_episode()],
            "运行项目结构与台账校验",
            lambda: self.load_project(),
        )

    def _batch_worker_command(self, command: str) -> list[str]:
        if not self.project_path:
            return []
        episode = self._selected_generation_episode()
        prefix = [sys.executable]
        if getattr(sys, "frozen", False):
            prefix.append("--batch-worker")
        else:
            prefix.extend([str(Path(__file__).resolve()), "--batch-worker"])
        return prefix + ["--project", str(self.project_path), "--episode", episode, command]

    def _video_provider_path(self) -> Path | None:
        return self.project_path / "09_automation" / "video-provider.json" if self.project_path else None

    def _video_provider_config(self) -> dict:
        path = self._video_provider_path()
        return load_json(path, {}) if path else {}

    def _available_episodes(self) -> list[str]:
        if not self.project_path:
            return []
        names = {path.name[:5].upper() for path in (self.project_path / "03_storyboard").glob("EP*-shotlist.csv")}
        names.update(path.stem.upper() for path in (self.project_path / "09_automation" / "episodes").glob("EP*.json"))
        return sorted(name for name in names if len(name) == 5 and name.startswith("EP") and name[2:].isdigit())

    def _configured_episode(self) -> str:
        """配置里选中的集（决定下一次加载哪一集）。"""
        config = self._video_provider_config()
        selected = str(config.get("selected_episode", "")).upper()
        available = self._available_episodes()
        if selected in available:
            return selected
        current = str(self.state_data.get("current_episode", "EP001")).upper()
        return current if current in available else (available[0] if available else current)

    def _selected_generation_episode(self) -> str:
        """界面当前这一屏展示的集；必须与 self.shots 保持一致。"""
        return getattr(self, "loaded_episode", "") or self._configured_episode()

    def _selected_shot_count(self) -> int:
        if not self.project_path:
            return 0
        episode = self._selected_generation_episode()
        shotlist = self.project_path / "03_storyboard" / f"{episode}-shotlist.csv"
        if shotlist.exists():
            return len(self._read_csv(shotlist))
        legacy = load_json(self.project_path / "09_automation" / "episodes" / f"{episode}.json", {})
        return len(legacy.get("shots", [])) if isinstance(legacy, dict) else 0

    def _render_generation_target(self) -> None:
        if not self.project_path:
            self.generation_target_label.configure(text="生成目标：未设置")
            return
        config = self._video_provider_config()
        episode = self._selected_generation_episode()
        plan = batch_video_engine.plan_durations(self.shots, config)
        duration = sum(plan.values()) or config.get("episode_duration_seconds", "自动")
        resolution = config.get("resolution", "720P")
        style = config.get("style_preset", "都市悬疑真人电影")
        shots = self._selected_shot_count()
        self.generation_target_label.configure(
            text=f"生成目标：{episode} · {shots} 镜 · {duration}秒 · {resolution}\n{style}"
        )

    # ---------- 生成进度可视化 ----------

    STATUS_LABELS = {
        "SUCCEEDED": "成功", "COMPLETED": "成功", "RUNNING": "生成中", "PROCESSING": "生成中",
        "GENERATING": "生成中", "PENDING": "排队中", "QUEUED": "排队中", "STARTING": "提交中",
        "UNSUBMITTED": "待提交",
        "FAILED": "失败", "CANCELED": "已取消", "UNKNOWN": "未知",
    }
    ACTIVE_STATUSES = {"RUNNING", "PROCESSING", "GENERATING", "PENDING", "QUEUED", "STARTING"}
    DONE_STATUSES = {"SUCCEEDED", "COMPLETED"}
    FAILED_STATUSES = {"FAILED", "CANCELED"}

    @staticmethod
    def _format_seconds(value: float) -> str:
        value = max(0, int(value))
        minutes, seconds = divmod(value, 60)
        if minutes >= 60:
            hours, minutes = divmod(minutes, 60)
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    def _video_state_dir(self, episode: str) -> Path | None:
        if not self.project_path:
            return None
        return self.project_path / "09_automation" / "video-state" / episode

    def _shot_states(self, episode: str) -> dict[str, dict]:
        """读取逐镜任务状态：新引擎目录优先，其次旧 wan3 管线，最后才是镜头表。"""
        states: dict[str, dict] = {}
        if not self.project_path:
            return states
        legacy_dir = self.project_path / "09_automation" / "aliyun-wan3-state" / episode
        for directory in (self._video_state_dir(episode), legacy_dir):
            if not directory.is_dir():
                continue
            for path in directory.glob("*.json"):
                data = load_json(path, {})
                if isinstance(data, dict) and data.get("shot_id") and str(data["shot_id"]) not in states:
                    states[str(data["shot_id"])] = data
        return states

    def _generation_snapshot(self) -> dict:
        episode = self._selected_generation_episode()
        config = self._video_provider_config()
        states = self._shot_states(episode)
        rows: list[dict] = []
        for shot in self.shots:
            shot_id = str(shot.get("shot_id", ""))
            state = states.get(shot_id, {})
            status = str(state.get("status") or "").upper()
            if not status:
                # 没有任务状态时，只把镜头表里明确成功/失败的当作真实状态；
                # DRAFT / GENERATING 这类历史标记一律视为待提交，避免虚报进行中。
                legacy = str(shot.get("status") or "").upper()
                status = legacy if legacy in self.DONE_STATUSES | self.FAILED_STATUSES else "UNSUBMITTED"
            rows.append({
                "shot_id": shot_id,
                "status": status,
                "duration": str(shot.get("duration_s", "")),
                "task_id": str(state.get("task_id", "")),
                "updated": str(state.get("updated_at", ""))[:19].replace("T", " "),
            })
        total = len(rows)
        done = sum(1 for row in rows if row["status"] in self.DONE_STATUSES)
        failed = sum(1 for row in rows if row["status"] in self.FAILED_STATUSES)
        running = sum(1 for row in rows if row["status"] in self.ACTIVE_STATUSES)
        return {
            "episode": episode, "rows": rows, "total": total, "done": done,
            "running": running, "failed": failed, "pending": total - done - failed - running,
            "config": config,
        }

    def _progress_texts(self, snapshot: dict) -> tuple[str, str]:
        total = snapshot["total"]
        done, running, failed = snapshot["done"], snapshot["running"], snapshot["failed"]
        if self.streaming and total and done == total:
            status = "镜头已全部完成，正在合成成片…"
        elif self.streaming:
            current = self.generation_current or "准备中"
            status = f"正在生成 · 当前 {current}"
        elif total and done == total:
            status = "全部镜头已完成，可合成或做质检"
        elif failed:
            status = f"已安全停止 · {failed} 个镜头失败，可单独重做"
        elif running:
            status = "有任务仍在云端排队或生成（未在本地轮询）"
        elif done:
            status = f"已完成 {done}/{total} 镜，还有 {snapshot['pending']} 镜待提交（可点“恢复未完成”）"
        else:
            status = "尚未开始批量生成"
        parts = [f"完成 {done}/{total}", f"进行中 {running}", f"待提交 {snapshot['pending']}"]
        if failed:
            parts.append(f"失败 {failed}")
        if self.generation_started_at and (self.streaming or done or failed):
            elapsed = (datetime.now() - self.generation_started_at).total_seconds()
            parts.append(f"已用 {self._format_seconds(elapsed)}")
            if self.streaming and done:
                remaining = elapsed / done * max(0, total - done)
                parts.append(f"预计剩余 {self._format_seconds(remaining)}")
        planned = sum(getattr(self, "_planned_durations", {}).values())
        parts.append(f"配置 {snapshot['config'].get('resolution', '—')} / 计划 {planned or '—'} 秒")
        return status, " · ".join(parts)

    def _apply_header_progress(self, snapshot: dict, percent: int) -> None:
        visible = bool(self.streaming or snapshot["done"] or snapshot["running"] or snapshot["failed"])
        if not visible:
            self.header_progress.grid_remove()
            return
        self.header_progress.grid()
        if self.streaming:
            state = "生成中"
        elif snapshot["total"] and snapshot["done"] == snapshot["total"]:
            state = "已完成"
        elif snapshot["failed"]:
            state = "已停止"
        else:
            state = "待继续"
        current = ""
        if self.streaming and self.generation_current:
            current = f" · {self.generation_current}"
        self.header_progress_label.configure(
            text=f"{state} · {snapshot['episode']} · {snapshot['done']}/{snapshot['total']} 镜{current}"
        )
        self.header_progress_bar["value"] = percent
        self.header_progress_pct.configure(text=f"{percent}%")

    def refresh_execution(self) -> None:
        if not hasattr(self, "exec_tree"):
            return
        snapshot = self._generation_snapshot()
        total = snapshot["total"]
        percent = round(snapshot["done"] * 100 / total) if total else 0
        config = snapshot["config"]
        self.exec_chips["集数"].configure(text=f"集数 {snapshot['episode']}")
        self.exec_chips["镜头"].configure(text=f"镜头 {total}")
        plan = batch_video_engine.plan_durations(self.shots, config)
        planned_total = sum(plan.values())
        requested = config.get("episode_duration_seconds")
        try:
            requested_value = int(requested)
        except (TypeError, ValueError):
            requested_value = planned_total
        if requested_value != planned_total:
            self.exec_chips["时长"].configure(
                text=f"时长 {planned_total} 秒（请求 {requested_value}）", fg=self.AMBER)
        else:
            self.exec_chips["时长"].configure(text=f"时长 {planned_total} 秒", fg=self.TEXT)
        self._planned_durations = plan
        self.exec_chips["画质"].configure(text=f"画质 {config.get('resolution', '—')}")
        self.exec_chips["风格"].configure(text=f"风格 {config.get('style_preset', '—')}")
        audio_on = bool(config.get("audio", True))
        self.exec_chips["声音"].configure(
            text=f"声音 {'开' if audio_on else '关 · 无音轨'}",
            fg=self.TEXT if audio_on else self.AMBER,
        )
        route = str(config.get("voice_route") or "native")
        self.exec_chips["配音"].configure(
            text="配音 原生" if route == "native" else "配音 外部 TTS",
            fg=self.CYAN if route == "native" else self.BLUE,
        )
        self.exec_percent.configure(text=f"{percent}%")
        self.exec_bar["value"] = percent
        status_text, meta_text = self._progress_texts(snapshot)
        self.exec_status.configure(text=status_text)
        self.exec_meta.configure(text=meta_text)
        self._apply_header_progress(snapshot, percent)
        final_path, final_text = self._final_video_summary()
        self.final_label.configure(
            text=f"成片：{final_text}",
            fg=self.GREEN if final_path else self.MUTED,
        )
        state = "normal" if final_path else "disabled"
        self.final_play_button.configure(state=state)
        self.final_folder_button.configure(state=state)

        for item in self.exec_tree.get_children():
            self.exec_tree.delete(item)
        for row in snapshot["rows"]:
            status = row["status"]
            tag = ""
            if status in self.DONE_STATUSES:
                tag = "done"
            elif status in self.ACTIVE_STATUSES:
                tag = "running"
            elif status in self.FAILED_STATUSES:
                tag = "failed"
            self.exec_tree.insert(
                "", "end",
                values=(
                    row["shot_id"], self.STATUS_LABELS.get(status, status),
                    f"{getattr(self, '_planned_durations', {}).get(row['shot_id'], row['duration'])} 秒",
                    row["task_id"] or "—", row["updated"] or "—",
                ),
                tags=(tag,) if tag else (),
            )

    def _handle_worker_line(self, line: str) -> None:
        self.log(line)
        progress_match = re.match(r"^\[(\d+)/(\d+)\]\s+(\S+)", line)
        if progress_match:
            self.generation_current = progress_match.group(3)
            self.generation_total = int(progress_match.group(2))
        shot_match = re.match(r"^(EP\d{3}-S\d{3}):\s*(.*)$", line)
        if shot_match and self.streaming:
            self.generation_current = shot_match.group(1)
            self.refresh_execution()

    def _tick_generation(self) -> None:
        if not self.streaming:
            return
        self.refresh_execution()
        self.after(1500, self._tick_generation)

    def open_video_generation_settings(self) -> None:
        if not self.project_path:
            return
        self._ensure_video_provider_config()
        config = self._video_provider_config()
        episodes = self._available_episodes()
        window = tk.Toplevel(self)
        window.title("视频生成设置")
        height = min(720, max(420, self.winfo_screenheight() - 140))
        window.geometry(f"660x{height}")
        window.minsize(560, 380)
        window.configure(bg=self.BG)
        window.transient(self)
        window.grab_set()

        # 底部按钮固定，不随表单滚动
        bottom = tk.Frame(window, bg=self.BG)
        bottom.pack(side="bottom", fill="x", padx=22, pady=(6, 14))

        holder = tk.Frame(window, bg=self.BG)
        holder.pack(fill="both", expand=True)
        canvas = tk.Canvas(holder, bg=self.BG, highlightthickness=0, bd=0, width=1)
        scrollbar = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        form = tk.Frame(canvas, bg=self.BG)
        window_id = canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", self._scroll_canvas_event(canvas)))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        panel = tk.Frame(form, bg=self.BG)
        panel.pack(fill="x", padx=24, pady=(18, 10))
        panel.grid_columnconfigure(1, weight=1)

        episode_var = tk.StringVar(value=self._selected_generation_episode())
        duration_var = tk.StringVar(value=str(config.get("episode_duration_seconds", 90)))
        resolution_var = tk.StringVar(value=str(config.get("resolution", "720P")))
        style_var = tk.StringVar(value=str(config.get("style_preset", "都市悬疑真人电影")))
        custom_var = tk.StringVar(value=str(config.get("custom_style_prompt", "")))
        audio_var = tk.BooleanVar(value=bool(config.get("audio", True)))
        route_labels = {"native": "原生有声（视频模型出音轨）", "external_tts": "外部 TTS（单独配音）"}
        route_var = tk.StringVar(value=route_labels.get(str(config.get("voice_route") or "native"),
                                                        route_labels["native"]))
        concurrency_var = tk.StringVar(value=str(config.get("max_concurrent", 1)))
        voice_var = tk.StringVar(value=str(config.get("default_voice") or "Microsoft Huihui Desktop"))
        engine_labels = {"sapi": "离线 SAPI（免费）", "dashscope": "云端 CosyVoice（按角色音色，计费）"}
        engine_var = tk.StringVar(value=engine_labels.get(str(config.get("tts_provider") or "sapi"),
                                                          engine_labels["sapi"]))

        fields = [
            ("生成集数", ttk.Combobox(panel, textvariable=episode_var, values=episodes)),
            ("目标总时长（秒）", ttk.Entry(panel, textvariable=duration_var)),
            ("画质", ttk.Combobox(panel, textvariable=resolution_var, state="readonly", values=["480P", "720P", "1080P"])),
            ("画面风格", ttk.Combobox(panel, textvariable=style_var, state="readonly", values=list(batch_video_engine.STYLE_PRESETS))),
            ("自定义风格补充", ttk.Entry(panel, textvariable=custom_var)),
        ]
        for row, (label_text, widget) in enumerate(fields):
            tk.Label(panel, text=label_text, bg=self.BG, fg=self.TEXT, font=("Microsoft YaHei UI", 10)).grid(
                row=row, column=0, sticky="w", pady=9, padx=(0, 16))
            widget.grid(row=row, column=1, sticky="ew", pady=9)

        # 用经典 Checkbutton：clam 主题的 ttk 勾选框会画成“×”，容易被当成报错。
        audio_row = tk.Frame(panel, bg=self.BG)
        audio_row.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 2))
        tk.Checkbutton(
            audio_row, text="生成原生声音（有台词镜头自动锁定原句）", variable=audio_var,
            bg=self.BG, fg=self.TEXT, activebackground=self.BG, activeforeground=self.TEXT,
            selectcolor=self.PANEL_2, bd=0, highlightthickness=0, cursor="hand2",
            font=("Microsoft YaHei UI", 10), anchor="w",
        ).pack(side="left")
        audio_state = tk.Label(audio_row, text="", bg=self.BG, font=("Microsoft YaHei UI", 10, "bold"))
        audio_state.pack(side="left", padx=(10, 0))
        audio_hint = tk.Label(
            panel, text="", bg=self.BG, fg=self.MUTED, wraplength=470, justify="left",
            font=("Microsoft YaHei UI", 9),
        )
        audio_hint.grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 2))

        def refresh_audio_state(*_args) -> None:
            if audio_var.get():
                audio_state.configure(text="开", fg=self.GREEN)
                audio_hint.configure(text="镜头会带原生音轨；合片后成片直接有声音。", fg=self.MUTED)
            else:
                audio_state.configure(text="关", fg=self.AMBER)
                audio_hint.configure(text="镜头不带音轨，成片没有声音，需要在后期配音。", fg=self.AMBER)

        audio_var.trace_add("write", refresh_audio_state)
        refresh_audio_state()

        tk.Label(
            panel, text="时长会按镜头原始占比分配，每镜 4–8 秒（下限 4 秒是实测可用底线，可在 video-provider.json 调整）；仅处理这里选中的集数。",
            bg=self.BG, fg=self.MUTED, wraplength=470, justify="left", font=("Microsoft YaHei UI", 9),
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 10))

        tk.Label(panel, text="配音路线", bg=self.BG, fg=self.TEXT, font=("Microsoft YaHei UI", 10)).grid(
            row=8, column=0, sticky="w", pady=6, padx=(0, 16))
        ttk.Combobox(panel, textvariable=route_var, state="readonly",
                     values=list(route_labels.values())).grid(row=8, column=1, sticky="ew", pady=6)

        tk.Label(panel, text="并发 / 默认音色", bg=self.BG, fg=self.TEXT, font=("Microsoft YaHei UI", 10)).grid(
            row=9, column=0, sticky="w", pady=6, padx=(0, 16))
        row9 = tk.Frame(panel, bg=self.BG)
        row9.grid(row=9, column=1, sticky="ew", pady=6)
        ttk.Entry(row9, textvariable=concurrency_var, width=5).pack(side="left")
        tk.Label(row9, text="并发 1-4", bg=self.BG, fg=self.MUTED, font=("Microsoft YaHei UI", 8)).pack(
            side="left", padx=(6, 14))
        ttk.Entry(row9, textvariable=voice_var, width=20).pack(side="left", fill="x", expand=True)

        tk.Label(panel, text="配音引擎", bg=self.BG, fg=self.TEXT, font=("Microsoft YaHei UI", 10)).grid(
            row=10, column=0, sticky="w", pady=(6, 14), padx=(0, 16))
        row10 = tk.Frame(panel, bg=self.BG)
        row10.grid(row=10, column=1, sticky="ew", pady=(6, 14))
        ttk.Combobox(row10, textvariable=engine_var, state="readonly",
                     values=list(engine_labels.values())).pack(side="left", fill="x", expand=True)
        self._mini_button(row10, "按角色绑音色", self.open_voice_binding).pack(side="left", padx=(8, 0))

        def save_settings() -> None:
            try:
                duration = int(duration_var.get().strip())
                if duration <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror(APP_NAME, "目标总时长必须是正整数秒。", parent=window)
                return
            episode_choice = episode_var.get().strip().upper()
            shot_rows = self._read_csv(self.project_path / "03_storyboard" / f"{episode_choice}-shotlist.csv")
            minimum = batch_video_engine.min_shot_limit(config) * len(shot_rows)
            if minimum and duration < minimum:
                if not messagebox.askyesno(
                    "时长偏短",
                    f"{episode_choice} 有 {len(shot_rows)} 个镜头，每镜最短 {batch_video_engine.min_shot_limit(config)} 秒，"
                    f"实际至少要 {minimum} 秒。\n"
                    f"仍保存 {duration} 秒吗？（批量时会按 {minimum} 秒执行）",
                    parent=window,
                ):
                    return
            try:
                concurrency = max(1, min(4, int(concurrency_var.get().strip() or 1)))
            except ValueError:
                concurrency = 1
            route_key = "native" if route_var.get().startswith("原生") else "external_tts"
            config.update({
                "selected_episode": episode_var.get(), "episode_duration_seconds": duration,
                "resolution": resolution_var.get(), "style_preset": style_var.get(),
                "custom_style_prompt": custom_var.get().strip(), "audio": audio_var.get(),
                "voice_route": route_key, "max_concurrent": concurrency,
                "default_voice": voice_var.get().strip() or "Microsoft Huihui Desktop",
                "tts_provider": "dashscope" if engine_var.get().startswith("云端") else "sapi",
            })
            path = self._video_provider_path()
            if path:
                path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self._render_generation_target()
            if episode_var.get().strip().upper() != self._selected_generation_episode():
                self.load_project()
            else:
                self.refresh_execution()
            self.log(f"视频生成设置已保存：{episode_var.get()} / {duration}秒 / "
                     f"{resolution_var.get()} / {style_var.get()} / 配音 {config['tts_provider']}")
            window.destroy()

        self._button(bottom, "保存设置", save_settings, self.BLUE).pack(side="right", padx=5)
        self._button(bottom, "取消", window.destroy, self.PANEL_2).pack(side="right")
        tk.Label(bottom, text="表单可上下滚动", bg=self.BG, fg=self.MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(side="left")

    @staticmethod
    def _scroll_canvas_event(canvas: tk.Canvas):
        def handler(event) -> None:
            canvas.yview_scroll(int(-event.delta / 120), "units")
        return handler

    def _next_free_episode(self, episodes: list[str]) -> str:
        numbers = [int(name[2:]) for name in episodes if name[2:].isdigit()]
        return f"EP{max(numbers) + 1:03d}" if numbers else "EP001"

    def _ask_import_episode(self, source: Path) -> str | None:
        episodes = self._available_episodes()
        suggestion = self._next_free_episode(episodes)
        window = tk.Toplevel(self)
        window.title("上传剧本并生成分镜")
        window.geometry("560x360")
        window.resizable(False, False)
        window.configure(bg=self.BG)
        window.transient(self)
        window.grab_set()

        body = tk.Frame(window, bg=self.BG)
        body.pack(fill="both", expand=True, padx=26, pady=22)
        body.grid_columnconfigure(1, weight=1)
        tk.Label(body, text="剧本文件", bg=self.BG, fg=self.MUTED, font=("Microsoft YaHei UI", 10)).grid(row=0, column=0, sticky="nw", pady=10, padx=(0, 14))
        tk.Label(body, text=source.name, bg=self.BG, fg=self.TEXT, wraplength=360, justify="left", font=("Microsoft YaHei UI", 10)).grid(row=0, column=1, sticky="w", pady=10)
        tk.Label(body, text="目标集数", bg=self.BG, fg=self.MUTED, font=("Microsoft YaHei UI", 10)).grid(row=1, column=0, sticky="w", pady=10, padx=(0, 14))
        episode_var = tk.StringVar(value=suggestion)
        combo = ttk.Combobox(body, textvariable=episode_var, values=episodes + [suggestion])
        combo.grid(row=1, column=1, sticky="ew", pady=10)
        tk.Label(
            body,
            text="支持 TXT / MD / DOCX / PDF。识别与分镜在本地完成，不调用付费模型；\n已有的剧本或镜头表会先备份再覆盖。",
            bg=self.BG, fg=self.MUTED, justify="left", wraplength=470, font=("Microsoft YaHei UI", 9),
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 16))

        result: dict[str, str | None] = {"episode": None}

        def confirm() -> None:
            raw = episode_var.get().strip().upper()
            digits = raw[2:] if raw.startswith("EP") else raw
            if not digits.isdigit() or int(digits) <= 0:
                messagebox.showerror(APP_NAME, "集数格式无效，请输入 EP002 或 2。", parent=window)
                return
            result["episode"] = f"EP{int(digits):03d}"
            window.destroy()

        buttons = tk.Frame(body, bg=self.BG)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e")
        self._button(buttons, "开始识别", confirm, self.CYAN).pack(side="left", padx=6)
        self._button(buttons, "取消", window.destroy, self.PANEL_2).pack(side="left", padx=6)
        combo.focus_set()
        window.bind("<Return>", lambda _event: confirm())
        window.wait_window()
        return result["episode"]

    def _project_characters(self) -> list[str]:
        """优先用分析结果里的角色，其次用镜头表里的说话人。"""
        names: list[str] = []
        if self.project_path:
            analysis = analysis_engine.load_analysis(self.project_path, self._selected_generation_episode())
            if analysis:
                names = [item.get("name", "") for item in analysis.get("characters", []) if item.get("name")]
        if not names:
            for shot in self.shots:
                speaker = (shot.get("speaker") or "").strip()
                if speaker and speaker not in {"无", "无台词", "旁白"} and speaker not in names:
                    names.append(speaker)
        return names

    def open_voice_binding(self) -> None:
        """按角色绑定云端音色（写入 video-provider.json 的 voice_map / voice_rate）。"""
        if not self.project_path:
            return
        import cloud_tts

        self._ensure_video_provider_config()
        config = self._video_provider_config()
        voice_map = dict(config.get("voice_map") or {})
        rate_map = dict(config.get("voice_rate") or {})
        characters = self._project_characters()
        if not characters:
            messagebox.showinfo(APP_NAME, "还没有角色信息。先上传剧本并解析，或直接在镜头表里填说话人。")
            return

        window = tk.Toplevel(self)
        window.title("按角色绑音色")
        window.geometry("620x480")
        window.configure(bg=self.BG)
        window.transient(self)
        window.grab_set()
        tk.Label(window, text="云端音色（可下拉选择，也可直接填官方 voice id）", bg=self.BG, fg=self.TEXT,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", padx=20, pady=(16, 4))
        tk.Label(window, text="语速是 SAPI 刻度（-10~10），调用云端时会换算成 0.5~2.0 倍率。",
                 bg=self.BG, fg=self.MUTED, font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=20)

        body = tk.Frame(window, bg=self.BG)
        body.pack(fill="both", expand=True, padx=20, pady=10)
        body.grid_columnconfigure(1, weight=1)
        catalog_labels = {f"{label}（{voice}）": voice for voice, label in cloud_tts.VOICE_CATALOG.items()}
        reverse = {voice: label for label, voice in catalog_labels.items()}
        entries: dict[str, tuple[tk.StringVar, tk.StringVar]] = {}
        for row, name in enumerate(characters):
            tk.Label(body, text=name, bg=self.BG, fg=self.TEXT, font=("Microsoft YaHei UI", 10)).grid(
                row=row, column=0, sticky="w", pady=6, padx=(0, 12))
            current = str(voice_map.get(name) or "")
            voice_var = tk.StringVar(value=reverse.get(current, current or ""))
            combo = ttk.Combobox(body, textvariable=voice_var, values=list(catalog_labels.keys()))
            combo.grid(row=row, column=1, sticky="ew", pady=6)
            rate_var = tk.StringVar(value=str(rate_map.get(name, voice_engine.DEFAULT_RATES.get(name, 0))))
            ttk.Entry(body, textvariable=rate_var, width=6).grid(row=row, column=2, sticky="e", padx=(8, 0))
            entries[name] = (voice_var, rate_var)

        result: dict[str, bool] = {"saved": False}

        def save() -> None:
            new_map: dict[str, str] = {}
            new_rate: dict[str, int] = {}
            for name, (voice_var, rate_var) in entries.items():
                raw = voice_var.get().strip()
                if not raw:
                    continue
                voice_id = catalog_labels.get(raw, raw)
                new_map[name] = voice_id
                try:
                    new_rate[name] = int(rate_var.get().strip() or 0)
                except ValueError:
                    new_rate[name] = 0
            config["voice_map"] = new_map
            config["voice_rate"] = new_rate
            config.setdefault("tts_provider", "dashscope")
            path = self._video_provider_path()
            if path:
                path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            result["saved"] = True
            self.log("已保存角色音色绑定：" + "、".join(f"{k}={v}" for k, v in new_map.items()))
            window.destroy()

        buttons = tk.Frame(window, bg=self.BG)
        buttons.pack(fill="x", padx=20, pady=(0, 16))
        self._button(buttons, "保存绑定", save, self.BLUE).pack(side="right")
        self._button(buttons, "取消", window.destroy, self.PANEL_2).pack(side="right", padx=8)
        window.wait_window()

    def import_script_and_storyboard(self) -> None:
        if not self.project_path:
            return
        source = filedialog.askopenfilename(
            title="选择单集剧本",
            filetypes=[("支持的剧本", "*.txt *.md *.docx *.pdf"), ("文本", "*.txt *.md"), ("Word", "*.docx"), ("PDF", "*.pdf")],
        )
        if not source:
            return
        episode = self._ask_import_episode(Path(source))
        if not episode:
            return
        script_target = self.project_path / "02_script" / f"{episode}-script.md"
        shotlist_target = self.project_path / "03_storyboard" / f"{episode}-shotlist.csv"
        overwrite = False
        if script_target.exists() or shotlist_target.exists():
            overwrite = messagebox.askyesno(
                "覆盖确认", f"{episode} 已存在剧本或镜头表。\n旧文件会先备份，再写入新的识别结果。是否继续？", parent=self,
            )
            if not overwrite:
                return
        if self.busy:
            messagebox.showwarning(APP_NAME, "已有任务正在运行，请稍候。")
            return
        self.busy = True
        self.status_badge.configure(text="识别剧本中", fg=self.AMBER)
        self.log(f"开始识别剧本：{source} → {episode}")

        def worker() -> None:
            try:
                result = script_storyboard_engine.import_script(
                    self.project_path, Path(source), episode, overwrite=overwrite
                )
                self.after(0, lambda: self._finish_script_import(result))
            except Exception as exc:
                error_message = str(exc)
                self.after(0, lambda value=error_message: self._finish_command("上传剧本并生成分镜", -1, value, None))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_script_import(self, result: dict) -> None:
        self.busy = False
        seconds = 0.0
        try:
            with timings.step_timer(self.project_path, result["episode"], "analysis") as timer:
                analysis_engine.analyze(self.project_path, result["episode"], force=True)
            seconds = timer.last
        except Exception as exc:  # noqa: BLE001
            self.log(f"剧本分析失败（不影响分镜）：{exc}")
        self.load_project()
        storyboard = Path(result["storyboard"])
        self.log(
            f"剧本识别完成：{result['episode']} / {result.get('scenes', '?')} 场 / {result['shots']} 个镜头 / "
            f"{result.get('dialogues', '?')} 句台词 / 预计 {result.get('seconds', '?')} 秒 → {storyboard}"
        )
        self.status_badge.configure(text="分镜待审", fg=self.AMBER)
        self._open_path(storyboard)
        messagebox.showinfo(
            APP_NAME,
            f"{result['episode']} 剧本识别完成。\n"
            f"已识别 {result.get('scenes', '?')} 场、{result['shots']} 个镜头、{result.get('dialogues', '?')} 句台词，\n"
            f"预计 {result.get('seconds', '?')} 秒，并生成可视化分镜。\n\n"
            f"请审核分镜后，再点击“生成预览（不提交）”确认镜头与提示词，最后批量生成。\n\n"
            f"剧本分析已生成（{seconds:.1f}s）：角色、节拍、情绪曲线、关系图见“剧本分析”页。",
            parent=self,
        )

    def open_selected_storyboard(self) -> None:
        if not self.project_path:
            return
        episode = self._selected_generation_episode()
        path = self.project_path / "03_storyboard" / f"{episode}-visual-storyboard.html"
        if not path.exists():
            messagebox.showwarning(APP_NAME, f"{episode} 还没有可视化分镜。请先上传该集剧本。")
            return
        self._open_path(path)

    def _ensure_video_provider_config(self) -> None:
        if not self.project_path:
            return
        target = self.project_path / "09_automation" / "video-provider.json"
        if target.exists():
            return
        source = self.workflow_root / "templates" / "video-provider.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        self.log(f"已补齐当前项目的视频生成配置：{target}")

    def check_generation_environment(self) -> None:
        self._ensure_video_provider_config()
        command = self._batch_worker_command("doctor")
        if command:
            self._run_background(command, "检测视频生成环境")

    def preview_generation_plan(self) -> None:
        if not self.project_path:
            return
        self._ensure_video_provider_config()
        command = self._batch_worker_command("plan")
        if command:
            self._run_streaming_background(command, f"生成预览 {self._selected_generation_episode()}")

    def batch_generate_current_episode(self) -> None:
        if not self.project_path:
            return
        self._ensure_video_provider_config()
        episode = self._selected_generation_episode()
        config = self._video_provider_config()
        audio_off = not bool(config.get("audio", True))
        voice_note = ""
        if str(config.get("voice_route") or "native") == "external_tts" and voice_engine.provider_of(config) == "dashscope":
            estimate = voice_engine.estimate(self.shots, config)
            voice_note = f"\n\n本批含云端配音约 {estimate['units']} 计费单位（约 ¥{estimate['cny']:.4f}）。"
        warning = (
            "\n\n⚠ 当前“视频生成设置”里声音是关闭的：这一批镜头不会带任何音轨，"
            "成片只能后期配音。需要原生声音，请先到“视频生成设置”勾上“生成原生声音”。"
            if audio_off else ""
        )
        approved = messagebox.askyesno(
            "确认付费批量生成",
            f"即将通过阿里云为 {episode} 批量处理 {self._selected_shot_count()} 个镜头。\n\n"
            "已有 task_id 或已成功镜头会继续复用，不重复提交；新镜头会产生模型费用。\n"
            "是否确认开始？" + voice_note + warning,
            parent=self,
            icon="warning" if audio_off else "question",
        )
        if approved:
            self._run_streaming_background(
                self._batch_worker_command("batch"), f"自动批量生成 {episode}", lambda: self.load_project()
            )

    def resume_batch_generation(self) -> None:
        if not self.project_path:
            return
        self._ensure_video_provider_config()
        approved = messagebox.askyesno(
            "继续未完成的生成",
            "将先轮询已有 task_id 的镜头；缺少任务的镜头会重新提交并产生费用。\n是否继续？",
            parent=self,
        )
        if not approved:
            return
        command = self._batch_worker_command("resume")
        if command:
            self._run_streaming_background(command, "恢复未完成的视频任务", lambda: self.load_project())

    def generate_voice(self) -> None:
        """外部 TTS 路线：单独跑一遍本集配音（也用于补配音）。"""
        if not self.project_path:
            return
        if self.busy:
            messagebox.showwarning(APP_NAME, "已有任务正在运行，请稍候。")
            return
        episode = self._selected_generation_episode()
        config = self._video_provider_config()
        if str(config.get("voice_route") or "native") != "external_tts":
            if not messagebox.askyesno(
                "生成本集配音",
                "当前配音路线是“原生有声”，走视频模型的音轨。\n"
                "仍要额外用离线 TTS 生成一套配音吗？（可用于补配音或对比）",
                parent=self,
            ):
                return
        if voice_engine.provider_of(config) == "dashscope":
            estimate = voice_engine.estimate(self.shots, config)
            if not messagebox.askyesno(
                "云端配音确认",
                f"将用云端 CosyVoice 按角色音色合成 {estimate['units']} 个计费单位，"
                f"约 ¥{estimate['cny']:.4f}。\n是否继续？",
                parent=self,
            ):
                return
        self.busy = True
        self.status_badge.configure(text="配音中", fg=self.AMBER)
        self.log(f"开始生成 {episode} 配音…")

        def worker() -> None:
            try:
                with timings.step_timer(self.project_path, episode, "voice") as timer:
                    result = voice_engine.synthesize(
                        self.project_path, episode, self.shots, config,
                        progress=lambda message: self.after(0, lambda value=message: self.log(value)),
                    )
                    voice_engine.qc(self.project_path, episode, self.shots)
                done = sum(1 for row in result.get("rows", []) if row.get("status") == "已完成")
                silent = sum(1 for row in result.get("rows", []) if row.get("status") == "无台词")
                message = f"配音完成：{done} 句，无台词 {silent} 镜，用时 {timer.last:.1f}s"
                self.after(0, lambda: self._finish_voice(message))
            except Exception as exc:  # noqa: BLE001
                detail = str(exc)
                self.after(0, lambda value=detail: self._finish_command("生成本集配音", -1, value, None))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_voice(self, message: str) -> None:
        self.busy = False
        self.log(message)
        self.status_badge.configure(text="配音完成", fg=self.GREEN)
        self.load_project()
        messagebox.showinfo(APP_NAME, message)

    def assemble_current_episode(self) -> None:
        self._ensure_video_provider_config()
        command = self._batch_worker_command("assemble")
        if command:
            self._run_streaming_background(command, "合成当前集视频", lambda: self.load_project())

    def generate_context(self) -> None:
        if not self.project_path:
            return
        stage = simpledialog.askstring("低 Token 上下文", "阶段：brief / bible / script / storyboard / generation / edit / qc / release", initialvalue="generation", parent=self)
        if not stage:
            return
        stage = stage.strip().lower()
        if stage not in {"brief", "bible", "script", "storyboard", "generation", "edit", "qc", "release"}:
            messagebox.showerror(APP_NAME, "阶段名称无效。")
            return
        script = self.workflow_root / "scripts" / "Get-ShortDramaContext.ps1"
        self._run_background(
            [self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-ProjectPath", str(self.project_path), "-Stage", stage, "-Episode", self.state_data.get("current_episode", "EP001")],
            f"生成 {stage} 阶段上下文",
            self._copy_last_output,
        )

    def _copy_last_output(self) -> None:
        if hasattr(self, "last_command_output"):
            self.clipboard_clear()
            self.clipboard_append(self.last_command_output)
            messagebox.showinfo(APP_NAME, "低 Token 上下文已复制到剪贴板。")

    def _run_background(self, command: list[str], title: str, on_success=None) -> None:
        if self.busy:
            messagebox.showwarning(APP_NAME, "已有任务正在运行，请稍候。")
            return
        self.busy = True
        self.status_badge.configure(text="运行中", fg=self.AMBER)
        self.log(f"开始：{title}")

        def worker() -> None:
            try:
                completed = subprocess.run(
                    command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    creationflags=CREATE_NO_WINDOW,
                )
                output = (completed.stdout or "") + (completed.stderr or "")
                self.last_command_output = output.strip()
                self.after(0, lambda: self._finish_command(title, completed.returncode, output, on_success))
            except Exception as exc:
                error_message = str(exc)
                self.after(0, lambda value=error_message: self._finish_command(title, -1, value, None))

        threading.Thread(target=worker, daemon=True).start()

    def _run_streaming_background(self, command: list[str], title: str, on_success=None) -> None:
        if self.busy:
            messagebox.showwarning(APP_NAME, "已有任务正在运行，请稍候。")
            return
        self.busy = True
        self.streaming = True
        self.generation_started_at = datetime.now()
        self.generation_current = ""
        self.generation_total = len(self.shots)
        self.status_badge.configure(text="批量运行中", fg=self.AMBER)
        self.log(f"开始：{title}")
        self.notebook.select(self.execution_tab)
        self.refresh_execution()
        self._tick_generation()

        def worker() -> None:
            output_lines: list[str] = []
            try:
                process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
                )
                if process.stdout:
                    for line in process.stdout:
                        line_text = line.rstrip()
                        output_lines.append(line_text)
                        if line_text:
                            self.after(0, lambda value=line_text: self._handle_worker_line(value))
                code = process.wait()
                self.last_command_output = "\n".join(output_lines)
                self.after(0, lambda: self._finish_command(title, code, "", on_success))
            except Exception as exc:
                error_message = str(exc)
                self.after(0, lambda value=error_message: self._finish_command(title, -1, value, None))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_command(self, title: str, code: int, output: str, on_success) -> None:
        self.busy = False
        self.streaming = False
        self.log(output.strip() or "命令没有输出。")
        if code == 0:
            self.log(f"完成：{title}")
            self.status_badge.configure(text=self.state_data.get("current_stage", "就绪"), fg=self.CYAN)
            if on_success:
                on_success()
        else:
            self.log(f"失败：{title}（退出码 {code}）")
            self.status_badge.configure(text="执行失败", fg=self.RED)
            detail = output.strip()[-800:] if output.strip() else getattr(self, "last_command_output", "")[-800:]
            messagebox.showerror(APP_NAME, f"{title}失败。\n\n{detail or '请查看运行日志。'}")
        self.refresh_execution()

    def on_close(self) -> None:
        if self.busy:
            messagebox.showwarning(APP_NAME, "批量任务仍在运行。请等待完成；如需中断，可稍后重新打开工作台并点击“恢复未完成的生成”。")
            return
        self.config_data["workflow_root"] = str(self.workflow_root)
        if self.project_path:
            self.config_data["active_project"] = self.project_path.name
        save_config(self.config_data)
        self.destroy()


if __name__ == "__main__":
    if "--lipcheck" in sys.argv:
        # 诊断用：AI短剧自动化工作台.exe --lipcheck <项目目录> <集数> <镜号>
        args = [item for item in sys.argv[1:] if item != "--lipcheck"]
        if len(args) >= 3:
            import lip_sync

            project = Path(args[0])
            result = lip_sync.detect(project, args[1], args[2], force=True)
            target = project / "07_qc" / "lipcheck.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                __import__("json").dumps({**result, "python": sys.executable}, ensure_ascii=False, indent=2),
                encoding="utf-8")
            raise SystemExit(0)

    if "--batch-worker" in sys.argv:
        args = list(sys.argv[1:])
        args.remove("--batch-worker")
        raise SystemExit(batch_video_engine.main(args))
    app = StudioApp()
    app.mainloop()
