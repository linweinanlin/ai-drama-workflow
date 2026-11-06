"""质检交付面板：自动质检表 + 逐镜人工结论 + 交付登记。"""
from __future__ import annotations

import re
import shutil
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

import batch_video_engine
import qc_engine

COLUMNS = (
    ("shot", "镜头", 110),
    ("video", "视频", 60),
    ("duration", "时长", 90),
    ("voice", "配音", 70),
    ("text", "台词", 60),
    ("frame", "帧链", 60),
    ("scene", "场景", 60),
    ("sync", "对齐", 80),
    ("verdict", "人工结论", 90),
    ("note", "备注", 220),
)


class QCPanel(tk.Frame):
    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, bg=app.BG)
        self.app = app
        self.result: dict[str, Any] | None = None
        self.review: dict[str, dict[str, str]] = {}
        self.episode = str(getattr(app, "loaded_episode", "") or app._selected_generation_episode())
        self.shots: list[dict[str, str]] = list(app.shots)
        self._build()
        self.refresh()

    # ---------- 骨架 ----------
    def _build(self) -> None:
        app = self.app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        bar = tk.Frame(self, bg=app.BG)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(8, 8))
        ttk.Label(bar, text="质检交付", style="Header.TLabel").pack(side="left")
        tk.Label(bar, text="集数", bg=app.BG, fg=app.MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(16, 6))
        self.episode_var = tk.StringVar(value=self.episode)
        self.episode_box = ttk.Combobox(bar, textvariable=self.episode_var, width=9, state="readonly")
        self.episode_box.pack(side="left")
        self.episode_box.bind("<<ComboboxSelected>>", lambda _e: self.select_episode(self.episode_var.get()))
        self.status_label = tk.Label(bar, text="", bg=app.BG, fg=app.MUTED, font=("Microsoft YaHei UI", 9))
        self.status_label.pack(side="left", padx=(12, 0))
        self._mini(bar, "播放成片", self.open_final).pack(side="right")
        self.sync_button = self._mini(bar, "对齐修复（配音/字幕）", self.run_sync)
        self.sync_button.pack(side="right", padx=(0, 8))
        self._mini(bar, "口型与表情表", self.run_performance).pack(side="right", padx=(0, 8))
        self._mini(bar, "交付登记", self.open_release_dialog).pack(side="right", padx=(0, 8))
        self._mini(bar, "运行自动质检", self.run_auto).pack(side="right", padx=(0, 8))

        chips = tk.Frame(self, bg=app.BG)
        chips.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.summary_chips: dict[str, tk.Label] = {}
        for name in ("自动通过", "人工通过", "待审", "返工", "成片"):
            label = tk.Label(chips, text=f"{name} —", bg=app.PANEL_2, fg=app.TEXT, padx=12, pady=6,
                             font=("Microsoft YaHei UI", 9))
            label.pack(side="left", padx=(0, 8))
            self.summary_chips[name] = label

        gates = tk.Frame(self, bg=app.BG)
        gates.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.gate_labels: list[tk.Label] = []
        for index in range(6):
            label = tk.Label(gates, text="—", bg=app.PANEL_2, fg=app.MUTED, padx=10, pady=5,
                             font=("Microsoft YaHei UI", 9, "bold"))
            label.grid(row=0, column=index, sticky="ew", padx=(0, 6))
            gates.grid_columnconfigure(index, weight=1, uniform="gates")
            self.gate_labels.append(label)

        self.tree = ttk.Treeview(self, columns=[key for key, _, _ in COLUMNS], show="headings")
        for key, label, width in COLUMNS:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w")
        self.tree.tag_configure("ok", foreground=app.GREEN)
        self.tree.tag_configure("bad", foreground=app.RED)
        self.tree.tag_configure("warn", foreground=app.AMBER)
        self.tree.grid(row=3, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=3, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Double-1>", lambda _e: self.open_detail())

        self.foot_label = tk.Label(self, text="双击镜头行打开详情：看首/中/尾帧、标通过或返工。",
                                   bg=app.BG, fg=app.MUTED, font=("Microsoft YaHei UI", 8),
                                   anchor="w", justify="left")
        self.foot_label.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 12))

    def _mini(self, parent, text: str, command) -> tk.Button:
        return tk.Button(
            parent, text=text, command=command, bg=self.app.PANEL_2, fg=self.app.TEXT,
            activebackground=self.app.BLUE, activeforeground=self.app.TEXT,
            relief="flat", bd=0, cursor="hand2", padx=10, pady=5,
            font=("Microsoft YaHei UI", 9),
        )

    # ---------- 刷新 ----------
    def refresh(self) -> None:
        app = self.app
        for item in self.tree.get_children():
            self.tree.delete(item)
        if not app.project_path:
            self.status_label.configure(text="没有可质检的镜头", fg=app.AMBER)
            self.foot_label.configure(text="先选择项目并确保这一集有镜头。")
            return
        episodes = self._episodes()
        self.episode_box.configure(values=episodes)
        if self.episode not in episodes:
            self.episode = episodes[0] if episodes else ""
        if self.episode:
            self.episode_var.set(self.episode)
        shots = app._load_episode_shots(self.episode) if self.episode else []
        if not shots:
            shots = list(app.shots)
        self.shots = shots
        if not shots:
            self.status_label.configure(text=f"{self.episode} 没有镜头", fg=app.AMBER)
            self.foot_label.configure(text="这一集还没有镜头表；先在“上传剧本并生成分镜”里生成。")
            return
        episode = self.episode
        config = app._video_provider_config()
        self.review = qc_engine.load_review(app.project_path, episode)
        self.result = qc_engine.auto_checks(app.project_path, episode, self.shots, config)

        summary = self.result["summary"]
        final = self.result["final"]
        passed = sum(1 for row in self.review.values() if row.get("verdict") == "通过")
        rework = sum(1 for row in self.review.values() if row.get("verdict") == "返工")
        pending = len(self.shots) - passed - rework
        self.summary_chips["自动通过"].configure(
            text=f"自动通过 {summary['passed']}/{summary['shots']}",
            fg=app.GREEN if summary["passed"] == summary["shots"] else app.AMBER)
        self.summary_chips["人工通过"].configure(text=f"人工通过 {passed}", fg=app.GREEN if passed else app.TEXT)
        self.summary_chips["待审"].configure(text=f"待审 {pending}", fg=app.AMBER if pending else app.MUTED)
        self.summary_chips["返工"].configure(text=f"返工 {rework}", fg=app.RED if rework else app.MUTED)
        self.summary_chips["成片"].configure(
            text=("成片 " + (f"{final['duration']} 秒" if final["exists"] else "缺失"))
            + ("" if not final["exists"] else (" · 含音轨" if final["has_audio"] else " · 无音轨")),
            fg=app.GREEN if final["exists"] else app.RED)

        for label, (name, status, color) in zip(self.gate_labels, app._quality_statuses()):
            label.configure(text=f"{name}\n{status}", fg=color)

        for row in self.result["shots"]:
            shot_id = row["shot_id"]
            review = self.review.get(shot_id, {})
            verdict = review.get("verdict", "待审")
            tag = "ok" if row["ok"] and verdict == "通过" else ("bad" if (not row["ok"] or verdict == "返工") else "warn")
            self.tree.insert("", "end", iid=shot_id, tags=(tag,), values=(
                shot_id,
                "有" if row["video"] else "缺失",
                f"{row['duration']}/{row['planned']}",
                ("已配音" if row["voice"] else "失败") if row["voice_status"] not in ("", "无台词") else "无台词",
                "一致" if row["text_ok"] else "不一致",
                "有" if row["frame_ok"] else "待补充",
                "有" if row["scene_ok"] else "待补充",
                self._sync_text(row),
                verdict,
                review.get("note", ""),
            ))
        self.status_label.configure(
            text=f"{episode} · {len(self.shots)} 镜 · 检查于 {self.result['checked_at'][11:]}",
            fg=app.MUTED)
        synced = self.result["summary"].get("synced")
        count = self.result["summary"].get("sync_cues", 0)
        prefix = f"已做对齐：{count} 条字幕 · " if synced else "尚未做对齐 · "
        self.foot_label.configure(
            text=prefix + "自动检查：视频/时长/配音/台词/帧链/场景；人工结论存 07_qc/"
                 + f"{episode}-qc-review.json，交付登记写 08_release/release-manifest.csv")

    # ---------- 动作 ----------
    def run_auto(self) -> None:
        if not self.app.project_path:
            return
        episode = self.episode or self.app._selected_generation_episode()
        config = self.app._video_provider_config()
        self.result = qc_engine.auto_checks(self.app.project_path, episode, self.shots, config)
        path = qc_engine.write_auto(self.app.project_path, self.result)
        self.refresh()
        self.app.log(f"自动质检完成：{self.result['summary']['passed']}/{self.result['summary']['shots']} 通过 → {path.name}")
        messagebox.showinfo("质检交付", f"自动质检完成：{self.result['summary']['passed']}/{self.result['summary']['shots']} 镜通过。\n"
                                     f"结果已写入 {path.name} 与 {episode}-qc.md。")

    def open_final(self) -> None:
        path = self._final_path()
        if not path:
            messagebox.showwarning("质检交付", f"{self.episode} 还没有成片。")
            return
        self.app._open_path(path)

    def _final_path(self) -> Path | None:
        if not self.app.project_path:
            return None
        exports = self.app.project_path / "06_edit" / "exports"
        for name in (f"{self.episode}-auto-final-subtitled.mp4", f"{self.episode}-auto-final-synced.mp4",
                     f"{self.episode}-auto-final-with-audio.mp4", f"{self.episode}-auto-final.mp4"):
            candidate = exports / name
            if candidate.exists():
                return candidate
        latest = sorted(exports.glob(f"{self.episode}*.mp4"), key=lambda item: item.stat().st_mtime, reverse=True)
        return latest[0] if latest else None

    def _episodes(self) -> list[str]:
        """列出所有已经生成过视频或成片的集。"""
        app = self.app
        found: list[str] = []
        if app.project_path:
            videos = app.project_path / "05_generation" / "videos"
            if videos.exists():
                found.extend(sorted(item.name for item in videos.iterdir() if item.is_dir()))
            exports = app.project_path / "06_edit" / "exports"
            if exports.exists():
                for item in exports.glob("EP*-auto-final*"):
                    prefix = item.name.split("-")[0]
                    if re.fullmatch(r"EP\d{3}", prefix) and prefix not in found:
                        found.append(prefix)
        current = str(getattr(app, "loaded_episode", "") or app._selected_generation_episode())
        if current and current not in found:
            found.append(current)
        return sorted(set(found)) or [current]

    def select_episode(self, episode: str) -> None:
        if not episode or episode == self.episode:
            return
        self.episode = episode
        self.app.log(f"质检交付切换到 {episode}")
        self.refresh()

    def open_detail(self) -> None:
        selection = self.tree.selection()
        if not selection or not self.result:
            return
        shot_id = selection[0]
        row = next((item for item in self.result["shots"] if item["shot_id"] == shot_id), None)
        if not row:
            return
        app = self.app
        episode = self.episode or app._selected_generation_episode()
        window = tk.Toplevel(self)
        window.title(f"质检详情 · {shot_id}")
        window.geometry("980x720")
        window.configure(bg=app.BG)

        left = tk.Frame(window, bg=app.BG)
        left.pack(side="left", fill="y", padx=16, pady=16)
        frames = self._frames(shot_id, episode)
        for label_text, path in frames:
            block = tk.Frame(left, bg=app.BG)
            block.pack(pady=(0, 6))
            tk.Label(block, text=label_text, bg=app.BG, fg=app.MUTED, font=("Microsoft YaHei UI", 8)).pack(anchor="w")
            if path:
                from PIL import Image, ImageTk

                image = Image.open(path)
                image.thumbnail((250, 444))
                photo = ImageTk.PhotoImage(image.convert("RGB"))
                holder = tk.Label(block, image=photo, bg=app.BG)
                holder.image = photo
                holder.pack()
            else:
                tk.Label(block, text="无画面", bg=app.PANEL, fg=app.MUTED, width=30, height=8).pack()

        right = tk.Frame(window, bg=app.BG)
        right.pack(side="left", fill="both", expand=True, padx=(8, 16), pady=16)
        tk.Label(right, text=shot_id, bg=app.BG, fg=app.TEXT, font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        details = [
            ("视频", "有" if row["video"] else "缺失"),
            ("时长", f"{row['duration']} 秒（计划 {row['planned']}）"),
            ("配音", f"{row['voice_status']} · {row['voice_provider'] or '—'}"),
            ("台词校验", "一致" if row["text_ok"] else "不一致"),
            ("台词原文", row["text"] or "无台词"),
            ("帧链", row["frame_ref"] or "缺失"),
            ("场景参考", row["scene_ref"] or "缺失"),
        ]
        for label, value in details:
            line = tk.Frame(right, bg=app.BG)
            line.pack(fill="x", pady=2)
            tk.Label(line, text=label, bg=app.BG, fg=app.MUTED, width=9, anchor="w",
                     font=("Microsoft YaHei UI", 9)).pack(side="left")
            tk.Label(line, text=str(value), bg=app.BG, fg=app.TEXT, anchor="w", wraplength=520,
                     justify="left", font=("Microsoft YaHei UI", 9)).pack(side="left")

        tk.Label(right, text="人工结论", bg=app.BG, fg=app.TEXT,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(12, 2))
        note_var = tk.StringVar(value=self.review.get(shot_id, {}).get("note", ""))
        entry = tk.Entry(right, textvariable=note_var, bg=app.PANEL_2, fg=app.TEXT,
                         insertbackground=app.TEXT, relief="flat", font=("Microsoft YaHei UI", 9))
        entry.pack(fill="x", ipady=4, pady=(0, 8))

        buttons = tk.Frame(right, bg=app.BG)
        buttons.pack(fill="x")

        def mark(verdict: str) -> None:
            qc_engine.save_verdict(app.project_path, episode, shot_id, verdict, note_var.get().strip())
            self.app.log(f"{shot_id} 人工结论：{verdict}")
            window.destroy()
            self.refresh()

        for verdict, color in (("通过", app.GREEN), ("返工", app.RED), ("待审", app.PANEL_2)):
            tk.Button(buttons, text=verdict, command=lambda v=verdict: mark(v), bg=color, fg=app.TEXT,
                      relief="flat", bd=0, padx=14, pady=6, cursor="hand2",
                      font=("Microsoft YaHei UI", 9, "bold")).pack(side="left", padx=(0, 8))
        self._mini(buttons, "播放这一镜", lambda: self._play(shot_id, episode)).pack(side="left", padx=(8, 0))

    def _play(self, shot_id: str, episode: str) -> None:
        video = self.app.project_path / "05_generation" / "videos" / episode / f"{shot_id}.mp4"
        if video.exists():
            self.app._open_path(video)

    def _frames(self, shot_id: str, episode: str) -> list[tuple[str, Path | None]]:
        video = self.app.project_path / "05_generation" / "videos" / episode / f"{shot_id}.mp4"
        if not video.exists():
            return [("无视频", None)]
        ffmpeg = batch_video_engine.find_ffmpeg()
        cache = self.app.project_path / "07_qc" / "frames"
        cache.mkdir(parents=True, exist_ok=True)
        result: list[tuple[str, Path | None]] = []
        probe = subprocess.run(
            [str(Path(ffmpeg).with_name("ffprobe.exe")) if ffmpeg else "ffprobe", "-v", "error",
             "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        try:
            duration = float((probe.stdout or "0").strip())
        except ValueError:
            duration = 0.0
        for label, ratio in (("首帧", 0.05), ("中帧", 0.5), ("尾帧", 0.95)):
            target = cache / f"{shot_id}-{label}.jpg"
            if not target.exists() or target.stat().st_mtime < video.stat().st_mtime:
                if ffmpeg:
                    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                                    "-ss", f"{max(0.0, duration * ratio):.2f}", "-i", str(video),
                                    "-frames:v", "1", "-vf", "scale=360:-1", str(target)],
                                   capture_output=True)
            result.append((label, target if target.exists() else None))
        return result

    def _sync_text(self, row: dict[str, Any]) -> str:
        if not row.get("text"):
            return "—"
        offset = row.get("sync_offset_ms")
        if row.get("sub_residue"):
            return "字幕残留"
        if offset is None:
            return "未对齐"
        return f"{offset}ms"

    def run_sync(self) -> None:
        """配音为准重排音轨、生成 SRT/ASS、混流并烧字幕。"""
        app = self.app
        if not app.project_path or not app.shots:
            return
        episode = self.episode or app._selected_generation_episode()
        config = app._video_provider_config()
        shots = list(self.shots)
        if self.sync_button.cget("state") == "disabled":
            return
        self.sync_button.configure(state="disabled", text="对齐中…")
        self.status_label.configure(text="正在对齐：测配音时长 → 重排音轨 → 生成字幕", fg=app.AMBER)

        def worker() -> None:
            import sync_engine

            try:
                result = sync_engine.align(app.project_path, episode, shots, config, height=1280)
                sync_engine.write_reports(app.project_path, result)
                outputs = sync_engine.remux_final(app.project_path, episode, burn_subtitles=True)
                self.after(0, lambda: self._sync_done(result, outputs))
            except Exception as exc:  # noqa: BLE001
                message = f"{type(exc).__name__}: {exc}"
                self.after(0, lambda: self._sync_failed(message))

        import threading

        threading.Thread(target=worker, daemon=True).start()

    def _sync_failed(self, message: str) -> None:
        self.sync_button.configure(state="normal", text="对齐修复（配音/字幕）")
        self.status_label.configure(text="对齐失败", fg=self.app.RED)
        self.app.log(f"对齐失败：{message}")
        messagebox.showerror("对齐修复", message)

    def _sync_done(self, result: dict[str, Any], outputs: dict[str, str]) -> None:
        app = self.app
        self.sync_button.configure(state="normal", text="对齐修复（配音/字幕）")
        self.refresh()
        timeline = result.get("timeline", [])
        problems = result.get("problems", [])
        summary = (f"对齐完成：{len(timeline)} 镜 · 总长 {result.get('total_duration')} 秒\n"
                   f"字幕 {sum(1 for item in timeline if item.get('text'))} 条 · 问题 {len(problems)} 个\n\n"
                   f"时间码表：07_qc/{result['episode']}-sync-timing.csv\n"
                   f"质检报告：07_qc/{result['episode']}-sync-report.csv\n"
                   f"字幕：{result['srt']}\n")
        if outputs.get("subtitled"):
            summary += f"带字幕成片：{outputs['subtitled']}"
        app.log(f"对齐完成：{len(timeline)} 镜，问题 {len(problems)} 个")
        messagebox.showinfo("对齐修复", summary)
        self._open_outputs_dialog(result, outputs)

    def _open_outputs_dialog(self, result: dict[str, Any], outputs: dict[str, str]) -> None:
        app = self.app
        window = tk.Toplevel(self)
        window.title("对齐产物")
        window.geometry("560x260")
        window.configure(bg=app.BG)
        tk.Label(window, text="可以直接打开检查", bg=app.BG, fg=app.TEXT,
                 font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=20, pady=(16, 8))
        rows = [
            ("带字幕成片", app.project_path / outputs["subtitled"]) if outputs.get("subtitled") else None,
            ("对齐后成片（无字幕）", app.project_path / outputs["synced"]) if outputs.get("synced") else None,
            ("字幕文件 SRT", app.project_path / result["srt"]),
            ("字幕文件 ASS", app.project_path / result["ass"]),
            ("时间码表 CSV", app.project_path / "07_qc" / f"{result['episode']}-sync-timing.csv"),
            ("质检报告 CSV", app.project_path / "07_qc" / f"{result['episode']}-sync-report.csv"),
        ]
        for item in rows:
            if not item:
                continue
            label, path = item
            line = tk.Frame(window, bg=app.BG)
            line.pack(fill="x", padx=20, pady=3)
            tk.Label(line, text=label, bg=app.BG, fg=app.MUTED, width=16, anchor="w",
                     font=("Microsoft YaHei UI", 9)).pack(side="left")
            tk.Button(line, text="打开", command=lambda p=path: app._open_path(p),
                      bg=app.PANEL_2, fg=app.TEXT, relief="flat", bd=0, padx=10, pady=2,
                      cursor="hand2", font=("Microsoft YaHei UI", 9)).pack(side="left")

    def run_performance(self) -> None:
        """生成逐镜头型/表情/动作/锁定后缀，并做五项质检。"""
        app = self.app
        if not app.project_path or not self.shots:
            return
        import json

        import analysis_engine
        import performance_engine

        episode = self.episode or app._selected_generation_episode()
        sync_path = app.project_path / "07_qc" / f"{episode}-sync.json"
        sync = None
        if sync_path.exists():
            try:
                sync = json.loads(sync_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                sync = None
        analysis = analysis_engine.load_analysis(app.project_path, episode)
        result = performance_engine.build(app.project_path, episode, self.shots,
                                          app._video_provider_config(), sync=sync, analysis=analysis)
        paths = performance_engine.write(app.project_path, result)
        passed = sum(1 for item in result["qc"] if item["ok"])
        total = len(result["qc"])
        app.log(f"口型与表情表：{passed}/{total} 通过 → {paths['csv'].name}")
        self.status_label.configure(
            text=f"{episode} · 口型表情 {passed}/{total} 通过 · {result['built_at'][11:]}", fg=app.MUTED)

        window = tk.Toplevel(self)
        window.title(f"口型与表情 · {episode}")
        window.geometry("660x520")
        window.configure(bg=app.BG)
        tk.Label(window, text=f"逐镜生成完毕：{passed}/{total} 通过", bg=app.BG, fg=app.TEXT,
                 font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w", padx=20, pady=(16, 4))
        failures = [item for item in result["qc"] if not item["ok"]]
        note = "全部通过：关键帧 ≥3、无笼统词、情绪与剧本一致、幅度跳变 ≤30%、锁定后缀齐全"
        if failures:
            note = "；".join(f"{item['shot']} {item['verdict']}" for item in failures[:3])
        tk.Label(window, text=note, bg=app.BG, fg=app.MUTED, wraplength=600, justify="left",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=20)

        preview = tk.Text(window, bg=app.PANEL_2, fg=app.TEXT, relief="flat", height=14,
                          font=("Microsoft YaHei UI", 9), wrap="word")
        preview.pack(fill="both", expand=True, padx=20, pady=10)
        first = result["shots"][0] if result["shots"] else {}
        lines = []
        for shot in result["shots"][:3]:
            lines.append(f"{shot['shot']}　{shot['dialogue'] or '（无台词）'}")
            for item in shot["mouth"].get("keyframes", []):
                lines.append(f"　嘴部·{item['role']}（{item['char']}）{item['shape']} @{item['time']:.2f}s")
            if shot["mouth"].get("narrator"):
                lines.append("　旁白镜：不做说话口型")
            lines.append(f"　表情：{shot['expression']['text']}")
            lines.append(f"　锁定：{shot['lock'][:90]}…")
            lines.append("")
        preview.insert("1.0", "\n".join(lines) or "没有镜头")
        preview.configure(state="disabled")

        buttons = tk.Frame(window, bg=app.BG)
        buttons.pack(fill="x", padx=20, pady=(0, 14))
        self._mini(buttons, "打开明细表 CSV", lambda: app._open_path(paths["csv"])).pack(side="right")
        self._mini(buttons, "打开设计稿 MD", lambda: app._open_path(paths["md"])).pack(side="right", padx=8)
        self._mini(buttons, "关闭", window.destroy).pack(side="right")

    # ---------- 交付登记 ----------
    def open_release_dialog(self) -> None:
        if not self.app.project_path:
            return
        app = self.app
        episode = self.episode or app._selected_generation_episode()
        rows = [row for row in qc_engine.release_rows(app.project_path) if row.get("episode") == episode]
        latest = rows[-1] if rows else {}
        window = tk.Toplevel(self)
        window.title(f"交付登记 · {episode}")
        window.geometry("640x600")
        window.configure(bg=app.BG)
        body = tk.Frame(window, bg=app.BG)
        body.pack(fill="both", expand=True, padx=22, pady=16)
        body.grid_columnconfigure(1, weight=1)

        fields = [
            ("平台", "抖音"),
            ("版本", f"{episode}-auto-final"),
            ("标题", ""),
            ("封面路径", latest.get("cover_path", "")),
            ("描述", ""),
            ("话题", ""),
            ("备注", latest.get("notes", "")),
        ]
        vars_map: dict[str, tk.StringVar] = {}
        for index, (label, default) in enumerate(fields):
            tk.Label(body, text=label, bg=app.BG, fg=app.MUTED, font=("Microsoft YaHei UI", 9)).grid(
                row=index, column=0, sticky="w", pady=6, padx=(0, 12))
            var = tk.StringVar(value=str(latest.get(label.lower(), "") or default))
            vars_map[label] = var
            if label in ("描述", "备注"):
                text = tk.Text(body, bg=app.PANEL_2, fg=app.TEXT, relief="flat", height=3,
                               font=("Microsoft YaHei UI", 9))
                text.insert("1.0", var.get())
                text.grid(row=index, column=1, sticky="ew", pady=6)
                vars_map[label] = text  # type: ignore[assignment]
            else:
                tk.Entry(body, textvariable=var, bg=app.PANEL_2, fg=app.TEXT, relief="flat",
                         font=("Microsoft YaHei UI", 9)).grid(row=index, column=1, sticky="ew", pady=6, ipady=4)

        tk.Label(body, text=f"已有交付记录：{len(rows)} 条（同集同版本会覆盖）", bg=app.BG, fg=app.MUTED,
                 font=("Microsoft YaHei UI", 8)).grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=(8, 0))

        def get(label: str) -> str:
            widget = vars_map[label]
            return widget.get("1.0", "end").strip() if isinstance(widget, tk.Text) else widget.get().strip()

        def save() -> None:
            qc_engine.register_release(app.project_path, episode, {
                "platform": get("平台"), "version": get("版本"), "title": get("标题"),
                "cover_path": get("封面路径"), "description": get("描述"),
                "hashtags": get("话题"), "notes": get("备注"),
            })
            app.log(f"交付登记已写入 release-manifest.csv：{episode} / {get('平台')} / {get('版本')}")
            window.destroy()
            self.refresh()
            messagebox.showinfo("交付登记", "已写入 08_release/release-manifest.csv。")

        buttons = tk.Frame(body, bg=app.BG)
        buttons.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self._mini(buttons, "保存登记", save).pack(side="right")
        self._mini(buttons, "取消", window.destroy).pack(side="right", padx=8)
