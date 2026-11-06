"""AI 漫剧自动化工作流 · 桌面外壳。

单窗口、单页面：外壳只承载「AI 漫剧生产线」一页，不再挂电商与星河两个工作区。
内核仍是原来的 Tk 进程，由 LegacyPage 通过窗口句柄原生挂载，两侧不共享事件循环——
壳层只负责品牌、窗口与生命周期，内核对上层零感知，也不需要改动内核一行代码。
"""
from __future__ import annotations

import codecs
import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QProcess, QProcessEnvironment, QTimer
from PySide6.QtGui import QFont, QIcon, QPixmap, QWindow
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QPushButton, QVBoxLayout, QWidget)

ROOT = Path(__file__).resolve().parent
APP_NAME = "AI 漫剧自动化工作流"
APP_EN = "AI DRAMA WORKFLOW  /  LOCAL PRODUCTION LINE"
def label(text, name=None):
    result = QLabel(text)
    if name:
        result.setObjectName(name)
    return result


def button(text, callback=None, name=None):
    result = QPushButton(text)
    if callback:
        result.clicked.connect(callback)
    if name:
        result.setObjectName(name)
    result.setCursor(Qt.CursorShape.PointingHandCursor)
    return result


def configure_process(process):
    env = QProcessEnvironment.systemEnvironment()
    env.insert("PYTHONIOENCODING", "utf-8")
    env.insert("PYTHONUNBUFFERED", "1")
    process.setProcessEnvironment(env)
    process.setWorkingDirectory(str(ROOT))
    process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)


class LegacyPage(QWidget):
    def __init__(self):
        super().__init__()
        self.process = QProcess(self)
        configure_process(self.process)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(lambda error: self.info.setText("漫剧模块启动失败：" + self.process.errorString()))
        self.busy = False
        self.buffer = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.container = None
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.info = label("AI漫剧生产线 · 正在加载原有工作流…", "muted")
        self.info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.box.addWidget(self.info)
        self.retry = button("重新加载漫剧模块", self.start, "ghost")
        self.retry.hide()
        self.box.addWidget(self.retry)

    def start(self):
        if self.process.state() != QProcess.ProcessState.NotRunning:
            return
        self.busy = False
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.buffer = ""
        self.retry.hide()
        self.process.start(sys.executable, ["-u", str(ROOT / "legacy_adapter.py")])

    def read_output(self):
        self.buffer += self.decoder.decode(bytes(self.process.readAllStandardOutput()))
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.startswith("@@LEGACY "):
                try:
                    data = json.loads(line[9:])
                    if data["type"] == "state":
                        self.busy = data["busy"]
                    elif data["type"] == "ready":
                        self.mount(data["hwnd"])
                    elif data["type"] == "description":
                        self.description = data
                except (KeyError, ValueError):
                    pass
            elif line.strip():
                self.info.setText(line.strip()[-300:])

    def mount(self, hwnd):
        self.native_hwnd = hwnd
        if os.name == "nt" and hwnd:
            self.foreign_window = QWindow.fromWinId(hwnd)
            self.container = QWidget.createWindowContainer(self.foreign_window, self)
            self.container.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.box.addWidget(self.container, 1)
            self.info.hide()
        else:
            self.info.setText("原有漫剧界面已在独立窗口打开。")

    def finished(self, code, status):
        self.busy = False
        if self.container:
            self.box.removeWidget(self.container)
            self.container.deleteLater()
            self.container = None
        self.info.setText("漫剧模块已关闭" if code == 0 else "漫剧模块退出，请检查运行环境后重新加载")
        self.info.show()
        self.retry.show()

    def request_close(self):
        self.process.write(b'{"command":"close"}\n')


def apply_dark_titlebar(widget):
    """Windows 原生标题栏默认是浅色，深色弹窗顶上会顶着一根白条；用 DWM 属性切成深色。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        enabled = ctypes.c_int(1)
        hwnd = int(widget.winId())
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE：20 为新版，19 兼容旧版
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
            ) == 0:
                break
    except Exception:
        pass


class _Glow:
    """一个漂浮的渐变光球"""
    __slots__ = ("x", "y", "vx", "vy", "r", "hue", "alpha", "phase")

    def __init__(self, w, h, hue):
        import random as _r
        self.x = _r.uniform(0, w)
        self.y = _r.uniform(0, h)
        self.vx = _r.uniform(-0.3, 0.3)
        self.vy = _r.uniform(-0.3, 0.3)
        self.r = _r.uniform(180, 380)
        self.hue = hue
        self.alpha = _r.uniform(0.18, 0.35)
        self.phase = _r.uniform(0, 6.28)

    def step(self, dt, w, h):
        self.x += self.vx * dt
        self.y += self.vy * dt
        self.phase += 0.008 * dt
        if self.x < -self.r: self.x = w + self.r
        if self.x > w + self.r: self.x = -self.r
        if self.y < -self.r: self.y = h + self.r
        if self.y > h + self.r: self.y = -self.r

    def color(self, t=0):
        from PySide6.QtGui import QColor
        a = self.alpha * (0.75 + 0.25 * __import__("math").sin(self.phase + t * 0.002))
        return QColor.fromHslF(self.hue, 0.9, 0.55, a)

    def inner(self):
        from PySide6.QtGui import QColor
        return QColor.fromHslF(self.hue, 0.75, 0.65, self.alpha * 0.0)


class _Particle:
    """一个慢速飘动的微光点"""
    __slots__ = ("x", "y", "vx", "vy", "size", "hue", "life")

    def __init__(self, w, h):
        import random as _r
        self.x = _r.uniform(0, w)
        self.y = _r.uniform(0, h)
        self.vx = _r.uniform(-0.12, 0.12)
        self.vy = _r.uniform(-0.12, 0.12)
        self.size = _r.uniform(1.2, 2.8)
        self.hue = _r.choice([0.73, 0.5])  # 紫 or 青
        self.life = _r.uniform(0.4, 1.0)

    def step(self, dt, w, h):
        self.x += self.vx * dt
        self.y += self.vy * dt
        if self.x < 0 or self.x > w: self.vx *= -1
        if self.y < 0 or self.y > h: self.vy *= -1


class DynamicBackground(QWidget):
    """全屏动态背景层，放到布局最底层即可，自动跟随窗口大小。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._glows = []
        self._particles = []
        self._grid = []
        self._t = 0
        self._init_scene()

        self._timer = QTimer(self)
        self._timer.setInterval(33)  # ~30fps
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ---------- 初始化 ----------
    def _init_scene(self):
        import random as _r
        w, h = max(self.width(), 1200), max(self.height(), 800)
        self._glows = [
            _Glow(w, h, hue=0.73),   # 电光紫 #7C5CFF
            _Glow(w, h, hue=0.5),    # 科技青 #00D4FF
            _Glow(w, h, hue=0.78),   # 粉紫
        ]
        self._particles = [_Particle(w, h) for _ in range(60)]
        # 网格点
        self._grid = []
        step = 48
        for gx in range(0, w + step, step):
            for gy in range(0, h + step, step):
                self._grid.append((gx, gy, _r.uniform(0.04, 0.12), _r.uniform(0, 6.28)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._init_scene()  # 窗口大小变了重建

    # ---------- 帧循环 ----------
    def _tick(self):
        self._t += 1
        w, h = self.width(), self.height()
        for g in self._glows:
            g.step(1, w, h)
        for p in self._particles:
            p.step(1, w, h)
        self.update()

    # ---------- 绘制 ----------
    def paintEvent(self, _evt):
        from PySide6.QtGui import QPainter, QRadialGradient, QLinearGradient, QColor, QPen
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # 1) 底色渐变
        grad = QLinearGradient(0, 0, w, h)
        grad.setColorAt(0.0, QColor("#080B14"))
        grad.setColorAt(0.5, QColor("#0C1020"))
        grad.setColorAt(1.0, QColor("#0A0E18"))
        painter.fillRect(self.rect(), grad)

        # 2) 漂浮光球（radial gradient 多次叠加制造虚化）
        for g in self._glows:
            cx, cy = int(g.x), int(g.y)
            for i, scale in enumerate((1.0, 0.7, 0.45)):
                rr = int(g.r * scale)
                if rr < 2:
                    continue
                rg = QRadialGradient(cx, cy, rr)
                col = g.color(self._t - i * 5)
                rg.setColorAt(0.0, col)
                rg.setColorAt(0.45, QColor(col.red(), col.green(), col.blue(), int(col.alpha() * 0.35)))
                rg.setColorAt(1.0, QColor(col.red(), col.green(), col.blue(), 0))
                painter.fillRect(self.rect(), rg)

        # 3) 数字网格点（很淡）
        pen = QPen(QColor(255, 255, 255, 14))
        pen.setWidthF(0.6)
        painter.setPen(pen)
        for gx, gy, base_a, phase in self._grid:
            t = (self._t * 0.012 + phase)
            a = int(14 * (0.4 + 0.6 * abs(__import__("math").sin(t))))
            painter.setPen(QPen(QColor(255, 255, 255, a)))
            painter.drawPoint(gx, gy)

        # 4) 漂浮粒子
        for p in self._particles:
            alpha = int(180 * p.life)
            col = QColor.fromHslF(p.hue, 0.85, 0.7, alpha / 255.0)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(col)
            painter.drawEllipse(int(p.x - p.size / 2), int(p.y - p.size / 2), int(p.size), int(p.size))

        # 5) 顶部/底部暗角 vignette
        vig = QLinearGradient(0, 0, 0, h)
        vig.setColorAt(0.0, QColor(0, 0, 0, 120))
        vig.setColorAt(0.15, QColor(0, 0, 0, 0))
        vig.setColorAt(0.85, QColor(0, 0, 0, 0))
        vig.setColorAt(1.0, QColor(0, 0, 0, 140))
        painter.fillRect(self.rect(), vig)

        painter.end()



class MainWindow(QMainWindow):
    """单页外壳：顶部一条品牌栏 + 漫剧生产线页面。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        icon_file = ROOT / "assets" / "app-icon.ico"
        if icon_file.is_file():
            self.setWindowIcon(QIcon(str(icon_file)))
        screen = QApplication.primaryScreen().availableGeometry()
        self.setMinimumSize(min(1180, screen.width() - 40), min(800, screen.height() - 60))
        self.resize(min(1440, screen.width() - 40), min(960, screen.height() - 20))
        self.closing = False

        root = QWidget()
        root.setObjectName("workspace")
        self.setCentralWidget(root)

        # 动态背景层：不参与布局，z-order 最低，跟窗口一起拉伸
        self._bg = DynamicBackground(root)
        self._bg.setGeometry(0, 0, root.width(), root.height())
        self._bg.lower()
        orig_resize = root.resizeEvent

        def _root_resize(event):
            orig_resize(event)
            self._bg.setGeometry(0, 0, root.width(), root.height())

        root.resizeEvent = _root_resize

        column = QVBoxLayout(root)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self._build_topbar())

        self.page = LegacyPage()
        self.page.process.finished.connect(self.page_closed)
        column.addWidget(self.page, 1)

        self.page.start()

    def _build_topbar(self):
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(66)
        bar.setStyleSheet(
            "QFrame#topbar { background: rgba(8, 12, 22, 0.82);"
            " border-bottom: 1px solid rgba(120, 165, 220, 0.18); }"
            "QFrame#topbar QLabel { background: transparent; }"
        )
        row = QHBoxLayout(bar)
        row.setContentsMargins(20, 10, 22, 10)
        row.setSpacing(12)

        logo = QLabel()
        logo.setFixedSize(36, 36)
        logo.setStyleSheet("background: transparent;")
        pix = QPixmap(str(ROOT / "assets" / "app-icon.png"))
        if not pix.isNull():
            logo.setPixmap(pix.scaled(36, 36, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))
        row.addWidget(logo)

        titles = QVBoxLayout()
        titles.setSpacing(1)
        titles.addWidget(label(APP_NAME, "brand"))
        titles.addWidget(label(APP_EN, "brandEn"))
        row.addLayout(titles)
        row.addStretch()
        row.addWidget(label("本地工作流 · 正文与素材只留本机", "muted"))
        return bar

    def page_closed(self, code, status):
        if self.closing:
            self.close()

    def closeEvent(self, event):
        if self.page.process.state() != QProcess.ProcessState.NotRunning:
            # 内核还在跑：先走它自己的保存与关闭流程，退出后由 page_closed 收尾。
            self.closing = True
            self.page.request_close()
            QTimer.singleShot(1500, self.force_close_if_needed)
            event.ignore()
            return
        event.accept()

    def force_close_if_needed(self):
        if self.page.process.state() != QProcess.ProcessState.NotRunning:
            self.closing = False
            self.statusBar().showMessage("漫剧任务仍在运行，已保留内核进程，请等待完成后关闭。")

    def showEvent(self, event):
        super().showEvent(event)
        apply_dark_titlebar(self)


def _set_windows_app_id():
    """任务栏图标默认跟着 pythonw.exe 走，必须先声明应用身份，Windows 才会用窗口图标。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("XingheStudio.AiDramaWorkflow")
    except (AttributeError, OSError):
        pass


def main():
    _set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    icon_file = ROOT / "assets" / "app-icon.ico"
    if icon_file.is_file():
        app.setWindowIcon(QIcon(str(icon_file)))
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet((ROOT / "styles.qss").read_text(encoding="utf-8"))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
