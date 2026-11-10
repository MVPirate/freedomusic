@echo off
chcp 65001 > nul
cd /d "%~dp0"
call venv\Scripts\activate
echo ========================================
echo Запуск API (FastAPI) на порту 8000...
echo ========================================
uvicorn api:app --reload --port 8000
pause