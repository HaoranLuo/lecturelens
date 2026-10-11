@echo off
rem LectureLens — 一键启动课堂字幕（首次运行自动装依赖）
cd /d D:\projects\lecture-translate
if not exist .venv (
    echo [setup] creating venv...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
)
start "" http://localhost:8787
.venv\Scripts\python.exe app\main.py %*
