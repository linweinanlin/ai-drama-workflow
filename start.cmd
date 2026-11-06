@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [1/3] 正在创建虚拟环境...
    python -m venv .venv
    if errorlevel 1 goto failed
)
"%PY%" -c "import PySide6, PIL, numpy, requests" >nul 2>&1
if errorlevel 1 (
    echo [2/3] 正在安装依赖...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 goto failed
)
echo [3/3] 启动 AI 漫剧自动化工作流...
"%PY%" launch.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo 启动失败，详情见本目录 startup.log
pause
exit /b 1
