@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" main.py
) else (
  python main.py
)
if errorlevel 1 (
  echo 启动失败。请按照 README.md 安装依赖并配置 .env.local。
  pause
)
