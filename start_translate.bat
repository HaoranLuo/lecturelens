@echo off
rem LectureLens launcher. ASCII only on purpose: cmd.exe parses this file with
rem the GBK codepage, and UTF-8 Chinese here corrupts every following command.
title LectureLens
cd /d D:\projects\lecture-translate
if not exist .venv (
    echo [setup] creating venv, first run takes a few minutes...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
)
echo [LectureLens] starting desktop window... closing the window hides to
echo the tray; quit from the tray icon menu.
.venv\Scripts\python.exe app\desktop.py %*
echo.
echo [LectureLens] app exited. Press any key to close.
pause >nul
