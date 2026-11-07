@echo off
REM Double-click this to open the FK Sales Console in your browser.
REM Keep this window open while you use it; close it to stop.
cd /d "%~dp0"
start "" http://127.0.0.1:8765
py pipeline\console.py
