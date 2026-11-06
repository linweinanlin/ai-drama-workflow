"""GUI 启动入口：把异常写进本机日志，pythonw 启动失败时也能看见原因。"""
from __future__ import annotations

import ctypes
import datetime
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    os.chdir(str(ROOT))
    with (ROOT / "startup.log").open("a", encoding="utf-8", buffering=1) as log:
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = log
        print("\nSTART", datetime.datetime.now().isoformat(), sys.executable, flush=True)
        try:
            from app import main as run_app
            return run_app()
        except Exception:
            traceback.print_exc(file=log)
            log.flush()
            if os.name == "nt":
                ctypes.windll.user32.MessageBoxW(
                    None,
                    f"启动失败，错误详情已保存到：\n{ROOT / 'startup.log'}",
                    "AI 漫剧自动化工作流",
                    0x10,
                )
            return 1
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr


if __name__ == "__main__":
    raise SystemExit(main())
