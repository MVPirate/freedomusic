@echo off
chcp 65001 > nul
cd /d "%~dp0"
call venv\Scripts\activate
echo ========================================
echo Запуск Telegram Бота...
echo ========================================
python bot.py
pause