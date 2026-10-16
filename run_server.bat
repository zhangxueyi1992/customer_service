@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动智能客服系统（http://127.0.0.1:8000）...
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
